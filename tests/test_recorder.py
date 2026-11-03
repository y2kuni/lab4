"""Phase 1: redaction, SSE parsing, store/dedup, JSONL log and load."""

import json
import os
import random
import stat
import tempfile
import time
import unittest

from recorder import Recorder, SSEParser, classify, redact_headers
from store import Store
from tests.harness import (SECRET_BEARER, SECRET_COOKIE, SECRET_KEY, SECRET_MARKERS, SECRET_TOKEN, Chain, FakeUpstream,
                           call, fixture)


def assert_no_secrets(tc, text: str):
    for m in SECRET_MARKERS:
        tc.assertNotIn(m, text)


class Redaction(unittest.TestCase):
    def test_redacts(self):
        h = [("Authorization", SECRET_BEARER), ("X-Api-Key", SECRET_KEY), ("Cookie", SECRET_COOKIE),
             ("x-session-token", SECRET_TOKEN), ("X-Client-Secret", "s3"), ("anthropic-beta", "b1"),
             ("Set-Cookie", "a=b")]
        out = redact_headers(h)
        self.assertEqual([n for n, _ in out], [n for n, _ in h])
        self.assertEqual(dict(out)["anthropic-beta"], "b1")
        for name, value in out:
            if name != "anthropic-beta":
                self.assertTrue(value.startswith("[redacted]"), name)
        assert_no_secrets(self, json.dumps(out))


def feed_all(data: bytes, sizes) -> SSEParser:
    p, i = SSEParser(), 0
    for n in sizes:
        p.feed(data[i:i + n])
        i += n
    p.feed(data[i:])
    p.close()
    return p


