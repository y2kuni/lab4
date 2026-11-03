# PLAN: implementing SPEC.md step by step

This plan follows the five milestones in SPEC §8 and splits each one into small steps. Every step ends in a working state: the proxy still forwards traffic, and there is a test or a manual check to prove it.

**Rules for every step**
- Python standard library only. The frontend is one `dashboard.html` (C1).
- Forwarding comes first. The recorder sees each chunk only *after* `wfile.write` + `flush`. Every recorder call is wrapped in `try/except` that logs the error and carries on (C2).
- Every server binds to `127.0.0.1` (C3). Redaction happens before anything is stored (C4). Recorded content is inserted with `textContent` only (C5).
- Each step ends with `python3 -m unittest` passing and one manual run of Claude Code through the proxy.

## Target layout

```
proxy.py            forwarding (as today) + CLI flags + wiring
recorder.py         Exchange capture, redaction, incremental SSE parser
store.py            in-memory exchanges, blob store (SHA-256), threads, JSONL log/load
dashboard.py        dashboard HTTP server + SSE broadcaster
dashboard.html      the whole UI (inline JS/CSS)
harness_patterns.json, prices.json   editable defaults
tests/              unittest + fixtures/ (recorded SSE streams, request bodies)
```

---

## Phase 0: Groundwork (½ day)

| Step | Work | Done when |
|---|---|---|
| 0.1 | Add `proxy.py` to the repo unchanged, as the baseline. | It's committed and tagged `baseline`. |
| 0.2 | Build a **golden forwarding test**: a fake upstream (`ThreadingHTTPServer` on a random port) that replays fixture SSE streams with delays, and a client that records the bytes and the TTFB it gets through the proxy. | The baseline proxy passes. This test then runs unchanged at the end of every later step (acceptance 6). |
| 0.3 | Collect fixtures: a streaming text reply, a `tool_use` reply with `input_json_delta`, a thinking reply, a non-streaming JSON reply, a 429/529 error body, an SSE `error` event, a truncated stream, a `count_tokens` call, a non-Anthropic (codex) body. Redact them by hand. | `tests/fixtures/` exists and holds no real credentials. |

## Phase 1: Recorder + store + JSONL, no UI (SPEC milestone 1)

| Step | Work | Done when |
|---|---|---|
| 1.1 | `redact_headers()`: `authorization`, `x-api-key`, `cookie`, `*token*`, `*secret*` (case-insensitive). | Unit tests. A grep for fixture secrets in every output is part of the test helpers. |
| 1.2 | `Exchange` object with timing marks (received, connected, first byte, last byte), status, headers, raw request and response bytes, `kind` (messages / count_tokens / other, §4.4). | Created and finished from `relay()` inside `try/except`. The golden test still passes. |
| 1.3 | Incremental SSE parser (§4.2). It is fed chunks, splits on event boundaries across chunk edges, and rebuilds `reply.content` and `usage`. Partial `tool_use` JSON stays as a raw string until `content_block_stop`. Unknown events are kept. | Each fixture, fed one byte at a time and also in random chunk sizes, gives the same final reply. |
| 1.4 | Non-streaming JSON replies, error bodies, transport failures, client aborts (`BrokenPipeError` → `aborted`), and bodies that are not JSON or not UTF-8 ("could not parse", §6). | A fixture test for each case. |
| 1.5 | `store.py`: a canonical-JSON SHA-256 blob store for system blocks, tools and messages. Exchanges hold hash lists (§3.1). Thread-safe with one `Lock`. A ring buffer keeps the last 2,000 exchanges (configurable). | 100 synthetic growing-conversation requests use roughly linear memory, not O(n²). |
| 1.6 | `--log PATH` (0600, append, one line per finished exchange, startup warning, D3), `--load PATH`, `--no-print`. | Round trip: record → log → load gives equal exchanges. `stat` shows mode 0600. |
| 1.7 | Measure the added TTFB (golden test, 200 runs, p95). | Under 5 ms added. Otherwise move the parsing onto a queue and a worker thread. |

## Phase 2: Dashboard server + SSE + timeline + raw view (SPEC milestone 2)

| Step | Work | Done when |
|---|---|---|
| 2.1 | `dashboard.py`: a second `ThreadingHTTPServer` on `--dash-port` 8042, with `--no-dash` (D1). It serves `GET /` and `/api/exchanges`, `/api/exchanges/{id}`, `/raw`, `/api/blob/{hash}`. | `curl` works for each endpoint. The API port still returns nothing from the dashboard. |
| 2.2 | `/api/events` SSE: a broadcaster with one queue per subscriber, the events `exchange.start/delta/end/error`, monotonic event ids, a replay buffer for `Last-Event-ID`, and heartbeats. A slow browser never blocks the recorder (drop it or resync). | Test: kill the connection partway through, reconnect with `Last-Event-ID`, and get no gaps and no duplicates. |
| 2.3 | `dashboard.html` skeleton: CSS variables for light and dark, a header with the live/reconnecting/offline dot, a timeline pane and a detail pane, and the layout that stacks below 900px. | The page loads offline and has no external requests (checked in devtools). |
| 2.4 | Timeline rows (§5.3, basic version): `#id`, time, method/path, status, latency, a streaming spinner, and the error border. New rows go on top. The "▲ N new" pill appears when you have scrolled away. Pause. | **Acceptance 1:** a live Claude Code session shows its first row within 1 s. |
| 2.5 | Raw tab (§5.4.7): redacted headers, the request body, response headers, and SSE events in order, with copy buttons. | **Acceptance 7, first check:** grep the page DOM and the API responses for the real token: no match. |
| 2.6 | Virtualised timeline (§6). | Smooth scrolling with 5,000 synthetic exchanges loaded through `--load`. |

