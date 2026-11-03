#!/usr/bin/env python3
"""proxy.py: the naive agent-traffic proxy for Lab 4.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Everything Claude Code sends to the API now passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

    python3 proxy.py --port 9000
    python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
    python3 proxy.py --log traffic.jsonl     # also keep a session log (mode 0600)
    python3 proxy.py --no-print              # quiet terminal

Recording (recorder.py, store.py) is an observer: each chunk is sent to the
client first and handed to the recorder after. A recorder failure is logged and
forwarding carries on.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

UPSTREAM = "https://api.anthropic.com"
PRINT = True
REC = None  # recorder.Recorder, set in main()


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        rec = REC.start(self.command, self.path, self.headers.items(), body) if REC else None

        # 1. Print what the agent sent.
        if PRINT:
            print(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
            for name, value in self.headers.items():
                if name.lower() in ("authorization", "x-api-key"):
                    value = value[:16] + "..."
                print(f"    {name}: {value}")
            if body:
                try:
                    print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
                except ValueError:
                    print(body.decode("utf-8", "replace"))

        # 2. Forward it, headers intact.
        target = urlsplit(UPSTREAM)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            print(f"\n[{stamp()}] !!! cannot reach {UPSTREAM}: {e}", flush=True)
            self.send_error(502, f"proxy: cannot reach {UPSTREAM}: {e}")
            self.close_connection = True
            if REC:
                REC.response(rec, 502, "Bad Gateway", [])
                REC.finish(rec, "error", f"proxy: cannot reach {UPSTREAM}: {e}")
            return
        if REC:
            REC.connected(rec)
        upstream.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        try:
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except BaseException as e:
            if REC:
                REC.finish(rec, "error", f"upstream failed before replying: {e!r}")
            raise

        # 3. Stream the reply back as it arrives, printing it on the way.
        if REC:
            REC.response(rec, reply.status, reply.reason, reply.getheaders())
        if PRINT:
            print(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
        self.send_response(reply.status, reply.reason)
        for name, value in reply.getheaders():
            if name.lower() not in SKIP and name.lower() != "content-encoding":
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            if REC:
                REC.finish(rec)
            return
        first = True
        while True:
            try:
                chunk = reply.read1(4096)  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
            except BaseException as e:
                if REC:
                    REC.finish(rec, "error", f"upstream stream failed: {e!r}")
                raise
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except BaseException as e:
                if REC:
                    REC.chunk(rec, chunk, first)
                    REC.finish(rec, "aborted", f"client disconnected: {e!r}")
                raise
            if REC:  # only after the bytes have gone to the client (SPEC C2)
                REC.chunk(rec, chunk, first)
            first = False
            if PRINT:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        self.close_connection = True
        if REC:
            REC.finish(rec)
        if PRINT:
            print(f"\n[{stamp()}] --- done", flush=True)


def main() -> None:
    global UPSTREAM, PRINT, REC
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--upstream", default=UPSTREAM, help="where to forward (default: the Anthropic API)")
    parser.add_argument("--log", metavar="PATH", help="append finished exchanges to a JSONL session log (mode 0600)")
    parser.add_argument("--load", metavar="PATH", help="open a recorded session log; forwards nothing")
    parser.add_argument("--no-print", action="store_true", help="don't dump traffic to the terminal")
    parser.add_argument("--max-exchanges", type=int, default=2000, help="exchanges kept in memory (default 2000)")
    parser.add_argument("--dash-port", type=int, default=8042, help="dashboard port (default 8042)")
    parser.add_argument("--no-dash", action="store_true", help="don't start the dashboard")
    parser.add_argument("--harness-patterns", metavar="PATH",
                        help="JSON rules for spotting harness-injected text (default: ./harness_patterns.json if present)")
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    PRINT = not args.no_print

    import dashboard
    ui = dashboard.ui_config(args.harness_patterns)
    ui["session"] = args.load or datetime.now().isoformat(timespec="seconds")
    from recorder import Recorder
    from store import Store
    from threads import Threads

    if args.load:
        store = Store(max_exchanges=args.max_exchanges)
        n = store.load(args.load)
        Threads(store, ui["harness_rules"]).rebuild()
        print(f"loaded {n} exchanges, {len(store.blobs)} unique blocks from {args.load} (nothing is forwarded)", flush=True)
        if args.no_dash:
            return
        dash = dashboard.make_server(store, args.dash_port, dict(ui, mode="load", source=args.load))
        print(f"dashboard on http://localhost:{args.dash_port}", flush=True)
        try:
            dash.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    if args.log:
        print(f"WARNING: logging to {args.log} (mode 0600). It holds full prompts, file contents and tool output; "
              "credentials are redacted.", flush=True)
    store = Store(max_exchanges=args.max_exchanges, log_path=args.log)
    Threads(store, ui["harness_rules"])
    REC = Recorder(store)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    if not args.no_dash:
        dashboard.start(store, args.dash_port, dict(ui, mode="live", upstream=UPSTREAM))
        print(f"dashboard on http://localhost:{args.dash_port}", flush=True)
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        REC.flush()
        store.close()


if __name__ == "__main__":
    main()
