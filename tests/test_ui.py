"""Browser tests (headless Chrome over CDP). Skipped when no Chrome is installed."""

import json
import threading
import time
import unittest

from tests.cdp import CHROME, Browser
from tests.harness import SECRET_MARKERS, Chain, Dash, FakeUpstream, call, fixture


@unittest.skipUnless(CHROME, "needs Chrome/Chromium")
class LiveUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = FakeUpstream().__enter__()
        cls.proxy = Chain(cls.up.url).__enter__()
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
        b.wait("document.querySelector('#pill').style.display === 'block' && document.querySelector('#pill').textContent.includes('2 new')")
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

    def test_7b_system_prompt_diff(self):
        b = self.b
        body = json.loads(fixture("request_messages.json"))
        body["system"][1]["text"] = "Be concise.\nline two\nline three"
        call(self.proxy.port, json.dumps(body).encode(), {"x-fixture": "text_stream.sse"})
        body["system"][1]["text"] = "Be concise.\nline 2 changed\nline three"
        body["max_tokens"] = 1000
        call(self.proxy.port, json.dumps(body).encode(), {"x-fixture": "text_stream.sse"})
        b.js("setFollow(true); S.tab = 'new'; null")
        b.wait("document.querySelector('#system-diff')", timeout=5)
        diff = b.js("document.querySelector('#system-diff').textContent")
        self.assertIn("- line two", diff)
        self.assertIn("+ line 2 changed", diff)
        self.assertIn("max_tokens", b.js("document.querySelector('.changes').textContent"))
        b.js("S.tab = 'system'; renderDetail(); null")
        b.wait("document.querySelector('.k-sys .warnchip')")

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
            with Dash(None, extra=["--load", log, "--max-exchanges", "6000"]) as p, Browser() as b:
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


