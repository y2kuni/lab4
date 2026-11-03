"""Phase 4: thread assignment, new_from, history rewrite, labels, trigger/outcome."""

import json
import unittest

from dashboard import DEFAULT_HARNESS_RULES
from recorder import Recorder
from store import Store
from tests.harness import FakeUpstream, Proxy
from threads import Threads


class Sim:
    def __init__(self):
        self.store = Store()
        self.threads = Threads(self.store, DEFAULT_HARNESS_RULES)
        self.rec = Recorder(self.store, log=lambda *_: None)

    def send(self, messages, system="main", tools=("Bash", "Read"), path="/v1/messages", reply=None):
        body = {"model": "m", "system": [{"type": "text", "text": system}], "messages": messages,
                "tools": [{"name": t, "input_schema": {}} for t in tools]}
        live = self.rec.start("POST", path, [], json.dumps(body).encode())
        self.rec.response(live, 200, "OK", [("content-type", "application/json")])
        if reply:
            self.rec.chunk(live, json.dumps({"type": "message", "role": "assistant", "content": reply,
                                             "stop_reason": "tool_use"}).encode(), True)
        self.rec.finish(live)
        self.rec.flush()
        return self.store.get(live.id)


def u(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def a(*names):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": f"id_{n}", "name": n, "input": {}} for n in names]}


def r(name, err=False):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"id_{name}", "content": "ok", "is_error": err}]}


REM = "<system-reminder>\nctx\n</system-reminder>"


class ThreadTests(unittest.TestCase):
    def test_interleaved_threads(self):
        s = Sim()
        m = [{"role": "user", "content": [{"type": "text", "text": REM}, {"type": "text", "text": "fix the bug"}]}]
        e1 = s.send(m, reply=[{"type": "tool_use", "id": "id_Bash", "name": "Bash", "input": {}}])
        side = s.send([u("title for: fix the bug")], system="titles", tools=())
        m += [a("Bash"), r("Bash")]
        e2 = s.send(m)
        sub1 = s.send([u("search for x")], system="sub", tools=("Grep",))
        m += [a("Read", "Read"), r("Read", err=True)]
        e3 = s.send(m)
        sub2 = s.send([u("search for x"), a("Grep"), r("Grep")], system="sub", tools=("Grep",))
        ct = s.send(m, path="/v1/messages/count_tokens")
        e4 = s.send(m)  # retry with the same messages
        self.assertEqual(len({e1["thread"], e2["thread"], e3["thread"], e4["thread"]}), 1)
        self.assertEqual(sub1["thread"], sub2["thread"])
        self.assertNotEqual(sub1["thread"], e1["thread"])
        self.assertNotIn(side["thread"], (e1["thread"], sub1["thread"]))
        self.assertEqual([e["request"]["new_from"] for e in (e1, e2, e3, e4)], [0, 1, 3, 5])
        self.assertEqual(sub2["request"]["new_from"], 1)
        self.assertEqual(e2["prev"], e1["id"])
        self.assertIn("retry", e4["badges"])
        labels = s.threads.labels
        self.assertEqual(labels[e1["thread"]]["label"], "main")
        self.assertEqual(labels[sub1["thread"]]["label"], "subagent 1")
        self.assertEqual(labels[side["thread"]]["label"], "side call")
        self.assertEqual(labels[ct["thread"]]["label"], "side call")
        # triggers and outcomes
        self.assertEqual(e1["extra"]["trigger"], {"kind": "you", "text": "fix the bug"})
        self.assertEqual(e1["extra"]["outcome"]["calls"], [{"name": "Bash", "n": 1}])
        self.assertEqual(e2["extra"]["trigger"], {"kind": "result", "tools": [{"name": "Bash", "error": False}]})
        self.assertEqual(e3["extra"]["trigger"]["tools"], [{"name": "Read", "error": True}])
        self.assertEqual(e4["extra"]["trigger"]["kind"], "retry")

    def test_compaction_and_rewind(self):
        s = Sim()
        m = [u("one"), a("Bash"), r("Bash"), a("Read"), r("Read")]
        e1 = s.send(m[:1])
        e2 = s.send(m)
        c = s.send([u("This session is being continued from a previous conversation. Summary: ..."), u("go on")])
        self.assertEqual(c["thread"], e1["thread"])
        self.assertIn("history rewritten", c["badges"])
        self.assertEqual(c["request"]["rewritten"], {"common": 0, "removed": 5, "added": 2, "prev_len": 5})
        self.assertEqual(c["prev"], e2["id"])
        # rewind: same first message, different continuation
        s2 = Sim()
        x1 = s2.send([u("one"), a("Bash"), r("Bash"), u("two")])
        x2 = s2.send([u("one"), a("Bash"), r("Bash"), u("TWO edited")])
        self.assertEqual(x1["thread"], x2["thread"])
        self.assertEqual(x2["request"]["rewritten"]["common"], 3)
        self.assertEqual(x2["request"]["new_from"], 3)

    def test_system_change_keeps_thread(self):
        s = Sim()
        e1 = s.send([u("one")], system="v1")
        e2 = s.send([u("one"), a("Bash"), r("Bash")], system="v2 (date changed)")
        self.assertEqual(e1["thread"], e2["thread"])
        self.assertEqual(e2["prev"], e1["id"])
        self.assertEqual(e2["request"]["new_from"], 1)
        # a different conversation with the new system prompt still starts its own thread
        e3 = s.send([u("something else")], system="v2 (date changed)")
        self.assertNotEqual(e3["thread"], e1["thread"])

    def test_parallel_subagents_stay_separate(self):
        s = Sim()
        a1 = s.send([u("search A")], system="sub", tools=("Grep",))
        b1 = s.send([u("search B")], system="sub", tools=("Grep",))
        a2 = s.send([u("search A"), a("Grep"), r("Grep")], system="sub", tools=("Grep",))
        self.assertNotEqual(a1["thread"], b1["thread"])
        self.assertEqual(a1["thread"], a2["thread"])

    def test_harness_only_trigger_and_prompt_mentioning_tag(self):
        s = Sim()
        e = s.send([u(REM)])
        self.assertEqual(e["extra"]["trigger"]["kind"], "harness")
        e = s.send([u("why is <system-reminder> in my logs?")], system="other")
        self.assertEqual(e["extra"]["trigger"]["kind"], "you")


