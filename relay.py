"""relay.py: the dashboard's recording relay.

proxy.py stays exactly as it is, and keeps running. The agent talks to this
relay, which records each exchange and forwards it to the running proxy.py
(which forwards to the API as always):

    claude -> relay :8040 -> proxy.py :8041 -> api.anthropic.com
                 |
              dashboard page :8042

Forwarding works like proxy.py: same headers dropped, the reply streamed back
chunk by chunk. Each chunk is sent on first and handed to the recorder after
(SPEC C2). Recorder failures are logged and forwarding carries on.
"""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

# Same hop-by-hop list as proxy.py. accept-encoding is dropped so replies come back as plain text.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "https://api.anthropic.com"   # set by make_relay
    rec = None                               # recorder.Recorder
    log = print

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def relay(self) -> None:
        REC = self.rec
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = REC.start(self.command, self.path, self.headers.items(), body) if REC else None

        target = urlsplit(self.upstream)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Short connect timeout (a lost SYN must not stall the agent); the long one is for slow streams.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            self.log(f"!!! relay cannot reach {self.upstream}: {e}")
            self.send_error(502, f"dashboard relay: cannot reach {self.upstream}: {e}")
            self.close_connection = True
            if REC:
                REC.response(rec, 502, "Bad Gateway", [])
                REC.finish(rec, "error", f"relay: cannot reach {self.upstream}: {e}")
            return
        if REC:
            REC.connected(rec)
        upstream.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        try:
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except BaseException as e:
            if REC:
                REC.finish(rec, "error", f"upstream failed before replying: {e!r}")
            raise

        if REC:
            REC.response(rec, reply.status, reply.reason, reply.getheaders())
        self.send_response(reply.status, reply.reason)
        for name, value in reply.getheaders():
            if name.lower() not in SKIP and name.lower() != "content-encoding":
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            if REC:
                REC.finish(rec)
            return
        first = True
        while True:
            try:
                chunk = reply.read1(4096)  # pass on each chunk as it lands
            except BaseException as e:
                if REC:
                    REC.finish(rec, "error", f"upstream stream failed: {e!r}")
                raise
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except BaseException as e:
                if REC:
                    REC.chunk(rec, chunk, first)
                    REC.finish(rec, "aborted", f"client disconnected: {e!r}")
                raise
            if REC:  # only after the bytes have gone on (SPEC C2)
                REC.chunk(rec, chunk, first)
            first = False
        self.close_connection = True
        if REC:
            REC.finish(rec)


def make_relay_handler(upstream: str, rec, log=print):
    return type("RelayHandler", (Relay,), {"upstream": upstream.rstrip("/"), "rec": rec,
                                            "log": staticmethod(log)})
