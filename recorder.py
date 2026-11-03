"""recorder.py: capture each exchange the proxy forwards, without slowing it down.

The proxy's handler thread only calls the cheap `Recorder.*` hooks below. Each
hook copies references onto a queue and returns. A single worker thread does all
the real work: redaction, JSON parsing, incremental SSE parsing, hashing into
the store (SPEC C2, §3).

Every hook is wrapped so that any failure is logged once and swallowed. The
forwarding code never sees an exception from here.
"""

from __future__ import annotations

import fnmatch
import json
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

# --- redaction (SPEC C4) -----------------------------------------------------

REDACT_EXACT = {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}
REDACT_GLOBS = ["*token*", "*secret*"]
REDACTED = "[redacted]"


def is_sensitive(name: str) -> bool:
    n = name.lower()
    return n in REDACT_EXACT or any(fnmatch.fnmatchcase(n, g) for g in REDACT_GLOBS)


def redact_headers(items) -> list[list[str]]:
    """[(name, value), ...] -> [[name, value-or-redacted], ...]; order and duplicates kept."""
    out = []
    for name, value in items:
        if is_sensitive(name):
            value = f"{REDACTED} ({len(value)} chars)"
        out.append([name, value])
    return out


# --- incremental SSE parsing (SPEC §4.2) --------------------------------------

class SSEParser:
    """Feed it raw response chunks; it rebuilds the Anthropic message as they arrive.

    `feed()` returns a list of small "delta" notes describing what changed, which
    the dashboard streams to the browser (`exchange.delta`).
    """

    def __init__(self):
        self.buf = b""
        self.events: list[dict] = []          # every event, in order: {"event", "data"} (data parsed if JSON)
        self.reply: dict | None = None
        self.error: dict | None = None
        self.unknown: list[dict] = []
        self.stopped = False                  # saw message_stop

    def feed(self, chunk: bytes) -> list[dict]:
        self.buf = (self.buf + chunk).replace(b"\r\n", b"\n")
        notes = []
        while b"\n\n" in self.buf:
            block, self.buf = self.buf.split(b"\n\n", 1)
            ev = self._parse_block(block)
            if ev is not None:
                notes.extend(self._apply(ev))
        return notes

    def close(self) -> list[dict]:
        """End of stream: parse a final event that had no trailing blank line."""
        notes = []
        if self.buf.strip():
            ev = self._parse_block(self.buf)
            self.buf = b""
            if ev is not None:
                notes.extend(self._apply(ev))
        # Tool inputs that never got content_block_stop stay as their raw partial JSON.
        return notes

    @staticmethod
    def _parse_block(block: bytes) -> dict | None:
        name, data_lines = None, []
        for line in block.decode("utf-8", "replace").split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
        if name is None and not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw
        if name is None and isinstance(data, dict):
            name = data.get("type")
        return {"event": name or "message", "data": data}

    def _block(self, index):
        content = self.reply.setdefault("content", []) if self.reply is not None else None
        if content is None or not isinstance(index, int) or index < 0:
            return None
        while len(content) <= index:
            content.append({"type": "unknown"})
        return content[index]

    def _apply(self, ev: dict) -> list[dict]:
        self.events.append(ev)
        name, d = ev["event"], ev["data"]
        if not isinstance(d, dict):
            self.unknown.append(ev)
            return [{"op": "raw", "event": name}]
        t = d.get("type", name)
        if t == "message_start":
            msg = dict(d.get("message") or {})
            msg["content"] = list(msg.get("content") or [])
            self.reply = msg
            return [{"op": "start", "id": msg.get("id"), "model": msg.get("model"), "usage": msg.get("usage")}]
        if t == "ping":
            return []
        if t == "error":
            self.error = d.get("error") or d
            return [{"op": "error", "error": self.error}]
        if self.reply is None:  # content before message_start: keep it, don't crash
            self.reply = {"content": []}
        if t == "content_block_start":
            i = d.get("index")
            block = dict(d.get("content_block") or {})
            if block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                block["_partial"] = ""
            if self._block(i) is not None:
                self.reply["content"][i] = block
            return [{"op": "block", "index": i, "block": {k: v for k, v in block.items() if k != "_partial"}}]
        if t == "content_block_delta":
            i, delta = d.get("index"), d.get("delta") or {}
            block = self._block(i)
            if block is None:
                return []
            dt = delta.get("type")
            if dt == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
                return [{"op": "text", "index": i, "append": delta.get("text", "")}]
            if dt == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                return [{"op": "thinking", "index": i, "append": delta.get("thinking", "")}]
            if dt == "input_json_delta":
                block["_partial"] = block.get("_partial", "") + delta.get("partial_json", "")
                return [{"op": "input", "index": i, "append": delta.get("partial_json", "")}]
            if dt == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
                return []
            if dt == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
                return []
            block.setdefault("_unknown_deltas", []).append(delta)
            self.unknown.append(ev)
            return [{"op": "raw", "event": name}]
        if t == "content_block_stop":
            i = d.get("index")
            block = self._block(i)
            if block is not None and "_partial" in block:
                partial = block.pop("_partial")
                try:
                    block["input"] = json.loads(partial) if partial.strip() else (block.get("input") or {})
                except ValueError:
                    block["input_raw"] = partial
                    block["input_error"] = "tool input is not valid JSON"
                return [{"op": "block_stop", "index": i, "input": block.get("input"),
                         "input_raw": block.get("input_raw")}]
            return [{"op": "block_stop", "index": i}]
        if t == "message_delta":
            delta = d.get("delta") or {}
            for k in ("stop_reason", "stop_sequence"):
                if k in delta:
                    self.reply[k] = delta[k]
            if isinstance(d.get("usage"), dict):
                self.reply.setdefault("usage", {}).update(d["usage"])
            return [{"op": "meta", "stop_reason": self.reply.get("stop_reason"), "usage": self.reply.get("usage")}]
        if t == "message_stop":
            self.stopped = True
            return [{"op": "stop"}]
        self.unknown.append(ev)
        return [{"op": "raw", "event": name}]


