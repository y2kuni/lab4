"""Golden forwarding test (PLAN 0.2): traffic must pass through unchanged.

proxy.py itself must stay byte-identical to the original. The tests run against
the real setup: client -> proxy.py -> dashboard relay -> fake upstream.
"""

import json
import os
import unittest

import subprocess

from tests.harness import ROOT, SECRET_BEARER, Chain, Dash, FakeUpstream, Proxy, call, fixture

SCRIPT = os.environ.get("PROXY_SCRIPT", str(ROOT / "proxy.py"))
STREAMS = ["text_stream.sse", "tool_use_stream.sse", "thinking_stream.sse", "sse_error.sse",
           "unknown_event.sse", "truncated.sse", "codex.sse", "nonstream.json", "count_tokens.json"]


class Forwarding(unittest.TestCase):
    MODE = "front"

    @classmethod
    def setUpClass(cls):
        cls.up = FakeUpstream().__enter__()
        cls.proxy = Chain(cls.up.url, script=SCRIPT, mode=cls.MODE).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.__exit__(None, None, None)
        cls.up.__exit__(None, None, None)

    def test_reply_bytes_identical(self):
        body = fixture("request_messages.json")
        for name in STREAMS:
            for chunks in (1, 7, 64):
                with self.subTest(fixture=name, chunks=chunks):
                    r = call(self.proxy.port, body, {"x-fixture": name, "x-chunks": str(chunks)})
                    self.assertEqual(r["status"], 200)
                    self.assertEqual(r["body"], fixture(name))

    def test_request_forwarded_intact(self):
        body = fixture("request_messages.json")
        call(self.proxy.port, body, {"x-fixture": "text_stream.sse", "accept-encoding": "gzip, br"},
             path="/v1/messages?beta=true")
        got = self.up.received[-1]
        self.assertEqual(got["path"], "/v1/messages?beta=true")
        self.assertEqual(got["body"], body)
        self.assertEqual(got["headers"].get("authorization"), SECRET_BEARER)
        self.assertEqual(got["headers"].get("anthropic-beta"), "oauth-2025-04-20")
        # The client's accept-encoding is dropped; http.client then sends "identity" itself.
        self.assertEqual(got["headers"].get("Accept-Encoding", "identity"), "identity")

    def test_binary_request_body(self):
        body = fixture("request_binary.bin")
        r = call(self.proxy.port, body, {"x-fixture": "text_stream.sse"})
        self.assertEqual(r["body"], fixture("text_stream.sse"))
        self.assertEqual(self.up.received[-1]["body"], body)

    def test_error_status_passthrough(self):
        r = call(self.proxy.port, b"{}", {"x-fixture": "error_429.json", "x-status": "429"})
        self.assertEqual(r["status"], 429)
        self.assertEqual(r["headers"].get("retry-after"), "7")
        self.assertEqual(r["body"], fixture("error_429.json"))
        r = call(self.proxy.port, b"{}", {"x-fixture": "error_529.json", "x-status": "529"})
        self.assertEqual(r["status"], 529)
        self.assertEqual(json.loads(r["body"])["error"]["type"], "overloaded_error")

    def test_get(self):
        r = call(self.proxy.port, method="GET", path="/v1/models", headers={"x-fixture": "count_tokens.json"})
        self.assertEqual(r["body"], fixture("count_tokens.json"))

    def test_upstream_cut_midstream(self):
        r = call(self.proxy.port, b"{}", {"x-fixture": "tool_use_stream.sse", "x-cut": "1", "x-chunks": "6"})
        self.assertEqual(r["status"], 200)
        self.assertTrue(fixture("tool_use_stream.sse").startswith(r["body"]))


class ForwardingBehind(Forwarding):
    MODE = "behind"


class Untouched(unittest.TestCase):
    def test_proxy_py_is_the_original(self):
        base = subprocess.run(["git", "show", "baseline:proxy.py"], cwd=ROOT, capture_output=True, check=True).stdout
        self.assertEqual((ROOT / "proxy.py").read_bytes(), base)


class Unreachable(unittest.TestCase):
    def test_502(self):
        from tests.harness import free_port
        with Chain(f"http://127.0.0.1:{free_port()}", script=SCRIPT) as p:
            r = call(p.port, b"{}")
            self.assertEqual(r["status"], 502)   # upstream down: the 502 reaches the client
        # the dashboard not running: proxy.py answers 502 by itself, exactly as before
        with Proxy(f"http://127.0.0.1:{free_port()}", SCRIPT) as p:
            self.assertEqual(call(p.port, b"{}")["status"], 502)


if __name__ == "__main__":
    unittest.main()
