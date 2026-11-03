"""Phase 2: dashboard server, API, SSE resume, security headers."""

import http.client
import json
import socket
import threading
import time
import unittest

from tests.harness import SECRET_MARKERS, Chain, Dash, FakeUpstream, call, fixture


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def jget(port, path):
    st, _, body = get(port, path)
    assert st == 200, (path, st, body[:200])
    return json.loads(body)


class SSEClient:
    """Minimal SSE reader on a background thread."""

    def __init__(self, port, last=None):
        self.events = []
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        q = f"?last={last}" if last is not None else ""
        self.sock.sendall(f"GET /api/events{q} HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _read(self):
        buf = b""
        try:
            while chunk := self.sock.recv(65536):
                buf += chunk
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    ev = {}
                    for line in block.decode().split("\n"):
                        k, _, v = line.partition(": ")
                        if k in ("id", "event", "data"):
                            ev[k] = v
                    if "event" in ev:
                        ev["id"] = int(ev["id"])
                        ev["data"] = json.loads(ev["data"])
                        self.events.append(ev)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = FakeUpstream().__enter__()
        cls.proxy = Chain(cls.up.url).__enter__()
        cls.d = cls.proxy.dash_port

    @classmethod
    def tearDownClass(cls):
        cls.proxy.__exit__(None, None, None)
        cls.up.__exit__(None, None, None)

    def send(self, name="text_stream.sse", **h):
        hdr = {"x-fixture": name}
        hdr.update(h)
        return call(self.proxy.port, fixture("request_messages.json"), hdr)

    def test_page_and_headers(self):
        st, h, body = get(self.d, "/", {"Host": f"localhost:{self.d}"})
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Proxy Traffic</title>", body)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        for sink in (b"innerHTML", b"outerHTML", b"insertAdjacentHTML", b"document.write", b"eval("):
            self.assertNotIn(sink, body)              # C5: content is never parsed as HTML
        import re
        self.assertFalse(re.search(rb"(src|href)=[\"']?(https?:)?//", body))  # C1: nothing loaded from elsewhere

    def test_dns_rebinding_guard(self):
        st, _, _ = get(self.d, "/api/exchanges", {"Host": "evil.example:8042"})
        self.assertEqual(st, 403)

    def test_loopback_only(self):
        out = self.proxy.output()
        self.assertIn("dashboard on http://localhost:", out)
        import subprocess
        ss = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
        for port in (self.proxy.port, self.d):
            lines = [line for line in ss.splitlines() if f":{port} " in line]
            self.assertTrue(lines, port)
            for line in lines:
                self.assertIn("127.0.0.1:", line)

    def test_api_and_no_secrets(self):
        self.send("tool_use_stream.sse")
        time.sleep(0.2)
        data = jget(self.d, "/api/exchanges")
        self.assertGreater(data["seq"], 0)
        ex_sum = data["exchanges"][-1]
        self.assertEqual(ex_sum["stop_reason"], "tool_use")
        ex = jget(self.d, f"/api/exchanges/{ex_sum['id']}")
        raw = jget(self.d, f"/api/exchanges/{ex_sum['id']}/raw")
        blob = jget(self.d, f"/api/blob/{ex['request']['tools'][0]}")
        self.assertEqual(blob["data"]["name"], "Bash")
        self.assertEqual(raw["response_body"], fixture("tool_use_stream.sse").decode())
        st, h, _ = get(self.d, f"/api/blob/{ex['request']['tools'][0]}")
        self.assertIn("immutable", h["Cache-Control"])
        everything = json.dumps([data, ex, raw, blob])
        for m in SECRET_MARKERS:
            self.assertNotIn(m, everything)
        self.assertEqual(get(self.d, "/api/exchanges/99999")[0], 404)
        self.assertEqual(get(self.d, "/api/blob/zzz")[0], 404)
        self.assertEqual(get(self.d, "/nope")[0], 404)

    def test_api_port_unaffected(self):
        # D1: dashboard paths on the API port are forwarded upstream like any other path, never served locally.
        call(self.proxy.port, b"", {"x-fixture": "count_tokens.json"}, path="/api/exchanges", method="GET")
        self.assertEqual(self.up.received[-1]["path"], "/api/exchanges")

    def test_sse_live_and_resume_without_gaps(self):
        start_seq = jget(self.d, "/api/exchanges")["seq"]
        c1 = SSEClient(self.d, last=start_seq)
        time.sleep(0.2)
        self.send("text_stream.sse", **{"x-chunks": "10", "x-gap-ms": "20"})
        self.assertTrue(wait_for(lambda: any(e["event"] == "exchange.end" for e in c1.events)))
        kinds = [e["event"] for e in c1.events if e["event"] != "threads"]
        self.assertEqual(kinds[0], "exchange.start")
        self.assertIn("exchange.delta", kinds)
        text = "".join(n.get("append", "") for e in c1.events if e["event"] == "exchange.delta"
                       for n in e["data"]["notes"] if n["op"] == "text")
        self.assertEqual(text, "Tests pass. Next I'll check the docs — ✓ ünïcode.")
        # drop the connection mid-way through a second exchange, then resume from the last id we saw
        c1.events.clear()
        t = threading.Thread(target=self.send, args=("tool_use_stream.sse",), kwargs={"x-chunks": "20", "x-gap-ms": "15"})
        t.start()
        self.assertTrue(wait_for(lambda: len(c1.events) >= 2))
        c1.close()
        seen = list(c1.events)
        last = seen[-1]["id"]
        t.join()
        time.sleep(0.2)
        c2 = SSEClient(self.d, last=last)
        self.assertTrue(wait_for(lambda: any(e["event"] == "exchange.end" for e in c2.events)))
        c2.close()
        ids = [e["id"] for e in seen] + [e["id"] for e in c2.events]
        self.assertEqual(ids, list(range(ids[0], ids[0] + len(ids))), "gap or duplicate in event ids")


class EventBuffer(unittest.TestCase):
    def test_resync_when_resume_point_was_dropped(self):
        from store import EVENT_BUFFER, Store
        st = Store()
        for i in range(EVENT_BUFFER + 10):
            st.emit("exchange.delta", {"i": i})
        _, backlog = st.subscribe(last_id=3)            # too old: the page must refetch
        self.assertIsNone(backlog)
        _, backlog = st.subscribe(last_id=st.seq - 4)   # recent: exact replay
        self.assertEqual([e[0] for e in backlog], list(range(st.seq - 3, st.seq + 1)))

    def test_slow_subscriber_never_blocks(self):
        from store import Store
        st = Store()
        q, _ = st.subscribe()
        t0 = time.time()
        for i in range(15000):                        # queue holds 10000
            st.emit("exchange.delta", {"i": i})
        self.assertLess(time.time() - t0, 2)
        self.assertNotIn(q, st.subscribers)


class Load(unittest.TestCase):
    def test_load_serves_dashboard(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "t.jsonl")
            with FakeUpstream() as up, Chain(up.url, extra=["--log", log]) as p:
                call(p.port, fixture("request_messages.json"), {"x-fixture": "thinking_stream.sse"})
                time.sleep(0.3)
            lp = Dash(None, extra=["--load", log])
            with lp:
                data = jget(lp.dash_port, "/api/exchanges")
                self.assertEqual(len(data["exchanges"]), 1)
                self.assertEqual(jget(lp.dash_port, "/api/config")["mode"], "load")
                # nothing listens on the API port in --load mode
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", lp.port), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
