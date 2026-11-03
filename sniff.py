"""sniff.py: watch proxy.py's port on the loopback interface, without touching the proxy or the agents.

Agents talk to proxy.py over plain HTTP on 127.0.0.1:8041. This module reads a
copy of those packets (Linux AF_PACKET socket on "lo"), puts each TCP
connection's two byte streams back together, parses the HTTP request and
response, and hands them to the same recorder the relay uses. It never sends
anything and cannot slow down or change the traffic.

Opening the capture socket needs root (or CAP_NET_RAW). dashboard.py opens it
first and then drops back to the invoking user (SUDO_UID/SUDO_GID) before doing
anything else.

Limits: a request already half-sent when capture starts is skipped (the next one
on a new connection is recorded). If the kernel drops packets under heavy load,
the exchange is marked "capture incomplete".
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time

ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4          # on "lo" every packet is seen twice; keep only the incoming copy
FIN, SYN, RST = 0x01, 0x02, 0x04
METHODS = (b"GET ", b"POST ", b"PUT ", b"HEAD ", b"DELETE ", b"PATCH ", b"OPTIONS ")
MAX_HEADER = 256 * 1024
IDLE_S = 900


def open_capture(iface: str = "lo") -> socket.socket:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(ETH_P_ALL))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
    s.bind((iface, 0))
    return s


def drop_privileges() -> str | None:
    """After the capture socket is open: become the user who ran sudo. Returns the user name."""
    if os.geteuid() != 0:
        return None
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not gid:
        raise SystemExit("refusing to keep running as root: start it with sudo from your normal user")
    os.setgroups([])
    os.setgid(int(gid))
    os.setuid(int(uid))
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("could not drop root privileges")
    os.environ["HOME"] = os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}")
    return os.environ.get("SUDO_USER")


# --- packet parsing -----------------------------------------------------------

def parse_frame(frame: bytes):
    """Ethernet (as framed on lo) + IPv4/IPv6 + TCP -> (src, sport, dst, dport, seq, flags, payload) or None."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    ip = frame[14:]
    if ethertype == 0x0800 and len(ip) >= 20:
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] != 6:
            return None
        total = struct.unpack("!H", ip[2:4])[0]
        src, dst = socket.inet_ntop(socket.AF_INET, ip[12:16]), socket.inet_ntop(socket.AF_INET, ip[16:20])
        tcp = ip[ihl:total] if total else ip[ihl:]
    elif ethertype == 0x86DD and len(ip) >= 40:
        if ip[6] != 6:                       # no extension headers on loopback HTTP
            return None
        plen = struct.unpack("!H", ip[4:6])[0]
        src, dst = socket.inet_ntop(socket.AF_INET6, ip[8:24]), socket.inet_ntop(socket.AF_INET6, ip[24:40])
        tcp = ip[40:40 + plen]
    else:
        return None
    if len(tcp) < 20:
        return None
    sport, dport, seq = struct.unpack("!HHI", tcp[:8])
    off = (tcp[12] >> 4) * 4
    flags = tcp[13]
    return src, sport, dst, dport, seq, flags, tcp[off:]


class Stream:
    """One direction of a TCP connection, put back in order."""

    def __init__(self):
        self.next = None             # next expected sequence number
        self.pending: dict[int, bytes] = {}
        self.fin = False
        self.gap = False

    def add(self, seq: int, flags: int, data: bytes) -> bytes:
        if flags & SYN:
            self.next = (seq + 1) & 0xFFFFFFFF
            return b""
        if self.next is None:
            if not data:
                return b""
            self.next = seq          # joined mid-connection
        out = []
        if data:
            d = (seq - self.next) & 0xFFFFFFFF
            if d < 0x80000000:       # at or ahead of what we expect
                self.pending.setdefault(seq, data)
            else:                    # retransmission overlapping data we already have
                back = (self.next - seq) & 0xFFFFFFFF
                if back < len(data):
                    self.pending.setdefault(self.next, data[back:])
        while self.next in self.pending:
            chunk = self.pending.pop(self.next)
            out.append(chunk)
            self.next = (self.next + len(chunk)) & 0xFFFFFFFF
        if flags & FIN:
            self.fin = True
        if len(self.pending) > 4096:
            self.gap = True
        return b"".join(out)


def _headers(block: bytes):
    lines = block.decode("latin-1").split("\r\n")
    hdrs = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs.append((k.strip(), v.strip()))
    return lines[0], hdrs