class DemoThreads(unittest.TestCase):
    def test_demo_session_and_load(self):
        import os
        import tempfile
        import time

        from tests.demo import play
        from tests.harness import call  # noqa: F401
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "t.jsonl")
            with FakeUpstream() as up, Proxy(up.url, extra=["--no-print", "--log", log], dash=True) as p:
                play(p.port, 0)
                time.sleep(0.3)
                from tests.test_dashboard import jget
                live = jget(p.dash_port, "/api/exchanges")
            labels = {k: v["label"] for k, v in live["threads"].items()}
            by_id = {e["id"]: e for e in live["exchanges"]}
            self.assertEqual(labels[by_id[3]["thread"]], "main")
            self.assertEqual(labels[by_id[10]["thread"]], "subagent 1")
            self.assertEqual(labels[by_id[1]["thread"]], "side call")
            self.assertEqual(labels[by_id[2]["thread"]], "side call")
            self.assertIn("history rewritten", by_id[17]["badges"])
            self.assertIn("retry", by_id[6]["badges"])
            self.assertEqual(by_id[4]["trigger"]["tools"][0]["name"], "Bash")
            with Proxy("http://127.0.0.1:1", extra=["--load", log], dash=True) as lp:
                loaded = jget(lp.dash_port, "/api/exchanges")
            self.assertEqual(loaded["threads"], live["threads"])
            self.assertEqual([e["trigger"] for e in loaded["exchanges"]], [e["trigger"] for e in live["exchanges"]])


if __name__ == "__main__":
    unittest.main()