# --- request classification ---------------------------------------------------

def parse_json(body: bytes):
    """-> (value, None) or (None, reason)."""
    if not body:
        return None, None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, "request body is not UTF-8"
    try:
        return json.loads(text), None
    except ValueError as e:
        return None, f"request body is not valid JSON: {e}"


def classify(path: str, body_json) -> str:
    p = path.split("?", 1)[0]
    if isinstance(body_json, dict) and isinstance(body_json.get("messages"), list):
        if p.endswith("/count_tokens"):
            return "count_tokens"
        if p.endswith("/v1/messages") or p.endswith("/messages"):
            return "messages"
    return "other"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --- the recorder -------------------------------------------------------------

class _Live:
    """Handler-side handle for one exchange. Only timestamps; nothing slow."""

    __slots__ = ("id", "t0")

    def __init__(self, ex_id: int):
        self.id = ex_id
        self.t0 = time.perf_counter()

    def ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 2)


class Recorder:
    """Thin, failure-proof front for the store. All hooks return immediately."""

    def __init__(self, store, log=print):
        self.store = store
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.ids = iter(range(1, 1 << 62))
        self.id_lock = threading.Lock()
        self.log = log
        self.failures = 0
        self.worker = threading.Thread(target=self._run, name="recorder", daemon=True)
        self.worker.start()

    # The hooks. Each is wrapped by _safe: never raises.

    def start(self, method: str, path: str, headers, body: bytes) -> _Live | None:
        def go():
            with self.id_lock:
                live = _Live(next(self.ids))
            self.q.put(("start", live.id, (method, path, list(headers), body, now_iso())))
            return live
        return self._safe(go)

    def connected(self, live):
        self._safe(lambda: live and self.q.put(("connected", live.id, live.ms())))

    def response(self, live, status: int, reason: str, headers):
        self._safe(lambda: live and self.q.put(("response", live.id, (status, reason, list(headers), live.ms()))))

    def chunk(self, live, data: bytes, first: bool):
        self._safe(lambda: live and self.q.put(("chunk", live.id, (data, live.ms() if first else None))))

    def finish(self, live, state: str = "done", error: str | None = None):
        self._safe(lambda: live and self.q.put(("finish", live.id, (state, error, live.ms()))))

    def flush(self, timeout: float = 5.0):
        """Wait until everything queued so far is processed (tests, shutdown)."""
        done = threading.Event()
        self.q.put(("barrier", 0, done))
        done.wait(timeout)

    def _safe(self, fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - the proxy must never see this
            self._fail()
            return None

    def _fail(self):
        self.failures += 1
        if self.failures <= 5:
            self.log(f"[recorder] error (forwarding unaffected):\n{traceback.format_exc()}")
            sys.stdout.flush()

    def _run(self):
        while True:
            op, ex_id, payload = self.q.get()
            if op == "barrier":
                payload.set()
                continue
            try:
                self.store.dispatch(op, ex_id, payload)
            except Exception:  # noqa: BLE001
                self._fail()