class Flow:
    """One client connection to proxy.py: HTTP requests out, responses back."""

    def __init__(self, rec):
        self.rec = rec
        self.c2s, self.s2c = Stream(), Stream()
        self.req_buf = b""
        self.resp_buf = b""
        self.queue: list = []        # live recorder handles waiting for their response, in order
        self.cur = None              # handle whose response is being received
        self.resp_left = None        # remaining body bytes (None = until close)
        self.resp_started = False
        self.first = True
        self.skip = False            # joined mid-request: ignore this connection
        self.seen = time.time()
        self.done = False

    # client -> proxy
    def on_client(self, data: bytes):
        if self.skip or not data:
            return
        self.req_buf += data
        while True:
            if not self.req_buf:
                return
            if not self.req_buf.startswith(METHODS) and len(self.req_buf) >= 8:
                self.skip = True     # not the start of a request: we joined mid-stream
                return
            end = self.req_buf.find(b"\r\n\r\n")
            if end < 0:
                if len(self.req_buf) > MAX_HEADER:
                    self.skip = True
                return
            line, hdrs = _headers(self.req_buf[:end])
            n = int(next((v for k, v in hdrs if k.lower() == "content-length"), "0") or 0)
            if len(self.req_buf) < end + 4 + n:
                return
            body = self.req_buf[end + 4:end + 4 + n]
            self.req_buf = self.req_buf[end + 4 + n:]
            parts = line.split(" ")
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else (line, "")
            live = self.rec.start(method, path, hdrs, body)
            if live is not None:
                self.queue.append(live)

    # proxy -> client
    def on_server(self, data: bytes):
        if self.skip or not data:
            return
        self.resp_buf += data
        while self.resp_buf:
            if not self.resp_started:
                if not self.queue:
                    if not self.resp_buf.startswith(b"HTTP/"):
                        self.skip = True
                    return
                end = self.resp_buf.find(b"\r\n\r\n")
                if end < 0:
                    return
                line, hdrs = _headers(self.resp_buf[:end])
                self.resp_buf = self.resp_buf[end + 4:]
                parts = line.split(" ", 2)
                status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                self.cur = self.queue.pop(0)
                self.rec.response(self.cur, status, parts[2] if len(parts) > 2 else "", hdrs)
                cl = next((v for k, v in hdrs if k.lower() == "content-length"), None)
                self.resp_left = int(cl) if cl and cl.isdigit() else None
                self.resp_started, self.first = True, True
                if self.resp_left == 0:
                    self._end_response()
                continue
            chunk = self.resp_buf if self.resp_left is None else self.resp_buf[:self.resp_left]
            self.resp_buf = self.resp_buf[len(chunk):]
            if chunk:
                self.rec.chunk(self.cur, chunk, self.first)
                self.first = False
            if self.resp_left is not None:
                self.resp_left -= len(chunk)
                if self.resp_left <= 0:
                    self._end_response()
            else:
                return

    def _end_response(self, state="done", error=None):
        if self.cur is not None:
            self.rec.finish(self.cur, state, error)
        self.cur, self.resp_started, self.resp_left = None, False, None

    def close(self, reset: bool = False):
        if self.done:
            return
        self.done = True
        gap = self.c2s.gap or self.s2c.gap
        if self.cur is not None:
            if gap:
                self._end_response("error", "capture incomplete: packets were missed")
            elif self.resp_left is None and self.s2c.fin and not reset:
                self._end_response()
            else:
                self._end_response("aborted", "connection closed before the reply finished")
        for live in self.queue:      # requests that never got a response
            self.rec.finish(live, "aborted", "connection closed before a reply")
        self.queue = []


class Sniffer:
    """Feeds captured frames for one TCP port into the recorder."""

    def __init__(self, rec, port: int, log=print):
        self.rec, self.port, self.log = rec, port, log
        self.flows: dict[tuple, Flow] = {}
        self.lock = threading.Lock()

    def feed(self, frame: bytes, pkttype: int = 0):
        if pkttype == PACKET_OUTGOING:
            return
        p = parse_frame(frame)
        if p is None:
            return
        src, sport, dst, dport, seq, flags, data = p
        if dport == self.port:
            key, to_server = (src, sport), True
        elif sport == self.port:
            key, to_server = (dst, dport), False
        else:
            return
        flow = self.flows.get(key)
        if flow is None or flow.done:
            if flow is not None and flow.done and not (flags & SYN):
                return                                   # trailing packets of a finished connection
            flow = self.flows[key] = Flow(self.rec)
        flow.seen = time.time()
        stream = flow.c2s if to_server else flow.s2c
        data = stream.add(seq, flags, data)
        if to_server:
            flow.on_client(data)
        else:
            flow.on_server(data)
        if flags & RST:
            flow.close(reset=True)
        elif flow.s2c.fin and not flow.s2c.pending and not flow.c2s.pending:
            flow.close()

    def sweep(self):
        now = time.time()
        for key, flow in list(self.flows.items()):
            stalled = flow.s2c.fin and now - flow.seen > 5          # FIN seen, but data still missing
            if flow.done or stalled or now - flow.seen > IDLE_S:
                if not flow.done:
                    flow.s2c.gap = flow.s2c.gap or bool(flow.s2c.pending)
                    flow.close()
                del self.flows[key]

    def run(self, sock: socket.socket):
        last = time.time()
        while True:
            frame, addr = sock.recvfrom(262144)
            try:
                self.feed(frame, addr[2] if len(addr) > 2 else 0)
            except Exception as e:  # noqa: BLE001 - a bad packet must not stop the capture
                self.log(f"[sniff] skipped a packet: {e!r}")
            if time.time() - last > 5:
                self.sweep()
                last = time.time()
