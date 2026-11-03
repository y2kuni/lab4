"""threads.py: split the traffic into conversations, and say what is new in each request (SPEC §4.3, §5.3).

Claude Code sends several conversations through one proxy: the main agent loop,
subagents, and small side calls. Each exchange is given a thread:

1. It continues a thread when its messages start with that thread's previous
   messages. A thread with the same system prompt is preferred; a changed
   system prompt is allowed (Claude Code edits it between turns) and is shown
   as a diff. `new_from` is the index of the first message not seen before.
2. It is a *history rewrite* of a thread (compaction, rewind) when the system
   prompt and tool set are the same and either its first message is the
   thread's first message, or it opens with a compaction summary. `new_from`
   is the common prefix, and the changed range is recorded.
3. Otherwise it starts a new thread.

Labels are recomputed as threads grow: *main* is the thread with tools that has
the most exchanges; other threads with tools are *subagent N*; threads without
tools, and token counts, are *side call*.

Everything here runs on the recorder's worker thread, under the store lock.
"""

from __future__ import annotations

import re

COMPACTION = re.compile(r"^\s*This session is being continued from a previous conversation")
SNIPPET = 90


class Thread:
    __slots__ = ("id", "system", "tools", "first", "last", "last_ex", "n", "created", "has_tools", "max_len")

    def __init__(self, tid: str, system: tuple, tools: frozenset, msgs: list, ex_id: int):
        self.id, self.system, self.tools = tid, system, tools
        self.first = msgs[0] if msgs else None
        self.last, self.last_ex = list(msgs), ex_id
        self.n, self.created = 0, ex_id
        self.has_tools, self.max_len = bool(tools), len(msgs)