## Phase 3: Parsed detail (SPEC milestone 3)

| Step | Work | Done when |
|---|---|---|
| 3.1 | Block renderers for each kind in §5.2 (icon, label and color always together). Long-text collapse (§5.8). Size badges. | A visual check page made from fixtures shows every kind. |
| 3.2 | Harness detection: built-in patterns, override with `harness_patterns.json`, no match means **You** (D2), and a "why?" tooltip showing the rule. | Unit tests in JS run from a `tests/ui.html` page. A real prompt that contains a `<tag>` is still shown as You. |
| 3.3 | Safe markdown renderer (escape everything first, then allow headings, emphasis, lists, code, links with `http(s)` only) plus a raw toggle (C5). | An XSS fixture set (`<img onerror>`, `javascript:` links, `</script>`) renders as inert text. |
| 3.4 | JSON tree (opens 2 levels deep, strings over 200 characters truncated, copy as JSON, string-held JSON parsed). Tool-result helpers: line numbers, `cat -n`, diff colors. Image thumbnails. | Fixture check. |
| 3.5 | Conversation tab: blob fetch with a cache, "N earlier messages ▸" that expands step by step, an anchor, copy and raw toggle on each message. | Opening a 200-message exchange fetches only the blobs it shows. |
| 3.6 | Tool call ↔ result pairing by id across exchanges, "arrived in #N", "⧗ awaiting result". | **Acceptance 3.** |
| 3.7 | System, Tools (searchable, schema tree), Reply (live streaming text, "receiving…" for tool input), and Request-parameters tabs. | **Acceptance 4, first part** (everything can be read in full). |

## Phase 4: Threads + "What's new" + diffs (SPEC milestone 4)

| Step | Work | Done when |
|---|---|---|
| 4.1 | Thread assignment in `store.py` (§4.3): prefix match against the last exchange of each live thread with the same system hashes. Hash lists make this cheap. | Unit tests: main loop + subagent + side calls interleaved are split correctly. |
| 4.2 | `new_from` and "history rewritten" (compaction), with the changed range. | A compaction fixture is flagged. |
| 4.3 | Thread labels (main / subagent N / side call) and renaming in the UI (kept in `localStorage`). Thread chips in the timeline. | Visual check on a real session with a subagent. |
| 4.4 | Timeline rows now show the trigger (👤 "…" or ◧ Bash ✓) and the outcome (◆ "…", ⟳ Read ×2). | **Acceptance 2:** with 50+ exchanges, a reviewer picks out the human-prompt rows without opening any. |
| 4.5 | "What's new" panel as the default focus. Diffs: system line diff, tools added/removed/changed, model or parameter changes, a cache hit-rate drop chip. "Changed since #N" badges. | **Acceptance 4** in full. |

## Phase 5: Stats, filters, search, keyboard, polish (SPEC milestone 5)

| Step | Work | Done when |
|---|---|---|
| 5.1 | `prices.json` per model family (cache write and read priced separately). "estimate" label. A model missing from the table shows "cost unknown", never $0. Header totals. | Unit test on the price calculation, including the unknown-model case. |
| 5.2 | Stats drawer: per-request token split, session totals, a cost-over-time line (inline SVG), cache hit rate, and the tool usage table. Clicking anything filters the timeline. | Visual check. |
| 5.3 | Filter chips (thread, kind, tool, model, status) kept in the URL hash. "Errors only" (`e`) and the header error-count link. | **Acceptance 5.** |
| 5.4 | Full-text search over the loaded and cached content, highlighting, `n`/`N`. | Fixture check. |
| 5.5 | Keyboard map (§5.9) and the `?` help overlay. Follow mode (`f`). | Manual check. |
| 5.6 | Robustness pass (§6): rate-limit and `retry-after` headers shown, SSE back-off, a memory cap test. | Soak test: a 1-hour real session, with RSS checked and the golden test re-run. |
| 5.7 | Final run through all acceptance criteria (§7 #1–8), including `--load` replay (#8). README with usage. | Every criterion ticked off in the README. |

---

## Risks and how this plan handles them

- **Slowing the agent (C2/AC6).** The golden TTFB test runs at every step. If the cost goes over budget, the fallback is a queue and a worker thread (1.7).
- **Thread detection guesses wrong.** It is isolated in `store.py`, covered by tests with interleaved fixtures, and the user can rename labels.
- **XSS from tool output (C5).** There is a dedicated fixture set (3.3), and the code has no `innerHTML` for content (enforced by a grep in the tests).
- **Credential leaks (C4/AC7).** Redaction happens at capture time, and the secret grep runs over the store, the log, the API and the DOM.