@unittest.skipUnless(CHROME, "needs Chrome/Chromium")
class DemoUI(unittest.TestCase):
    """Phase 3: message kinds, harness detection, pairing, safe rendering. Uses the demo session."""

    @classmethod
    def setUpClass(cls):
        from tests.demo import play
        cls.up = FakeUpstream().__enter__()
        cls.proxy = Chain(cls.up.url).__enter__()
        cls.n = play(cls.proxy.port, 0)
        # a real prompt that merely mentions a tag must still count as the human's
        body = json.loads(fixture("request_messages.json"))
        body["messages"] = [{"role": "user", "content": "why does <system-reminder> appear in my logs?"}]
        call(cls.proxy.port, json.dumps(body).encode(), {"x-fixture": "text_stream.sse"})
        cls.b = Browser(1400, 1000).__enter__()
        cls.b.goto(f"http://localhost:{cls.proxy.dash_port}/")
        cls.b.js("window.__alerts = 0; window.alert = () => { window.__alerts++; }; null")
        cls.b.wait(f"S.order.length === {cls.n + 1}")

    @classmethod
    def tearDownClass(cls):
        cls.b.__exit__(None, None, None)
        cls.proxy.__exit__(None, None, None)
        cls.up.__exit__(None, None, None)

    def open(self, ex_id, tab="conversation"):
        b = self.b
        b.js(f"S.tab = '{tab}'; select({ex_id}, true); null")
        b.wait(f"document.querySelector('#detail').dataset.ex === '{ex_id}' && !document.querySelector('#tabbody > .muted')")
        if tab == "conversation":
            b.wait("document.querySelector('#replysec')")

    def texts(self, sel):
        return self.b.js(f"[...document.querySelectorAll({json.dumps(sel)})].map(e => e.textContent)")

    def test_harness_vs_you(self):
        self.open(3)
        self.b.js("document.querySelector('.earlier button')?.click(); null")
        self.b.wait("document.querySelector('.k-harness')")
        harness = self.b.js("document.querySelector('.k-harness .lbl').title")
        self.assertIn("system-reminder", harness)
        self.assertTrue(any("fix the failing test" in t for t in self.texts(".k-you")))
        self.open(self.n + 1)
        self.assertEqual(self.b.js("document.querySelectorAll('.k-harness').length"), 0)
        self.assertTrue(any("why does <system-reminder>" in t for t in self.texts(".k-you")))

    def test_tool_call_paired_with_result(self):            # acceptance 3
        b = self.b
        b.js("convShown.set(8, 0); null")
        self.open(8)
        calls = b.js("document.querySelectorAll('#tabbody .msg .k-call').length")
        self.assertGreater(calls, 0)
        self.assertEqual(b.js("document.querySelectorAll('#tabbody .msg .k-call + .pair .k-result, #tabbody .msg .k-call .pair .k-result').length"), calls)
        self.assertGreater(b.js("document.querySelectorAll('.ptr').length"), 0)   # result row points up to its call
        link = b.js("[...document.querySelectorAll('a.chip.link')].map(a => a.textContent)")
        self.assertIn("arrived in #7", link)
        b.js("[...document.querySelectorAll('a.chip.link')].find(a => a.textContent === 'arrived in #7').click(); null")
        b.wait("S.selected === 7")
        # a call in the reply, whose result came in the next request
        self.open(3)
        b.wait("document.querySelector('#replysec .k-call .k-result')")
        self.assertIn("arrived in #4", self.texts("#replysec a.chip.link"))
        # the last call has no result yet
        self.open(self.n)
        b.wait("document.querySelector('#replysec .awaiting')")

    def test_error_result_marked(self):
        self.open(12)
        self.b.wait("document.querySelector('.k-err')")
        self.assertIn("error", self.b.js("document.querySelector('.k-err .lbl').textContent"))

    def test_hostile_content_is_inert(self):                # C5
        b = self.b
        self.open(14)
        b.wait("document.querySelector('#replysec .k-model')")
        self.assertEqual(b.js("document.querySelectorAll('#detail img, #detail script, #detail iframe').length"), 0)
        self.assertEqual(b.js("[...document.querySelectorAll('#detail a')].filter(a => a.getAttribute('href') !== '#' && !/^https?:/.test(a.href)).length"), 0)
        self.assertIn("<script>alert(1)</script>", b.js("document.querySelector('#replysec').textContent"))
        self.assertEqual(b.js("window.__alerts"), 0)

    def test_markdown_and_size(self):
        b = self.b
        self.open(8)
        b.wait("document.querySelector('#replysec .md')")
        self.assertEqual(b.js("document.querySelector('#replysec .md strong').textContent"), "Cause:")
        self.assertIn("return a + b", b.js("document.querySelector('#replysec .md pre.code').textContent"))
        self.assertEqual(b.js("document.querySelectorAll('#replysec .md li').length"), 2)
        self.assertIn("chars", b.js("document.querySelector('#replysec .sz').textContent"))
        # long tool output collapses with a "show all" button
        self.open(4)
        b.js("document.querySelector('.earlier button')?.click(); null")
        b.wait("[...document.querySelectorAll('button.more')].some(x => /show all \\(\\d+ lines\\)/.test(x.textContent))")

    def test_system_and_tools_tabs(self):                   # acceptance 4 (read in full)
        b = self.b
        self.open(3, "system")
        b.wait("document.querySelectorAll('.k-sys').length === 3")
        b.js("[...document.querySelectorAll('.k-sys button.more')].forEach(x => x.click()); null")
        self.assertIn("Rule 60", b.js("document.querySelector('#tabbody').textContent"))
        self.open(3, "tools")
        b.wait("document.querySelectorAll('details.tool').length === 7")
        b.js("(q => { q.value = 'ripgrep'; q.dispatchEvent(new Event('input')); })(document.querySelector('input.search')); null")
        b.wait("document.querySelectorAll('details.tool').length === 1")
        b.js("(q => { q.value = ''; q.dispatchEvent(new Event('input')); })(document.querySelector('input.search')); null")

    def test_other_kinds(self):
        b = self.b
        self.open(2, "params")
        self.assertIn("count_tokens", b.js("document.querySelector('#tabbody').textContent"))
        self.open(5)
        b.wait("document.querySelector('.errbox')")
        self.assertIn("overloaded_error", b.js("document.querySelector('.errbox').textContent"))

    def test_rows_tell_prompts_from_tool_loops(self):    # acceptance 2
        b = self.b
        b.js("S.tab = 'new'; null")
        rows = b.js("""S.order.map(id => { const s = S.ex.get(id); return [id, s.trigger && s.trigger.kind]; })""")
        kinds = dict((i, k) for i, k in rows)
        self.assertEqual(kinds[3], "you")
        self.assertEqual(kinds[4], "result")
        self.assertEqual(kinds[6], "retry")
        b.js("document.querySelector('#tl').scrollTop = 0; renderTimeline(); null")
        you = b.js("[...document.querySelectorAll('.row')].filter(r => r.querySelector('.t-you')).map(r => +r.dataset.id)")
        self.assertTrue({3, 9, 15}.issubset(set(you)), you)
        self.assertFalse({4, 5, 7, 8}.intersection(you))
        self.assertIn("main", b.js("document.querySelector('.row[data-id=\"3\"] .chip.th').textContent"))
        self.assertIn("subagent 1", b.js("document.querySelector('.row[data-id=\"10\"] .chip.th').textContent"))

    def test_whats_new(self):
        b = self.b
        self.open(17, "new")
        b.wait("document.querySelector('.changes')")
        chips = b.js("document.querySelector('.changes').textContent")
        self.assertIn("history rewritten", chips)
        self.assertIn("cache hit", chips)
        self.open(15, "new")
        b.wait("document.querySelector('#tools-diff')")
        self.assertIn("mcp__docs__search", b.js("document.querySelector('#tools-diff').textContent"))
        self.open(15, "tools")
        b.wait("document.querySelector('.chip.warnchip')")
        self.assertIn("new since #14", b.js("document.querySelector('.chip.warnchip').textContent"))
        self.open(4, "new")
        b.wait("document.querySelector('.prevreply') && document.querySelector('.callref')")
        self.assertIn("python3 -m pytest", b.js("document.querySelector('.callref').textContent"))

    def test_thread_rename(self):
        b = self.b
        b.js("window.prompt = () => 'my main loop'; null")
        b.js("document.querySelector('#tl').scrollTop = 0; renderTimeline(); null")
        b.js("document.querySelector('.row .chip.th0').dispatchEvent(new MouseEvent('dblclick', {bubbles: true})); null")
        b.wait("document.querySelector('.row .chip.th0').textContent === 'my main loop'")
        b.js("window.prompt = () => ''; document.querySelector('.row .chip.th0').dispatchEvent(new MouseEvent('dblclick', {bubbles: true})); null")
        b.wait("document.querySelector('.row .chip.th0').textContent === 'main'")

    def test_zz_no_console_errors(self):
        self.assertEqual([c for c in self.b.console if "EXCEPTION" in c], [])
