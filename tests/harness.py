"""Test helpers: a fake upstream API, a proxy launcher, and a timing client.

The fake upstream picks its reply from request headers sent by the test:
    x-fixture:  file name in tests/fixtures (default text_stream.sse)
    x-status:   HTTP status to answer with (default 200)
    x-chunks:   how many pieces to split the reply into (default 5)
    x-gap-ms:   delay between pieces (default 2)
    x-delay-ms: delay before the first byte (default 0)
    x-cut:      "1" = close the socket mid-reply without finishing the chunked body
Every request it receives is kept in `.received` for assertions.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Credentials the tests send; nothing derived from them may appear in any output.
SECRET_BEARER = "Bearer sk-ant-oat01-SECRETSECRETSECRET-abcdef0123456789"
SECRET_KEY = "sk-ant-api03-KEYKEYKEYKEYKEY-0123456789abcdef"
SECRET_COOKIE = "session=COOKIECOOKIECOOKIE"
SECRET_TOKEN = "TOKVALTOKVALTOKVAL"
SECRET_MARKERS = ["SECRETSECRETSECRET", "KEYKEYKEYKEYKEY", "COOKIECOOKIECOOKIE", "TOKVALTOKVALTOKVAL"]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: D401
        pass

    def do_POST(self):
        self._serve()

    def do_GET(self):
        self._serve()

    def _serve(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received.append({"method": self.command, "path": self.path,
                                     "headers": dict(self.headers.items()), "body": body})
        name = self.headers.get("x-fixture", "text_stream.sse")
        data = fixture(name)
        status = int(self.headers.get("x-status", "200"))
        chunks = max(1, int(self.headers.get("x-chunks", "5")))
        gap = int(self.headers.get("x-gap-ms", "2")) / 1000
        delay = int(self.headers.get("x-delay-ms", "0")) / 1000
        cut = self.headers.get("x-cut") == "1"
        if delay:
            time.sleep(delay)
        self.send_response(status)
        ctype = "text/event-stream" if name.endswith(".sse") else "application/json"
        self.send_header("content-type", ctype)
        self.send_header("request-id", "req_fake")
        if status == 429:
            self.send_header("retry-after", "7")
            self.send_header("anthropic-ratelimit-requests-remaining", "0")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        size = -(-len(data) // chunks)
        pieces = [data[i:i + size] for i in range(0, len(data), size)]
        for i, piece in enumerate(pieces):
            if cut and i == len(pieces) // 2:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.flush()
            if gap and i < len(pieces) - 1:
                time.sleep(gap)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True


class FakeUpstream:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.server.daemon_threads = True
        self.server.received = []
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def received(self):
        return self.server.received

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Proxy:
    """Runs a proxy script as a subprocess on free ports."""

    def __init__(self, upstream: str, script: Path | str = ROOT / "proxy.py", extra: list[str] | None = None,
                 dash: bool = False):
        self.port = free_port()
        self.dash_port = free_port() if dash else None
        self.script = str(script)
        self.args = [sys.executable, "-u", self.script, "--port", str(self.port), "--upstream", upstream]
        if dash:
            self.args += ["--dash-port", str(self.dash_port)]
        self.args += extra or []
        self.out_path = None

    def __enter__(self):
        import tempfile
        self.out = tempfile.NamedTemporaryFile("w+b", prefix="proxy-out-", delete=False)
        self.out_path = self.out.name
        self.proc = subprocess.Popen(self.args, stdout=self.out, stderr=subprocess.STDOUT, cwd=str(ROOT))
        ports = [self.port] + ([self.dash_port] if self.dash_port else [])
        deadline = time.time() + 10
        for p in ports:
            while True:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"proxy exited early:\n{self.output()}")
                try:
                    socket.create_connection(("127.0.0.1", p), timeout=0.2).close()
                    break
                except OSError:
                    if time.time() > deadline:
                        raise RuntimeError(f"proxy did not start:\n{self.output()}")
                    time.sleep(0.05)
        return self

    def output(self) -> str:
        self.out.flush()
        return Path(self.out_path).read_bytes().decode("utf-8", "replace")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.out.close()
        os.unlink(self.out_path)


def call(port: int, body: bytes = b"{}", headers: dict | None = None, path: str = "/v1/messages",
         method: str = "POST") -> dict:
    """Send one request; return status, headers, body bytes and TTFB/total in ms."""
    h = {"content-type": "application/json", "authorization": SECRET_BEARER, "x-api-key": SECRET_KEY,
         "cookie": SECRET_COOKIE, "x-session-token": SECRET_TOKEN, "anthropic-beta": "oauth-2025-04-20",
         "anthropic-version": "2023-06-01"}
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    t0 = time.perf_counter()
    conn.request(method, path, body=body if method != "GET" else None, headers=h)
    resp = conn.getresponse()
    first = resp.read1(65536) if method != "HEAD" else b""
    ttfb = (time.perf_counter() - t0) * 1000
    rest = b""
    try:
        while method != "HEAD" and (chunk := resp.read1(65536)):
            rest += chunk
    except http.client.IncompleteRead as e:
        rest += e.partial
    total = (time.perf_counter() - t0) * 1000
    conn.close()
    return {"status": resp.status, "headers": dict(resp.getheaders()), "body": first + rest,
            "ttfb_ms": ttfb, "total_ms": total}
