"""dashboard.py: the dashboard's own HTTP server (SPEC §3, §3.2, D1).

It runs in the proxy's process on a separate port, bound to 127.0.0.1, so no
dashboard path can ever clash with an API path. It only reads from the store.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
HEARTBEAT_S = 15

# The page loads nothing from anywhere else, and recorded content can never run as script (C1, C5).
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DashHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store = None          # set by make_server
    config: dict = {}
    allowed_hosts: set = set()

    def log_message(self, format, *args):  # noqa: A002
        pass

    # --- plumbing ------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200, cache: str = "no-store"):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   cache)

    def _not_found(self):
        self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self._json({"error": "read-only"}, 405)

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):
        # DNS-rebinding guard: only answer requests addressed to this machine by name or loopback IP.
        host = (self.headers.get("Host") or "").lower()
        if host not in self.allowed_hosts:
            self._json({"error": "bad host"}, 403)
            return
        url = urlsplit(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                           extra=[("Content-Security-Policy", CSP)])
            elif path == "/api/events":
                self._events(qs)
            elif path == "/api/config":
                self._json(self.config)
            elif path == "/api/exchanges":
                since = int((qs.get("since") or ["0"])[0])
                with self.store.lock:
                    seq = self.store.seq
                    items = self.store.list_summaries(since)
                    threads = self.store.threads.labels if self.store.threads else {}
                self._json({"seq": seq, "exchanges": items, "threads": threads})
            elif m := re.fullmatch(r"/api/exchanges/(\d+)", path):
                ex = self.store.get(int(m.group(1)))
                self._json(ex) if ex else self._not_found()
            elif m := re.fullmatch(r"/api/exchanges/(\d+)/raw", path):
                raw = self.store.get_raw(int(m.group(1)))
                self._json(raw) if raw else self._not_found()
            elif m := re.fullmatch(r"/api/blob/([0-9a-f]{8,64})", path):
                b = self.store.get_blob(m.group(1))
                # content-addressed: the same hash always means the same content
                self._json(b, cache="private, max-age=31536000, immutable") if b else self._not_found()
            else:
                self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- SSE -----------------------------------------------------------------

    def _events(self, qs):
        last = self.headers.get("Last-Event-ID") or (qs.get("last") or [None])[0]
        last_id = int(last) if last and str(last).isdigit() else None
        q, backlog = self.store.subscribe(last_id)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 1000\n\n")
            if last_id is not None and backlog is None:
                self._write_event(self.store.seq, "resync", {"reason": "event buffer overflowed"})
            for ev in backlog or []:
                self._write_event(*ev)
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev[1] == "_overflow":  # we fell behind; tell the page to refetch
                    self._write_event(self.store.seq, "resync", {"reason": "client too slow"})
                    self.wfile.flush()
                    return
                self._write_event(*ev)
                if q.empty():
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.store.unsubscribe(q)

    def _write_event(self, seq: int, etype: str, data: dict):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"id: {seq}\nevent: {etype}\ndata: {payload}\n\n".encode("utf-8"))


# SPEC §5.2, D2: text in a `user` message is "harness" only when one of these rules matches the
# whole block. Anything else is shown as the human's prompt, so a bad rule can't hide a real prompt.
DEFAULT_HARNESS_RULES = [
    {"name": "<system-reminder> block", "pattern": r"^\s*<system-reminder>[\s\S]*</system-reminder>\s*$"},
    {"name": "slash command (<command-name>)", "pattern": r"^\s*<command-(name|message|args)>[\s\S]*</command-(name|message|args)>\s*$"},
    {"name": "local command output", "pattern": r"^\s*<local-command-(stdout|stderr)>[\s\S]*</local-command-(stdout|stderr)>\s*$"},
    {"name": "hook output", "pattern": r"^\s*<user-prompt-submit-hook>[\s\S]*</user-prompt-submit-hook>\s*$"},
    {"name": "task notification", "pattern": r"^\s*<task-notification>[\s\S]*</task-notification>\s*$"},
    {"name": "local-command caveat", "pattern": r"^\s*Caveat: The messages below were generated by the user while running local commands"},
    {"name": "interrupt marker", "pattern": r"^\s*\[Request interrupted by user[^\]]*\]\s*$"},
    {"name": "compaction summary", "pattern": r"^\s*This session is being continued from a previous conversation"},
]


def ui_config(patterns_path: str | None) -> dict:
    """Harness rules: built-in defaults, replaced by a JSON file if given (SPEC D2)."""
    rules, source = DEFAULT_HARNESS_RULES, "built-in"
    path = Path(patterns_path) if patterns_path else Path.cwd() / "harness_patterns.json"
    if patterns_path or path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data["rules"] if isinstance(data, dict) else data
        for r in rules:
            re.compile(r["pattern"])  # fail at startup, not in the browser
        source = str(path)
    return {"harness_rules": rules, "harness_source": source}


def make_server(store, port: int, config: dict | None = None) -> ThreadingHTTPServer:
    handler = type("Handler", (DashHandler,), {
        "store": store,
        "config": config or {},
        "allowed_hosts": {f"localhost:{port}", f"127.0.0.1:{port}"},
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)  # C3: loopback only
    server.daemon_threads = True
    return server


def start(store, port: int, config: dict | None = None) -> ThreadingHTTPServer:
    server = make_server(store, port, config)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server
