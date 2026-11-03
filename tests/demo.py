#!/usr/bin/env python3
"""demo.py: play a realistic Claude Code session through the proxy, against a fake API.

    python3 tests/demo.py                 # dashboard on http://localhost:8042
    python3 tests/demo.py --fast          # no pauses between requests
    python3 tests/demo.py --log demo.jsonl

It starts a fake upstream and proxy.py, then sends what Claude Code would send:
a main agent loop with tool calls and results, a subagent, side calls (title,
count_tokens), an overloaded error and a retry, a failing tool, a compacted
history, and some hostile content (HTML/script in tool output). Nothing leaves
this machine. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.harness import ROOT, FakeUpstream, call, free_port  # noqa: E402

LONG_RULES = "\n".join(f"- Rule {i}: keep changes minimal and explain them briefly." for i in range(1, 61))
SYSTEM_MAIN = [
    {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.0; cc_entrypoint=cli"},
    {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "You are an interactive CLI tool that helps users with software engineering tasks.\n\n# Rules\n"
     + LONG_RULES + "\n\n# Environment\nWorking directory: /home/dev/calc\nPlatform: linux", "cache_control": {"type": "ephemeral"}},
]
SYSTEM_SUB = [SYSTEM_MAIN[0], {"type": "text", "text": "You are a subagent for Claude Code. Search the codebase and report back concisely."}]


def tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False,
                             "$schema": "http://json-schema.org/draft-07/schema#"}}


S = {"type": "string"}
TOOLS = [
    tool("Bash", "Executes a given bash command and returns its output.\n\n" + "Usage notes:\n" + "\n".join(f"- note {i}" for i in range(20)),
         {"command": dict(S, description="The command to execute"), "description": S, "timeout": {"type": "number"}}, ["command"]),
    tool("Read", "Reads a file from the local filesystem.", {"file_path": S, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["file_path"]),
    tool("Edit", "Performs exact string replacements in files.", {"file_path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}}, ["file_path", "old_string", "new_string"]),
    tool("Write", "Writes a file to the local filesystem.", {"file_path": S, "content": S}, ["file_path", "content"]),
    tool("Grep", "A powerful search tool built on ripgrep.", {"pattern": S, "path": S, "glob": S}, ["pattern"]),
    tool("Glob", "Fast file pattern matching tool.", {"pattern": S, "path": S}, ["pattern"]),
    tool("Task", "Launch a new agent to handle complex, multi-step tasks autonomously.", {"description": S, "prompt": S, "subagent_type": S}, ["description", "prompt"]),
]
TOOLS_SUB = [t for t in TOOLS if t["name"] in ("Read", "Grep", "Glob", "Bash")]

REMINDER = "<system-reminder>\nAs you answer the user's questions, you can use the following context:\n# claudeMd\nCodebase and user instructions are shown below.\n- Use pytest for tests.\n</system-reminder>"
CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
CAT_N = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(CALC.splitlines(), 1))
PYTEST_FAIL = "\n".join(["============================= test session starts ==============================",
                         "collected 4 items", "", "test_calc.py .F..                                                       [100%]", "",
                         "=================================== FAILURES ===================================",
                         "___________________________________ test_add ___________________________________", "",
                         "    def test_add():", ">       assert add(2, 3) == 5", "E       assert -1 == 5",
                         "E        +  where -1 = add(2, 3)", "", "test_calc.py:5: AssertionError"]
                        + [f"  context line {i}" for i in range(20)]
                        + ["=========================== short test summary info ============================",
                           "FAILED test_calc.py::test_add - assert -1 == 5", "========================= 1 failed, 3 passed in 0.03s ========================="])
DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
HOSTILE = '<img src=x onerror="alert(\'xss\')"><script>alert(1)</script> [click](javascript:alert(2)) </pre></div>'


def u_text(*texts):
    return {"role": "user", "content": [{"type": "text", "text": t} for t in texts]}


def a_msg(content):
    return {"role": "assistant", "content": content}


def tr(tid, text, is_error=False):
    b = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if is_error:
        b["is_error"] = True
    return b


class Session:
    def __init__(self, port, pause):
        self.port, self.pause = port, pause
        self.n = 0

    def send(self, messages, reply, *, system=SYSTEM_MAIN, tools=TOOLS, model="claude-opus-5-5", stop="end_turn",
             usage=None, status=None, fixture=None, path="/v1/messages?beta=true", stream=True, extra=None, gap=12):
        self.n += 1
        body = {"model": model, "max_tokens": 32000, "messages": messages, "system": system, "stream": stream,
                "metadata": {"user_id": "user_demo_session"}}
        if tools:
            body["tools"] = tools
        if model.startswith("claude-opus"):
            body["thinking"] = {"type": "enabled", "budget_tokens": 8000}
            body["temperature"] = 1
        body.update(extra or {})
        hdr = {"x-chunks": "30", "x-gap-ms": str(gap), "x-delay-ms": "250" if self.pause else "0",
               "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14",
               "user-agent": "claude-cli/2.1.0 (external, cli)"}
        if fixture:
            hdr["x-fixture"] = fixture
        else:
            spec = {"content": reply, "stop_reason": stop, "usage": usage or self.usage(messages), "model": model,
                    "msg_id": f"msg_demo{self.n:03d}"}
            hdr["x-sse-json"] = base64.b64encode(json.dumps(spec).encode()).decode()
        if status:
            hdr["x-status"] = str(status)
        call(self.port, json.dumps(body).encode(), hdr, path=path)
        if self.pause:
            time.sleep(self.pause)
        return reply

    @staticmethod
    def usage(messages):
        size = len(json.dumps(messages)) // 4
        return {"input_tokens": 3 + size % 50, "cache_creation_input_tokens": 300 + size % 900,
                "cache_read_input_tokens": 9000 + size}


def play(port, pause):
    s = Session(port, pause)
    m = [u_text(REMINDER, "fix the failing test in calc.py")]
    # side call: title for the session (no tools, small model)
    s.send([u_text("Summarize this coding conversation in under 50 characters.\nUser: fix the failing test in calc.py")],
           [{"type": "text", "text": "Fix failing calc test"}], system=[{"type": "text", "text": "Generate a short title."}],
           tools=None, model="claude-haiku-5-5", stream=False, fixture="nonstream.json")
    s.send(m, None, path="/v1/messages/count_tokens?beta=true", fixture="count_tokens.json", stream=False)
    r = s.send(m, [{"type": "thinking", "thinking": "Run the tests first to see the failure."},
                   {"type": "text", "text": "I'll start by running the tests."},
                   {"type": "tool_use", "id": "toolu_01run", "name": "Bash", "input": {"command": "python3 -m pytest -q", "description": "Run tests"}}],
               stop="tool_use")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_01run", PYTEST_FAIL)]}]
    r = s.send(m, [{"type": "tool_use", "id": "toolu_02read", "name": "Read", "input": {"file_path": "/home/dev/calc/calc.py"}}], stop="tool_use")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_02read", CAT_N)]}]
    # overloaded, then the retry with the same messages
    s.send(m, None, fixture="error_529.json", status=529)
    r = s.send(m, [{"type": "text", "text": "`add` subtracts instead of adding. Fixing it:"},
                   {"type": "tool_use", "id": "toolu_03edit", "name": "Edit", "input": {"file_path": "/home/dev/calc/calc.py", "old_string": "return a - b", "new_string": "return a + b"}}],
               stop="tool_use")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_03edit", "The file /home/dev/calc/calc.py has been updated.\n" + DIFF)]}]
    r = s.send(m, [{"type": "tool_use", "id": "toolu_04run", "name": "Bash", "input": {"command": "python3 -m pytest -q", "description": "Run tests again"}}], stop="tool_use")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_04run", "....                                                                     [100%]\n4 passed in 0.02s")]}]
    r = s.send(m, [{"type": "text", "text": "Tests pass now.\n\n**Cause:** `add` used `-`.\n\n```python\ndef add(a, b):\n    return a + b\n```\n\n- 4 tests pass\n- no other files changed"}])
    m += [a_msg(r), u_text(REMINDER, "now find every caller of add() and check it handles floats")]
    # main agent delegates to a subagent
    r = s.send(m, [{"type": "tool_use", "id": "toolu_05task", "name": "Task", "input": {"description": "Find add() callers", "prompt": "Find every caller of add() in /home/dev/calc and report if any pass floats.", "subagent_type": "Explore"}}], stop="tool_use")
    sub = [u_text("Find every caller of add() in /home/dev/calc and report if any pass floats.")]
    sr = s.send(sub, [{"type": "tool_use", "id": "toolu_s1grep", "name": "Grep", "input": {"pattern": "add\\(", "path": "/home/dev/calc"}}], system=SYSTEM_SUB, tools=TOOLS_SUB, model="claude-sonnet-5-5", stop="tool_use")
    sub += [a_msg(sr), {"role": "user", "content": [tr("toolu_s1grep", "calc.py:1:def add(a, b):\nreport.py:12:    total = add(total, 0.5)\ntest_calc.py:5:    assert add(2, 3) == 5")]}]
    sr = s.send(sub, [{"type": "tool_use", "id": "toolu_s2read", "name": "Read", "input": {"file_path": "/home/dev/calc/missing.py"}}], system=SYSTEM_SUB, tools=TOOLS_SUB, model="claude-sonnet-5-5", stop="tool_use")
    sub += [a_msg(sr), {"role": "user", "content": [tr("toolu_s2read", "File does not exist: /home/dev/calc/missing.py", is_error=True)]}]
    s.send(sub, [{"type": "text", "text": "One caller passes a float: `report.py:12` calls `add(total, 0.5)`. That works with the fix."}], system=SYSTEM_SUB, tools=TOOLS_SUB, model="claude-sonnet-5-5")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_05task", [{"type": "text", "text": "One caller passes a float: report.py:12 calls add(total, 0.5). That works with the fix."}])]}]
    r = s.send(m, [{"type": "tool_use", "id": "toolu_06web", "name": "Bash", "input": {"command": "curl -s http://localhost:8000/report", "description": "Fetch report page"}}], stop="tool_use")
    m += [a_msg(r), {"role": "user", "content": [tr("toolu_06web", HOSTILE)]}]
    r = s.send(m, [{"type": "text", "text": "The report page renders fine. Everything checks out. " + HOSTILE}])
    m += [a_msg(r)]
    # a new tool appears mid-session (an MCP server connected)
    tools2 = TOOLS + [tool("mcp__docs__search", "Search the project docs.", {"query": S}, ["query"])]
    m2 = m + [u_text(REMINDER, "add a float test")]
    r = s.send(m2, [{"type": "tool_use", "id": "toolu_07write", "name": "Write", "input": {"file_path": "/home/dev/calc/test_float.py", "content": "from calc import add\n\n\ndef test_float():\n    assert add(0.5, 0.25) == 0.75\n"}}], tools=tools2, stop="tool_use")
    m2 += [a_msg(r), {"role": "user", "content": [tr("toolu_07write", "File created successfully at: /home/dev/calc/test_float.py")]}]
    s.send(m2, [{"type": "text", "text": "Added `test_float.py`."}], tools=tools2)
    # compaction: the history is replaced by a summary
    compacted = [u_text("This session is being continued from a previous conversation. Summary:\n1. Fixed add() in calc.py.\n2. Checked callers.\n3. Added a float test."),
                 u_text(REMINDER, "great, now commit it")]
    r = s.send(compacted, [{"type": "tool_use", "id": "toolu_08git", "name": "Bash", "input": {"command": "git commit -am 'Fix add()'", "description": "Commit"}}], tools=tools2, stop="tool_use",
               usage={"input_tokens": 400, "cache_creation_input_tokens": 6000, "cache_read_input_tokens": 0})
    return s.n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dash-port", type=int, default=8042)
    ap.add_argument("--port", type=int, default=0, help="proxy port (default: a free one)")
    ap.add_argument("--fast", action="store_true", help="no pauses")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between requests (default 1.5)")
    ap.add_argument("--log", help="pass --log to the proxy")
    ap.add_argument("--once", action="store_true", help="exit after playing the session")
    args = ap.parse_args()
    port = args.port or free_port()
    with FakeUpstream() as up:
        cmd = [sys.executable, str(ROOT / "proxy.py"), "--port", str(port), "--upstream", up.url, "--no-print",
               "--dash-port", str(args.dash_port)] + (["--log", args.log] if args.log else [])
        proc = subprocess.Popen(cmd, cwd=str(ROOT))
        try:
            time.sleep(0.8)
            if proc.poll() is not None:
                sys.exit("proxy failed to start")
            print(f"demo: open http://localhost:{args.dash_port}  (playing the session…)", flush=True)
            n = play(port, 0 if args.fast else args.pause)
            print(f"demo: sent {n} requests.", "Exiting." if args.once else "Ctrl-C to stop.", flush=True)
            if not args.once:
                proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            time.sleep(0.3)
            proc.terminate()
            proc.wait(5)


if __name__ == "__main__":
    main()