class Threads:
    def __init__(self, store, harness_rules: list[dict] | None = None):
        self.store = store
        self.threads: dict[str, Thread] = {}
        self.count = 0
        self.rules = [(r["name"], re.compile(r["pattern"])) for r in (harness_rules or [])]
        self.labels: dict[str, dict] = {}
        store.threads = self
        store.listeners.append(self.on_start)
        store.finish_listeners.append(self.on_finish)

    # --- assignment ----------------------------------------------------------

    def on_start(self, store, ex):
        req = ex["request"]
        if ex["kind"] == "count_tokens":
            self._ensure_side("tc")
            ex["thread"] = "tc"
            req["new_from"] = 0
            ex["extra"] = {"trigger": {"kind": "count", "text": "count tokens"}}
            self._relabel()
            return
        if ex["kind"] != "messages":
            return
        msgs, system, tools = req["messages"], tuple(req["system"]), frozenset(req["tools"])
        best, best_rank = None, None
        for t in self.threads.values():
            if not t.last or len(msgs) < len(t.last) or msgs[:len(t.last)] != t.last:
                continue
            if t.system != system and t.id == "tc":
                continue
            rank = (t.system == system, len(t.last), t.last_ex)
            if best is None or rank > best_rank:
                best, best_rank = t, rank
        rewrite = None
        if best is None and msgs:
            first_text = _first_text(self._msg(msgs[0]))
            for t in sorted(self.threads.values(), key=lambda t: -t.last_ex):
                if t.system == system and t.tools == tools and t.has_tools and \
                        (msgs[0] == t.first or COMPACTION.search(first_text or "")):
                    common = _common_prefix(msgs, t.last)
                    rewrite = {"thread": t, "common": common, "removed": len(t.last) - common,
                               "added": len(msgs) - common, "prev_len": len(t.last)}
                    break
        if best is not None:
            t, new_from = best, len(best.last)
            ex["prev"] = best.last_ex
        elif rewrite is not None:
            t, new_from = rewrite["thread"], rewrite["common"]
            ex["prev"] = t.last_ex
            req["rewritten"] = {k: v for k, v in rewrite.items() if k != "thread"}
            ex["badges"].append("history rewritten")
            t.first = msgs[0]
        else:
            self.count += 1
            t = Thread(f"t{self.count}", system, tools, msgs, ex["id"])
            self.threads[t.id] = t
            new_from = 0
        t.last, t.last_ex, t.tools, t.system = list(msgs), ex["id"], tools, system
        t.n += 1
        t.has_tools = t.has_tools or bool(tools)
        t.max_len = max(t.max_len, len(msgs))
        ex["thread"] = t.id
        req["new_from"] = new_from
        if new_from == len(msgs) and ex.get("prev") is not None:
            ex["badges"].append("retry")
        ex["extra"] = {"trigger": self.trigger(msgs, new_from)}
        self._relabel()

    def on_finish(self, store, ex):
        if ex["kind"] in ("messages", "count_tokens"):
            ex.setdefault("extra", {})["outcome"] = outcome(ex)

    def _ensure_side(self, tid):
        if tid not in self.threads:
            t = Thread(tid, (), frozenset(), [], 0)
            self.threads[tid] = t
        self.threads[tid].n += 1

    def _msg(self, h):
        return (self.store.blobs.get(h) or {}).get("data")

    # --- labels ----------------------------------------------------------------

    def _relabel(self):
        with_tools = [t for t in self.threads.values() if t.has_tools and t.id != "tc"]
        main = max(with_tools, key=lambda t: (t.n, len(t.tools), -t.created), default=None)
        labels, sub = {}, 0
        for t in sorted(self.threads.values(), key=lambda t: t.created):
            if t is main:
                label, role = "main", "main"
            elif t in with_tools:
                sub += 1
                label, role = f"subagent {sub}", "subagent"
            else:
                label, role = "side call", "side"
            labels[t.id] = {"label": label, "role": role, "n": t.n}
        if labels != self.labels:
            self.labels = labels
            self.store.emit("threads", {"threads": labels})

    def rebuild(self):
        """After --load: rebuild thread state from the stored exchanges."""
        for ex in self.store.exchanges.values():
            tid = ex.get("thread")
            if not tid:
                continue
            req = ex["request"]
            if tid not in self.threads:
                self.threads[tid] = Thread(tid, tuple(req.get("system", [])), frozenset(req.get("tools", [])),
                                           req.get("messages", []), ex["id"])
                if tid.startswith("t") and tid[1:].isdigit():
                    self.count = max(self.count, int(tid[1:]))
            t = self.threads[tid]
            t.n += 1
            t.last, t.last_ex = list(req.get("messages", [])), ex["id"]
            t.has_tools = t.has_tools or bool(req.get("tools"))
            t.max_len = max(t.max_len, len(req.get("messages", [])))
        self._relabel()

    # --- trigger: what made this request happen -----------------------------

    def harness_rule(self, text: str):
        for name, rx in self.rules:
            if rx.search(text):
                return name
        return None

    def trigger(self, msgs: list, new_from: int) -> dict:
        new = [self._msg(h) for h in msgs[new_from:]]
        if not new:
            return {"kind": "retry", "text": "same messages again"}
        # the last new user message is what triggered the request
        user = next((m for m in reversed(new) if isinstance(m, dict) and m.get("role") == "user"), None)
        if user is None:
            return {"kind": "other", "text": f"{len(new)} new message(s)"}
        blocks = user["content"] if isinstance(user.get("content"), list) else [{"type": "text", "text": user.get("content") or ""}]
        results, prompt, harness = [], None, False
        names = self._call_names(msgs, new_from)
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                results.append({"name": names.get(b.get("tool_use_id"), "?"), "error": bool(b.get("is_error"))})
            elif b.get("type") == "text":
                text = b.get("text") or ""
                if self.harness_rule(text):
                    harness = True
                elif text.strip():
                    prompt = text
            elif b.get("type") == "image" and prompt is None:
                prompt = "[image]"
        if prompt is not None:
            return {"kind": "you", "text": _snip(prompt)}
        if results:
            return {"kind": "result", "tools": results}
        return {"kind": "harness" if harness else "other", "text": "harness context" if harness else ""}

    def _call_names(self, msgs, new_from) -> dict:
        names = {}
        for h in msgs[max(0, new_from - 1):]:
            m = self._msg(h)
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type", "").endswith("tool_use"):
                        names[b.get("id")] = b.get("name")
        return names


def outcome(ex) -> dict:
    """What the reply did: text, tool calls, or just a stop reason."""
    reply = ex.get("reply") or {}
    if ex["kind"] == "count_tokens":
        n = (reply.get("count") or {}).get("input_tokens")
        return {"kind": "count", "text": f"{n} tokens" if n is not None else ""}
    calls: dict[str, int] = {}
    text = None
    for b in reply.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type", "").endswith("tool_use"):
            calls[b.get("name") or "?"] = calls.get(b.get("name") or "?", 0) + 1
        elif b.get("type") == "text" and text is None and (b.get("text") or "").strip():
            text = b["text"]
    return {"text": _snip(text) if text else None, "calls": [{"name": k, "n": v} for k, v in calls.items()],
            "stop": reply.get("stop_reason")}


def _first_text(m) -> str | None:
    if not isinstance(m, dict):
        return None
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text")
    return None


def _common_prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _snip(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= SNIPPET else one[:SNIPPET - 1] + "…"
