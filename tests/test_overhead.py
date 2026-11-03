"""TTFB overhead vs. the baseline proxy (PLAN 1.7, acceptance 6): must stay under 5 ms."""

import os
import statistics
import subprocess
import tempfile
import unittest

from tests.harness import ROOT, FakeUpstream, Proxy, call, fixture

SCRIPT = os.environ.get("PROXY_SCRIPT", str(ROOT / "proxy.py"))
RUNS = int(os.environ.get("OVERHEAD_RUNS", "200"))


def p95(xs):
    return sorted(xs)[int(len(xs) * 0.95) - 1]


class Overhead(unittest.TestCase):
    def test_ttfb_overhead_under_5ms(self):
        base_src = subprocess.run(["git", "show", "baseline:proxy.py"], cwd=ROOT, capture_output=True,
                                  check=True).stdout
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "proxy_baseline.py")
            with open(base, "wb") as f:
                f.write(base_src)
            body = fixture("request_messages.json")
            hdr = {"x-fixture": "tool_use_stream.sse", "x-chunks": "8", "x-gap-ms": "1"}
            with FakeUpstream() as up, Proxy(up.url, base) as pb, Proxy(up.url, SCRIPT) as pc:
                for p in (pb, pc):  # warm up
                    for _ in range(10):
                        call(p.port, body, hdr)
                b, c = [], []
                for _ in range(RUNS):  # interleave to cancel out machine noise
                    b.append(call(pb.port, body, hdr)["ttfb_ms"])
                    c.append(call(pc.port, body, hdr)["ttfb_ms"])
        diff_med = statistics.median(c) - statistics.median(b)
        diff_p95 = p95(c) - p95(b)
        print(f"\nTTFB baseline median {statistics.median(b):.2f} p95 {p95(b):.2f} | "
              f"current median {statistics.median(c):.2f} p95 {p95(c):.2f} | "
              f"added median {diff_med:+.2f} ms p95 {diff_p95:+.2f} ms")
        self.assertLess(diff_med, 5.0)
        self.assertLess(diff_p95, 5.0)


if __name__ == "__main__":
    unittest.main()