class SSE(unittest.TestCase):
    def check_invariant(self, name):
        data = fixture(name)
        whole = feed_all(data, [])
        bytewise = feed_all(data, [1] * len(data))
        rng = random.Random(7)
        for _ in range(20):
            sizes = [rng.randint(1, 60) for _ in range(len(data) // 10)]
            self.assertEqual(feed_all(data, sizes).reply, whole.reply)
        self.assertEqual(bytewise.reply, whole.reply)
        crlf = feed_all(data.replace(b"\n", b"\r\n"), [3] * len(data))
        self.assertEqual(crlf.reply, whole.reply)
        return whole

    def test_text(self):
        p = self.check_invariant("text_stream.sse")
        self.assertTrue(p.stopped)
        self.assertEqual(p.reply["content"][0]["text"], "Tests pass. Next I'll check the docs — ✓ ünïcode.")
        self.assertEqual(p.reply["stop_reason"], "end_turn")
        self.assertEqual(p.reply["usage"]["output_tokens"], 15)
        self.assertEqual(p.reply["usage"]["cache_read_input_tokens"], 41000)

    def test_tool_use(self):
        p = self.check_invariant("tool_use_stream.sse")
        tool = p.reply["content"][1]
        self.assertEqual(tool["name"], "Bash")
        self.assertEqual(tool["input"], {"command": "python3 -m unittest", "description": "Run tests"})
        self.assertNotIn("_partial", tool)
        self.assertEqual(p.reply["stop_reason"], "tool_use")

    def test_partial_tool_input_is_raw_while_streaming(self):
        data = fixture("tool_use_stream.sse")
        cut = data.index(b"ittest")
        p = SSEParser()
        notes = p.feed(data[:cut])
        self.assertIn("_partial", p.reply["content"][1])
        self.assertEqual(p.reply["content"][1]["input"], {})
        self.assertTrue(any(n["op"] == "input" for n in notes))

    def test_thinking(self):
        p = self.check_invariant("thinking_stream.sse")
        self.assertEqual(p.reply["content"][0]["thinking"], "The user wants a haiku. Keep it short.")
        self.assertTrue(p.reply["content"][0]["signature"])

    def test_error_event(self):
        p = self.check_invariant("sse_error.sse")
        self.assertEqual(p.error["type"], "overloaded_error")
        self.assertEqual(p.reply["content"][0]["text"], "Partial")

    def test_unknown_event_kept(self):
        p = self.check_invariant("unknown_event.sse")
        self.assertEqual(len(p.unknown), 1)
        self.assertEqual(p.unknown[0]["event"], "future_event")
        self.assertEqual(p.reply["content"][0]["text"], "ok")

    def test_truncated(self):
        p = feed_all(fixture("truncated.sse"), [5] * 300)
        self.assertFalse(p.stopped)

    def test_classify(self):
        m = {"messages": []}
        self.assertEqual(classify("/v1/messages?beta=true", m), "messages")
        self.assertEqual(classify("/v1/messages/count_tokens?beta=true", m), "count_tokens")
        self.assertEqual(classify("/responses", {"input": []}), "other")
        self.assertEqual(classify("/v1/messages", None), "other")


def record(store, rec, body, status=200, ctype="text/event-stream", resp=b"", state="done", err=None,
           path="/v1/messages"):
    live = rec.start("POST", path, [("authorization", SECRET_BEARER), ("content-type", "application/json")], body)
    rec.connected(live)
    rec.response(live, status, "OK", [("content-type", ctype), ("set-cookie", SECRET_COOKIE)])
    for i in range(0, len(resp), 50):
        rec.chunk(live, resp[i:i + 50], i == 0)
    rec.finish(live, state, err)
    rec.flush()
    return store.get(live.id)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.rec = Recorder(self.store, log=lambda *_: None)

    def test_messages_exchange(self):
        ex = record(self.store, self.rec, fixture("request_messages.json"), resp=fixture("tool_use_stream.sse"))
        self.assertEqual(ex["kind"], "messages")
        self.assertEqual(ex["state"], "done")
        self.assertEqual(ex["request"]["model"], "claude-opus-5-5")
        self.assertEqual(len(ex["request"]["system"]), 2)
        self.assertEqual(len(ex["request"]["tools"]), 2)
        self.assertEqual(ex["request"]["params"], {})
        self.assertEqual(ex["reply"]["content"][1]["input"]["command"], "python3 -m unittest")
        self.assertIsNone(ex["error"])
        for k in ("connected_ms", "ttfb_ms", "total_ms"):
            self.assertIsNotNone(ex["t"][k], k)
        tool = self.store.get_blob(ex["request"]["tools"][0])
        self.assertEqual(tool["data"]["name"], "Bash")
        raw = self.store.get_raw(ex["id"])
        self.assertEqual(raw["request_body"], fixture("request_messages.json").decode())
        self.assertEqual(raw["response_body"], fixture("tool_use_stream.sse").decode())
        assert_no_secrets(self, json.dumps([ex, raw]))

    def test_errors(self):
        ex = record(self.store, self.rec, b"{}", status=429, ctype="application/json", resp=fixture("error_429.json"))
        self.assertEqual(ex["error"]["type"], "upstream_http")
        self.assertIn("rate_limit_error", ex["error"]["message"])
        ex = record(self.store, self.rec, fixture("request_messages.json"), resp=fixture("sse_error.sse"))
        self.assertEqual(ex["error"]["type"], "sse")
        ex = record(self.store, self.rec, fixture("request_messages.json"), resp=fixture("truncated.sse"))
        self.assertEqual(ex["error"]["type"], "incomplete")
        ex = record(self.store, self.rec, fixture("request_messages.json"), resp=fixture("text_stream.sse")[:300],
                    state="aborted", err="client disconnected")
        self.assertEqual(ex["state"], "aborted")
        self.assertIn("aborted", ex["badges"])

    def test_unparseable_and_other(self):
        ex = record(self.store, self.rec, fixture("request_binary.bin"), resp=fixture("text_stream.sse"))
        self.assertEqual(ex["kind"], "other")
        self.assertIn("could not parse", ex["badges"])
        ex = record(self.store, self.rec, fixture("request_codex.json"), resp=fixture("codex.sse"), path="/responses")
        self.assertEqual(ex["kind"], "other")
        self.assertEqual(ex["request"]["json"]["model"], "gpt-x")
        self.assertEqual(self.store.get_raw(ex["id"])["response_body"], fixture("codex.sse").decode())

    def test_nonstream_and_count_tokens(self):
        body = json.loads(fixture("request_messages.json"))
        body["stream"] = False
        ex = record(self.store, self.rec, json.dumps(body).encode(), ctype="application/json",
                    resp=fixture("nonstream.json"))
        self.assertEqual(ex["reply"]["content"][0]["text"], "Fix proxy tests")
        ex = record(self.store, self.rec, json.dumps(body).encode(), ctype="application/json",
                    resp=fixture("count_tokens.json"), path="/v1/messages/count_tokens")
        self.assertEqual(ex["kind"], "count_tokens")
        self.assertEqual(ex["reply"]["count"]["input_tokens"], 4211)

    def test_dedup_is_linear(self):
        base = json.loads(fixture("request_messages.json"))
        msgs = list(base["messages"])
        sizes = []
        for i in range(100):
            msgs.append({"role": "assistant" if i % 2 == 0 else "user", "content": f"turn {i} " + "x" * 500})
            body = json.dumps(dict(base, messages=msgs)).encode()
            sizes.append(len(body))
            live = self.rec.start("POST", "/v1/messages", [], body)
            self.rec.finish(live)
        self.rec.flush()
        stored = sum(b["size"] for b in self.store.blobs.values())
        self.assertEqual(len(self.store.blobs), 2 + 2 + 101)
        self.assertLess(stored, sizes[-1] * 1.2)    # ~ one copy of the final conversation
        self.assertGreater(sum(sizes), stored * 40)  # vs. the O(n^2) wire total

    def test_eviction_releases_blobs(self):
        store = Store(max_exchanges=3)
        rec = Recorder(store, log=lambda *_: None)
        for i in range(6):
            body = json.dumps({"model": "m", "messages": [{"role": "user", "content": f"only {i}"}]}).encode()
            rec.finish(rec.start("POST", "/v1/messages", [], body))
        rec.flush()
        self.assertEqual(list(store.exchanges), [4, 5, 6])
        self.assertEqual(len(store.blobs), 3)

    def test_recorder_failure_is_swallowed(self):
        class Broken:
            def dispatch(self, *a):
                raise RuntimeError("boom")
        logs = []
        rec = Recorder(Broken(), log=logs.append)
        live = rec.start("POST", "/v1/messages", [], b"{}")
        rec.finish(live)
        rec.flush()
        self.assertTrue(logs and "boom" in logs[0])


class LogRoundTrip(unittest.TestCase):
    def test_proxy_log_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "traffic.jsonl")
            body = fixture("request_messages.json")
            with FakeUpstream() as up, Chain(up.url, extra=["--log", log]) as p:
                for name in ("text_stream.sse", "tool_use_stream.sse", "thinking_stream.sse"):
                    call(p.port, body, {"x-fixture": name})
                call(p.port, b"{}", {"x-fixture": "error_529.json", "x-status": "529"})
                time.sleep(0.3)
                out = p.output()
            self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
            with open(log, encoding="utf-8") as f:
                text = f.read()
            assert_no_secrets(self, text)
            assert_no_secrets(self, out)  # the dashboard prints no traffic
            self.assertIn("WARNING", out)
            lines = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(sum(1 for r in lines if r["type"] == "exchange"), 4)
            self.assertEqual(sum(1 for r in lines if r["type"] == "blob"), 5)  # deduped across requests
            s = Store()
            self.assertEqual(s.load(log), 4)
            ex = s.get(2)
            self.assertEqual(ex["reply"]["content"][1]["name"], "Bash")
            self.assertEqual(s.get(4)["error"]["type"], "upstream_http")
            raw = s.get_raw(1)
            self.assertTrue(raw["request_reconstructed"])
            self.assertEqual(json.loads(raw["request_body"]), json.loads(body))
            self.assertEqual(raw["response_body"], fixture("text_stream.sse").decode())


if __name__ == "__main__":
    unittest.main()
