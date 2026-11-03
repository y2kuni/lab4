"""Browser tests (headless Chrome over CDP). Skipped when no Chrome is installed."""

import json
import threading
import time
import unittest

from tests.cdp import CHROME, Browser
from tests.harness import SECRET_MARKERS, FakeUpstream, Proxy, call, fixture


@unittest.skipUnless(CHROME, "needs Chrome/Chromium")
class LiveUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = FakeUpstream().__enter__()
        cls.proxy = Proxy(cls.up.url, extra=["--no-print"], dash=True).__enter__()
        cls.url = f"http://localhost:{cls.proxy.dash_port}/"
        cls.b = Browser().__enter__()
        cls.b.goto(cls.url)
        cls.b.wait("document.querySelector('#conn').className === 'live'")

    @classmethod
    def tearDownClass(cls):
        cls.b.__exit__(None, None, None)
        cls.proxy.__exit__(None, None, None)
        cls.up.__exit__(None, None, None)

    def send(self, name="text_stream.sse", body=None, **h):
        hdr = {"x-fixture": name}
        hdr.update(h)
        return call(self.proxy.port, body or fixture("request_messages.json"), hdr)

    def test_1_first_row_within_1s_and_reply_streams(self):
        b = self.b
        n0 = b.js("S.order.length")
        t = threading.Thread(target=self.send, kwargs={"x-chunks": "40", "x-gap-ms": "60", "x-delay-ms": "100"})
        t0 = time.time()
        t.start()
        b.wait(f"S.order.length > {n0}", timeout=1.0)            # acceptance 1
        self.assertLess(time.time() - t0, 1.0)
        b.wait("document.querySelector('.row.sel .spin')")       # streaming spinner on the new row
        # the reply text grows while the exchange is still streaming
        b.wait("(document.querySelector('#replysec')?.textContent || '').includes('Tests pass')", timeout=3)
        self.assertEqual(b.js("S.ex.get(S.selected).state"), "streaming")
        t.join()
        b.wait("S.ex.get(S.selected).state === 'done' && !document.querySelector('.row.sel .spin')")
        b.wait("document.querySelector('#replysec').textContent.includes('ünïcode')")

    def test_2_raw_and_no_secrets_in_dom(self):
        b = self.b
        self.send("tool_use_stream.sse")
        b.wait("S.ex.get(S.selected)?.stop_reason === 'tool_use'")
        b.key("r")
        b.wait("document.querySelectorAll('.evt').length >= 10")
        dom = b.js("document.documentElement.outerHTML")
        self.assertIn("[redacted]", dom)
        for m in SECRET_MARKERS:
            self.assertNotIn(m, dom)
        b.key("r")

    def test_3_errors_marked(self):
        b = self.b
        self.send("error_529.json", **{"x-status": "529"})
        b.wait("document.querySelector('.row.sel.err') && document.querySelector('.errbox')")
        self.assertIn("overloaded_error", b.js("document.querySelector('.errbox').textContent"))
        self.assertGreaterEqual(int(b.js("document.querySelector('#totals .err b').textContent")), 1)

    def test_4_view_stays_put_when_not_following(self):
        b = self.b
        for _ in range(25):
            self.send("text_stream.sse", **{"x-chunks": "1"})
        b.wait("S.order.length >= 25")
        b.js("setFollow(false); document.querySelector('#tl').scrollTop = 400; null")
        time.sleep(0.2)
        top_id = b.js("document.elementFromPoint(100, document.querySelector('#tl').getBoundingClientRect().top + 60).closest('.row').dataset.id")
        self.send("text_stream.sse", **{"x-chunks": "1"})
        self.send("text_stream.sse", **{"x-chunks": "1"})
        b.wait("document.querySelector('#pill').style.display === 'block'")
        self.assertIn("2 new", b.js("document.querySelector('#pill').textContent"))
        same = b.js("document.elementFromPoint(100, document.querySelector('#tl').getBoundingClientRect().top + 60).closest('.row').dataset.id")
        self.assertEqual(same, top_id)
        b.click("#pill")
        b.wait("document.querySelector('#tl').scrollTop === 0 && document.querySelector('#pill').style.display === 'none'")
        b.key("f")
        b.wait("S.follow && S.selected === S.order[0]")

    def test_5_pause_buffers(self):
        b = self.b
        b.key("p")
        n = b.js("S.order.length")
        self.send()
        b.wait("S.buffer.length > 0")
        self.assertEqual(b.js("S.order.length"), n)
        b.key("p")
        b.wait(f"S.order.length === {n + 1}")

    def test_6_reconnect_has_no_gaps(self):
        b = self.b
        b.js("es.close(); setConn('reconnecting'); null")
        self.send()
        self.send()
        n = b.js("S.order.length")
        b.js("connect(); null")
        b.wait(f"S.order.length === {n + 2}")
        b.wait("document.querySelector('#conn').className === 'live'")

    def test_7_narrow_layout(self):
        b = self.b
        b.resize(400, 800)
        try:
            b.wait("getComputedStyle(document.querySelector('#right')).display === 'none' || document.body.classList.contains('detail-open')")
            b.js("document.body.classList.remove('detail-open'); null")
            b.wait("document.querySelectorAll('.row').length > 10")
            self.assertFalse(b.js("document.documentElement.scrollWidth > window.innerWidth"))
            b.click(".row")
            b.wait("document.body.classList.contains('detail-open') && getComputedStyle(document.querySelector('#back')).display !== 'none'")
            self.assertFalse(b.js("document.documentElement.scrollWidth > window.innerWidth"))
            b.click("#back")
            b.wait("!document.body.classList.contains('detail-open')")
        finally:
            b.resize(1400, 900)

    def test_8_no_console_errors(self):
        self.assertEqual([c for c in self.b.console if "EXCEPTION" in c or "rror" in c], [])


@unittest.skipUnless(CHROME, "needs Chrome/Chromium")
class ManyRows(unittest.TestCase):
    def test_virtualised_timeline(self):
        import os
        import tempfile
        from store import Store, canonical
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "big.jsonl")
            with open(log, "w") as f:
                for i in range(1, 5001):
                    ex = {"id": i, "thread": None, "method": "POST", "path": "/v1/messages", "kind": "messages",
                          "t": {"start": "2026-11-03T10:00:00.000+00:00", "connected_ms": 1, "ttfb_ms": 5, "total_ms": 9},
                          "status": 200, "reason": "OK", "state": "done", "request": {"headers": [], "messages": []},
                          "response": {"headers": [], "size": 0}, "reply": {"stop_reason": "end_turn", "content": []},
                          "error": None, "badges": []}
                    f.write(json.dumps({"type": "exchange", "exchange": ex}) + "\n")
            with Proxy("http://127.0.0.1:1", extra=["--load", log, "--max-exchanges", "6000"], dash=True) as p, Browser() as b:
                b.goto(f"http://localhost:{p.dash_port}/")
                b.wait("S.order.length === 5000")
                self.assertLess(b.js("document.querySelectorAll('.row').length"), 60)
                t0 = time.time()
                for y in range(0, 250000, 5000):
                    b.js(f"document.querySelector('#tl').scrollTop = {y}; renderTimeline(); null")
                per = (time.time() - t0) / 50 * 1000
                self.assertLess(per, 50, f"{per:.1f} ms per scroll render")
                b.js("document.querySelector('#tl').scrollTop = 1e9; renderTimeline(); null")
                b.wait("document.querySelector('.row[data-id=\"1\"]')")


if __name__ == "__main__":
    unittest.main()
