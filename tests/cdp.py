"""cdp.py: drive headless Chrome over the DevTools protocol, standard library only.

Used by the UI tests to load the dashboard, wait for it, run JS checks, click,
and take screenshots.

    with Browser() as b:
        b.goto("http://localhost:8042/")
        b.wait("document.querySelectorAll('.row').length > 3")
        b.screenshot("out.png")
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time

from tests.harness import free_port

CHROME = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)


class WS:
    """Just enough RFC 6455 for one CDP connection."""

    def __init__(self, host: str, port: int, path: str):
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(head.decode())

    def send(self, text: str):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b0 & 0x0F
            if op == 0x9:  # ping
                continue
            if op == 0x8:
                raise ConnectionError("websocket closed")
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")


class Browser:
    def __init__(self, width: int = 1400, height: int = 900, dark: bool = False):
        if not CHROME:
            raise RuntimeError("no Chrome/Chromium found")
        self.width, self.height, self.dark = width, height, dark
        self.next_id = 0
        self.console: list[str] = []

    def __enter__(self):
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="cdp-")
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                                      "--hide-scrollbars", f"--user-data-dir={self.profile}",
                                      f"--remote-debugging-port={self.port}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                c.request("PUT", "/json/new?about:blank")
                target = json.loads(c.getresponse().read())
                break
            except (OSError, ValueError):
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        path = target["webSocketDebuggerUrl"].split(str(self.port), 1)[1]
        self.ws = WS("127.0.0.1", self.port, path)
        self.cmd("Runtime.enable")
        self.cmd("Page.enable")
        self.resize(self.width, self.height)
        if self.dark:
            self.cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        return self

    def __exit__(self, *exc):
        try:
            self.proc.terminate()
            self.proc.wait(5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def cmd(self, method: str, **params):
        self.next_id += 1
        my = self.next_id
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.consoleAPICalled":
                self.console.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
            elif msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.console.append("EXCEPTION: " + (d.get("exception", {}).get("description") or d.get("text", "")))
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def resize(self, w: int, h: int):
        self.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)

    def goto(self, url: str):
        self.cmd("Page.navigate", url=url)
        self.wait("document.readyState === 'complete'")

    def js(self, expr: str):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error in {expr!r}: {r['exceptionDetails'].get('exception', {}).get('description')}")
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 10):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(f"!!({expr})"):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for: {expr}")

    def click(self, selector: str):
        ok = self.js(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return false; e.click(); return true; }})()")
        if not ok:
            raise RuntimeError(f"no element {selector}")

    def key(self, key: str):
        self.js(f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: {json.dumps(key)}, bubbles: true}}))")

    def screenshot(self, path: str, full: bool = False):
        params = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        data = self.cmd("Page.captureScreenshot", **params)["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
