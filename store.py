"""store.py: in-memory exchanges, a content-addressed blob store, and the JSONL log.

Every request repeats the system prompt, the tool definitions and all earlier
messages. Each of those is hashed (SHA-256 of canonical JSON) and stored once.
An exchange keeps only lists of hashes (SPEC §3.1).

All `on_*` methods run on the recorder's single worker thread. Readers (the
dashboard's handler threads) go through the public methods, which take the lock.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import queue
import threading

from recorder import SSEParser, classify, parse_json, redact_headers

TOP_LEVEL = ("model", "max_tokens", "stream", "thinking", "metadata")
EVENT_BUFFER = 5000


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def blob_hash(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()[:24]


class Store:
    def __init__(self, max_exchanges: int = 2000, raw_keep: int = 100, log_path: str | None = None):
        self.lock = threading.RLock()
        self.max = max_exchanges
        self.raw_keep = raw_keep
        self.exchanges: "collections.OrderedDict[int, dict]" = collections.OrderedDict()
        self.blobs: dict[str, dict] = {}            # hash -> {"kind", "data", "size"}
        self.refs: collections.Counter = collections.Counter()
        self.raw_req: "collections.OrderedDict[int, bytes]" = collections.OrderedDict()
        self.raw_resp: dict[int, bytearray] = {}
        self.parsers: dict[int, SSEParser] = {}
        # live events for the dashboard (SSE), with ids for Last-Event-ID resume
        self.seq = 0
        self.events: collections.deque = collections.deque(maxlen=EVENT_BUFFER)
        self.subscribers: list[queue.Queue] = []
        self.listeners: list = []                    # callables(ex) run when an exchange starts (threads, §4.3)
        self.log_path = log_path
        self.log_file = None
        self.logged: set[str] = set()
        if log_path:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.chmod(log_path, 0o600)
            self.log_file = os.fdopen(fd, "a", encoding="utf-8")

    # --- blobs ---------------------------------------------------------------

    def _put(self, kind: str, obj) -> str:
        h = blob_hash(obj)
        if h not in self.blobs:
            self.blobs[h] = {"kind": kind, "data": obj, "size": len(canonical(obj))}
        self.refs[h] += 1
        return h

    def _release(self, hashes):
        for h in hashes:
            self.refs[h] -= 1
            if self.refs[h] <= 0:
                del self.refs[h]
                self.blobs.pop(h, None)

    @staticmethod
    def _ex_hashes(ex) -> list[str]:
        r = ex["request"]
        return list(r.get("system", [])) + list(r.get("tools", [])) + list(r.get("messages", []))

    # --- events --------------------------------------------------------------

    def emit(self, etype: str, data: dict):
        with self.lock:
            self.seq += 1
            ev = (self.seq, etype, data)
            self.events.append(ev)
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:  # a stuck browser must never block recording
                self.unsubscribe(q)
                try:
                    q.put_nowait((0, "_overflow", {}))
                except queue.Full:
                    pass

    def subscribe(self, last_id: int | None = None):
        """-> (queue, backlog events after last_id, or None if a gap means "resync")."""
        q: queue.Queue = queue.Queue(maxsize=10000)
        with self.lock:
            self.subscribers.append(q)
            backlog = None
            if last_id is not None:
                if not self.events or last_id >= self.events[0][0] - 1:
                    backlog = [e for e in self.events if e[0] > last_id]
        return q, backlog

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # --- recorder callbacks (worker thread) ----------------------------------

    def dispatch(self, op: str, ex_id: int, payload):
        with self.lock:  # readers copy under the same lock, so they never see a half-updated exchange
            getattr(self, "on_" + op)(ex_id, payload)

    def on_start(self, ex_id, payload):
        method, path, headers, body, started = payload
        body_json, parse_err = parse_json(body)
        kind = classify(path, body_json)
        req: dict = {"headers": redact_headers(headers), "size": len(body)}
        ex = {"id": ex_id, "thread": None, "method": method, "path": path, "kind": kind,
              "t": {"start": started, "connected_ms": None, "ttfb_ms": None, "total_ms": None},
              "status": None, "reason": None, "state": "pending", "request": req,
              "response": {"headers": [], "size": 0, "content_type": None},
              "reply": None, "error": None, "badges": []}
        if parse_err:
            ex["badges"].append("could not parse")
            req["parse_error"] = parse_err
        if kind in ("messages", "count_tokens"):
            b = body_json
            for k in TOP_LEVEL:
                if k in b:
                    req[k] = b[k]
            req["params"] = {k: v for k, v in b.items() if k not in TOP_LEVEL + ("system", "tools", "messages")}
            system = b.get("system")
            if isinstance(system, str):
                system = [{"type": "text", "text": system}]
            req["system"] = [self._put("system", s) for s in (system or [])]
            req["tools"] = [self._put("tool", t) for t in (b.get("tools") or [])]
            req["messages"] = [self._put("message", m) for m in b["messages"]]
            req["new_from"] = 0
        elif isinstance(body_json, (dict, list)):
            req["json"] = body_json      # §4.4: other traffic is shown as-is
        with self.lock:
            self.exchanges[ex_id] = ex
            self.raw_req[ex_id] = body
            while len(self.raw_req) > self.raw_keep:
                self.raw_req.popitem(last=False)
            while len(self.exchanges) > self.max:
                _, old = self.exchanges.popitem(last=False)
                self._release(self._ex_hashes(old))
                self.raw_resp.pop(old["id"], None)
        for fn in self.listeners:
            fn(self, ex)
        self.emit("exchange.start", self.summary(ex))

    def on_connected(self, ex_id, ms):
        ex = self.exchanges.get(ex_id)
        if ex:
            ex["t"]["connected_ms"] = ms

    def on_response(self, ex_id, payload):
        status, reason, headers, ms = payload
        ex = self.exchanges.get(ex_id)
        if not ex:
            return
        ex["status"], ex["reason"], ex["state"] = status, reason, "streaming"
        ex["response"]["headers"] = redact_headers(headers)
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "") or ""
        ex["response"]["content_type"] = ctype
        self.raw_resp[ex_id] = bytearray()
        if "text/event-stream" in ctype:
            self.parsers[ex_id] = SSEParser()
        self.emit("exchange.delta", {"id": ex_id, "notes": [{"op": "status", "status": status}]})

    def on_chunk(self, ex_id, payload):
        data, first_ms = payload
        ex = self.exchanges.get(ex_id)
        if not ex:
            return
        if first_ms is not None:
            ex["t"]["ttfb_ms"] = first_ms
        self.raw_resp.setdefault(ex_id, bytearray()).extend(data)
        ex["response"]["size"] += len(data)
        parser = self.parsers.get(ex_id)
        if parser and ex["kind"] == "messages":
            notes = parser.feed(data)
            ex["reply"] = parser.reply
            if notes:
                self.emit("exchange.delta", {"id": ex_id, "notes": notes})

    def on_finish(self, ex_id, payload):
        state, error, ms = payload
        ex = self.exchanges.get(ex_id)
        if not ex:
            return
        ex["t"]["total_ms"] = ms
        ex["state"] = state
        raw = bytes(self.raw_resp.get(ex_id, b""))
        parser = self.parsers.pop(ex_id, None)
        if parser:
            if ex["kind"] == "messages":
                parser.close()
                ex["reply"] = _clean_reply(parser.reply)
                if parser.error:
                    ex["error"] = {"type": "sse", "message": _err_msg(parser.error), "body": parser.error}
                elif not parser.stopped and state == "done" and (ex["status"] or 0) < 400:
                    ex["error"] = {"type": "incomplete", "message": "stream ended before message_stop"}
                    ex["badges"].append("incomplete")
                if parser.unknown:
                    ex["unknown_events"] = len(parser.unknown)
            else:
                for _ in parser.feed(raw):
                    pass
            ex["sse_events"] = len(parser.events) if parser.events else 0
        elif raw and ex["kind"] != "other":
            body, err = parse_json(raw)
            if err:
                ex["badges"].append("could not parse")
            elif (ex["status"] or 0) < 400 and isinstance(body, dict):
                if ex["kind"] == "messages" and body.get("type") == "message":
                    ex["reply"] = body
                elif ex["kind"] == "count_tokens":
                    ex["reply"] = {"count": body}
        if (ex["status"] or 0) >= 400:
            body, _ = parse_json(raw)
            ex["error"] = {"type": "upstream_http", "status": ex["status"],
                           "message": _err_msg((body or {}).get("error") if isinstance(body, dict) else None)
                           or f"HTTP {ex['status']} {ex['reason'] or ''}".strip(),
                           "body": body if body is not None else raw.decode("utf-8", "replace")}
        if error:
            ex["error"] = {"type": state, "message": error}
        if state == "aborted":
            ex["badges"].append("aborted")
        self._log(ex, raw)
        self.emit("exchange.error" if ex["error"] else "exchange.end", self.summary(ex))

    # --- persistence ---------------------------------------------------------

    def _log(self, ex, raw: bytes):
        if not self.log_file:
            return
        lines = []
        for h in self._ex_hashes(ex):
            if h not in self.logged and h in self.blobs:
                b = self.blobs[h]
                lines.append(json.dumps({"type": "blob", "hash": h, "kind": b["kind"], "data": b["data"]},
                                        ensure_ascii=False))
                self.logged.add(h)
        rec = dict(ex, raw_response_b64=base64.b64encode(raw).decode("ascii"))
        lines.append(json.dumps({"type": "exchange", "exchange": rec}, ensure_ascii=False))
        self.log_file.write("\n".join(lines) + "\n")
        self.log_file.flush()

    def load(self, path: str) -> int:
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec["type"] == "blob":
                    self.blobs.setdefault(rec["hash"], {"kind": rec["kind"], "data": rec["data"],
                                                        "size": len(canonical(rec["data"]))})
                elif rec["type"] == "exchange":
                    ex = rec["exchange"]
                    raw = base64.b64decode(ex.pop("raw_response_b64", "") or "")
                    ex.setdefault("badges", [])
                    with self.lock:
                        self.exchanges[ex["id"]] = ex
                        self.raw_resp[ex["id"]] = bytearray(raw)
                        for h in self._ex_hashes(ex):
                            self.refs[h] += 1
                        while len(self.exchanges) > self.max:
                            _, old = self.exchanges.popitem(last=False)
                            self._release(self._ex_hashes(old))
                            self.raw_resp.pop(old["id"], None)
                    n += 1
        return n

    def close(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    # --- reads (dashboard threads) -------------------------------------------

    def summary(self, ex) -> dict:
        r, reply = ex["request"], ex.get("reply") or {}
        return {"id": ex["id"], "thread": ex.get("thread"), "kind": ex["kind"], "method": ex["method"],
                "path": ex["path"], "model": r.get("model") or reply.get("model"), "status": ex["status"],
                "state": ex["state"], "t": ex["t"], "usage": reply.get("usage"),
                "stop_reason": reply.get("stop_reason"), "n_messages": len(r.get("messages", [])),
                "new_from": r.get("new_from"), "error": (ex["error"] or {}).get("message") if ex["error"] else None,
                "badges": ex.get("badges", []), **(ex.get("extra") or {})}

    def list_summaries(self, since: int = 0) -> list[dict]:
        with self.lock:
            return [self.summary(e) for i, e in self.exchanges.items() if i > since]

    def get(self, ex_id: int) -> dict | None:
        with self.lock:
            ex = self.exchanges.get(ex_id)
            return json.loads(json.dumps(ex)) if ex else None

    def get_raw(self, ex_id: int) -> dict | None:
        with self.lock:
            ex = self.exchanges.get(ex_id)
            if not ex:
                return None
            req = self.raw_req.get(ex_id)
            resp = bytes(self.raw_resp.get(ex_id, b""))
            reconstructed = False
            if req is None and ex["kind"] in ("messages", "count_tokens"):
                req = canonical(self.rebuild_request(ex))
                reconstructed = True
        return {"request_headers": ex["request"]["headers"], "response_headers": ex["response"]["headers"],
                "request_body": (req or b"").decode("utf-8", "replace"), "request_reconstructed": reconstructed,
                "response_body": resp.decode("utf-8", "replace"), "status": ex["status"], "reason": ex["reason"]}

    def rebuild_request(self, ex) -> dict:
        r = ex["request"]
        out = {k: r[k] for k in TOP_LEVEL if k in r}
        out.update(r.get("params") or {})
        for key in ("system", "tools", "messages"):
            if r.get(key):
                out[key] = [self.blobs.get(h, {}).get("data") for h in r[key]]
        return out

    def get_blob(self, h: str) -> dict | None:
        with self.lock:
            b = self.blobs.get(h)
            return dict(b, hash=h) if b else None


def _err_msg(err) -> str | None:
    if isinstance(err, dict):
        return f"{err.get('type', 'error')}: {err.get('message', '')}".strip(": ")
    return None


def _clean_reply(reply):
    if not reply:
        return reply
    for block in reply.get("content", []):
        if "_partial" in block:  # stream ended mid tool input
            block["input_raw"] = block.pop("_partial")
            block["input_error"] = "tool input was cut off"
    return reply
