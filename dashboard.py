#!/usr/bin/env python3
"""dashboard.py: live traffic dashboard for proxy.py. A separate program; proxy.py is not changed.

    python3 proxy.py                          # already running on :8041, left as it is
    python3 dashboard.py                      # relay on :8040 -> proxy.py :8041, page on http://localhost:8042
    ANTHROPIC_BASE_URL=http://localhost:8040 claude

The dashboard runs a small recording relay (relay.py) in front of the running
proxy.py. The agent talks to the relay; the relay records each exchange and
passes it on, unchanged, to proxy.py, which forwards it to the API as always.
proxy.py is neither changed nor restarted. The page has its own port, bound to
127.0.0.1.

    claude -> relay :8040 -> proxy.py :8041 -> api.anthropic.com
                 |
              page :8042

Other setups through flags: `--upstream https://api.anthropic.com` (no proxy.py
at all), or put the relay behind proxy.py with
`python3 proxy.py --upstream http://localhost:8040`.

    python3 dashboard.py --log traffic.jsonl  # also keep a session log (mode 0600)
    python3 dashboard.py --load traffic.jsonl # open a saved session; no relay
"""

from __future__ import annotations

import argparse
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


def _check_upstream(upstream: str) -> None:
    """Say so at startup if nothing answers at the upstream (e.g. proxy.py isn't running)."""
    import socket
    u = urlsplit(upstream)
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=1).close()
    except OSError:
        print(f"note: nothing is answering at {upstream} yet. Is proxy.py running? Requests will get a 502 until it is.",
              flush=True)


def main() -> None:
    from datetime import datetime

    from recorder import Recorder
    from relay import make_relay_handler
    from store import Store
    from threads import Threads

    ap = argparse.ArgumentParser(description="Live traffic dashboard for proxy.py (runs beside it; proxy.py is unchanged).")
    ap.add_argument("--port", type=int, default=8040, help="relay port the agent talks to (default 8040)")
    ap.add_argument("--dash-port", type=int, default=8042, help="dashboard page port (default 8042)")
    ap.add_argument("--upstream", default="http://localhost:8041",
                    help="where the relay forwards (default: the running proxy.py on http://localhost:8041)")
    ap.add_argument("--log", metavar="PATH", help="append finished exchanges to a JSONL session log (mode 0600)")
    ap.add_argument("--load", metavar="PATH", help="open a recorded session log; no relay is started")
    ap.add_argument("--max-exchanges", type=int, default=2000, help="exchanges kept in memory (default 2000)")
    ap.add_argument("--harness-patterns", metavar="PATH",
                    help="JSON rules for spotting harness-injected text (default: ./harness_patterns.json if present)")
    args = ap.parse_args()
    upstream = args.upstream.rstrip("/")
    ui = ui_config(args.harness_patterns)
    ui["session"] = args.load or datetime.now().isoformat(timespec="seconds")

    if args.load:
        store = Store(max_exchanges=args.max_exchanges)
        n = store.load(args.load)
        Threads(store, ui["harness_rules"]).rebuild()
        print(f"loaded {n} exchanges, {len(store.blobs)} unique blocks from {args.load}", flush=True)
        page = make_server(store, args.dash_port, dict(ui, mode="load", source=args.load))
        print(f"dashboard on http://localhost:{args.dash_port}", flush=True)
        try:
            page.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    if args.log:
        print(f"WARNING: logging to {args.log} (mode 0600). It holds full prompts, file contents and tool output; "
              "credentials are redacted.", flush=True)
    store = Store(max_exchanges=args.max_exchanges, log_path=args.log)
    Threads(store, ui["harness_rules"])
    rec = Recorder(store)
    relay = ThreadingHTTPServer(("127.0.0.1", args.port), make_relay_handler(upstream, rec))  # C3: loopback only
    relay.daemon_threads = True
    start(store, args.dash_port, dict(ui, mode="live", upstream=upstream))
    print(f"dashboard on http://localhost:{args.dash_port}", flush=True)
    print(f"relay listening on http://localhost:{args.port}  ->  {upstream}", flush=True)
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude", flush=True)
    _check_upstream(upstream)
    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rec.flush()
        store.close()


if __name__ == "__main__":
    main()
