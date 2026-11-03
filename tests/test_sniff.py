"""--sniff: TCP reassembly and HTTP parsing from loopback packets (synthetic frames; no root needed)."""

import json
import random
import socket
import struct
import unittest

from recorder import Recorder
from sniff import FIN, PACKET_OUTGOING, RST, SYN, Sniffer, drop_privileges
from store import Store
from tests.harness import SECRET_BEARER, SECRET_MARKERS, fixture
from threads import Threads

ACK, PSH = 0x10, 0x08
PORT = 8041


def frame(src, sport, dst, dport, seq, flags, data=b""):
    tcp = struct.pack("!HHIIBBHHH", sport, dport, seq, 0, 5 << 4, flags, 65535, 0, 0) + data
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    return b"\0" * 12 + b"\x08\x00" + ip + tcp


class Conn:
    """Builds the packets of one client connection to proxy.py."""

    def __init__(self, cport, cseq=1000, sseq=50000):
        self.cport, self.cseq, self.sseq = cport, cseq, sseq

    def c(self, flags, data=b""):
        f = frame("127.0.0.1", self.cport, "127.0.0.1", PORT, self.cseq, flags, data)
        self.cseq += len(data) + (1 if flags & (SYN | FIN) else 0)
        return f

    def s(self, flags, data=b""):
        f = frame("127.0.0.1", PORT, "127.0.0.1", self.cport, self.sseq, flags, data)
        self.sseq += len(data) + (1 if flags & (SYN | FIN) else 0)
        return f


def request(body: bytes) -> bytes:
    return (b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: localhost:8041\r\nContent-Type: application/json\r\n"
            b"Authorization: " + SECRET_BEARER.encode() + b"\r\nContent-Length: %d\r\n\r\n" % len(body)) + body


def response(status=200, body=b"", ctype=b"text/event-stream", length=False):
    head = b"HTTP/1.1 %d OK\r\nContent-Type: %s\r\nConnection: close\r\n" % (status, ctype)
    if length:
        head += b"Content-Length: %d\r\n" % len(body)
    return head + b"\r\n" + body


def pieces(data, n):
    size = max(1, -(-len(data) // n))
    return [data[i:i + size] for i in range(0, len(data), size)]


def exchange_packets(c: Conn, req: bytes, resp: bytes, split=7):
    pk = [c.c(SYN), c.s(SYN | ACK), c.c(ACK)]
    pk += [c.c(ACK | PSH, p) for p in pieces(req, split)]
    pk += [c.s(ACK | PSH, p) for p in pieces(resp, split * 3)]
    pk += [c.s(FIN | ACK), c.c(FIN | ACK)]
    return pk


class SniffTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        Threads(self.store)
        self.rec = Recorder(self.store, log=lambda *_: None)
        self.sn = Sniffer(self.rec, PORT, log=lambda *_: None)

    def feed(self, packets, dup=True):
        for p in packets:
            if dup:                                  # loopback shows each packet twice
                self.sn.feed(p, PACKET_OUTGOING)
            self.sn.feed(p, 0)
        self.rec.flush()

    def last(self):
        return self.store.get(max(self.store.exchanges))

    def test_streaming_exchange(self):
        body = fixture("request_messages.json")
        self.feed(exchange_packets(Conn(40001), request(body), response(body=fixture("tool_use_stream.sse"))))
        ex = self.last()
        self.assertEqual(len(self.store.exchanges), 1)
        self.assertEqual((ex["kind"], ex["status"], ex["state"]), ("messages", 200, "done"))
        self.assertEqual(ex["reply"]["content"][1]["input"]["command"], "python3 -m unittest")
        self.assertEqual(self.store.get_raw(ex["id"])["request_body"], body.decode())
        self.assertEqual(self.store.get_raw(ex["id"])["response_body"], fixture("tool_use_stream.sse").decode())
        dump = json.dumps([ex, self.store.get_raw(ex["id"])])
        for m in SECRET_MARKERS:
            self.assertNotIn(m, dump)

    def test_out_of_order_and_retransmits(self):
        body = fixture("request_messages.json")
        pk = exchange_packets(Conn(40002), request(body), response(body=fixture("text_stream.sse")), split=12)
        head, mid, tail = pk[:3], pk[3:-2], pk[-2:]
        rng = random.Random(3)
        # swap neighbours and repeat some packets, like a busy loopback might
        for i in range(0, len(mid) - 1, 3):
            mid[i], mid[i + 1] = mid[i + 1], mid[i]
        mid += rng.sample(mid, 5)
        self.feed(head + mid + tail)
        ex = self.last()
        self.assertEqual(ex["state"], "done")
        self.assertEqual(ex["reply"]["content"][0]["text"], "Tests pass. Next I'll check the docs — ✓ ünïcode.")

    def test_fin_before_last_data_waits(self):
        c = Conn(40003)
        pk = exchange_packets(c, request(b"{}"), response(body=fixture("text_stream.sse")))
        last_data, fin = pk[-3], pk[-2]
        self.feed(pk[:-3] + [fin, last_data, pk[-1]])
        self.assertEqual(self.last()["state"], "done")

    def test_many_connections_interleaved(self):
        a = exchange_packets(Conn(40010), request(fixture("request_messages.json")), response(body=fixture("text_stream.sse")))
        b = exchange_packets(Conn(40011), request(fixture("request_messages.json")), response(body=fixture("thinking_stream.sse")))
        mixed = [p for pair in zip(a, b) for p in pair] + a[len(b):] + b[len(a):]
        self.feed(mixed)
        self.assertEqual(len(self.store.exchanges), 2)
        self.assertTrue(all(e["state"] == "done" for e in self.store.exchanges.values()))

    def test_error_with_content_length(self):
        err = fixture("error_529.json")
        self.feed(exchange_packets(Conn(40004), request(b"{}"), response(529, err, b"application/json", length=True)))
        ex = self.last()
        self.assertEqual(ex["status"], 529)
        self.assertEqual(ex["error"]["type"], "upstream_http")

    def test_client_reset_is_aborted(self):
        c = Conn(40005)
        pk = [c.c(SYN), c.s(SYN | ACK), c.c(ACK), c.c(ACK | PSH, request(b"{}")),
              c.s(ACK | PSH, response(body=fixture("text_stream.sse")[:200])), c.c(RST)]
        self.feed(pk)
        self.assertEqual(self.last()["state"], "aborted")

    def test_joined_mid_connection_is_skipped(self):
        c = Conn(40006)
        full = request(fixture("request_messages.json"))
        pk = exchange_packets(c, full, response(body=fixture("text_stream.sse")), split=5)
        self.feed(pk[5:])                                   # no SYN, starts inside the request body
        self.assertEqual(len(self.store.exchanges), 0)
        self.feed(exchange_packets(Conn(40007), request(b"{}"), response(body=fixture("text_stream.sse"))))
        self.assertEqual(len(self.store.exchanges), 1)       # the next connection is recorded

    def test_other_ports_ignored(self):
        f = frame("127.0.0.1", 40008, "127.0.0.1", 9999, 1, ACK | PSH, request(b"{}"))
        self.feed([f])
        self.assertEqual(len(self.store.exchanges), 0)

    def test_no_privileges_to_drop_when_not_root(self):
        self.assertIsNone(drop_privileges())


if __name__ == "__main__":
    unittest.main()
