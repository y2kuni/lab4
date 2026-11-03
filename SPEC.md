# SPEC: Live traffic dashboard for `proxy.py`

Status: final · 2026-11-02 · amended 2026-11-03 (§4.3.1 terminal and agent attribution; §8 incremental build plan; C1 fastest-to-build language; C6 and §3 passive capture: `proxy.py` unchanged, attach to already-running agents; §4.5 Codex support)

## 1. Goal

While `proxy.py` is running, you can start the dashboard and open a page in the browser that updates live and shows the conversation between the agent and the model, for **both Claude Code and Codex**, in a form that is easy to read.

**`proxy.py` is not changed, and nothing has to be restarted.** The dashboard is started separately, *after* `proxy.py` and the agents are already running, and it captures their conversations from that moment on (C6, §3).

**Readability is the main requirement.** During a busy agent turn, requests arrive every few seconds and each one carries the whole conversation again. At a glance you must be able to:

- tell the kinds of message apart: user prompt, model text, tool call, tool result, error;
- see what is *new* in each request, without rereading everything that came before;
- follow the conversation as it grows;
- open any message and see every detail of it, including the raw JSON.

Statistics (tokens, cost, timing, tool usage) are useful, but they come second. They never take the place of the messages.

### Non-goals (v1)

- Changing, blocking or replaying traffic. The proxy stays an observer.
- Full parsing of APIs other than the Anthropic Messages API (Claude Code) and the OpenAI Responses API (Codex). Other traffic is recorded and shown as raw request and response (§4.4).
- Multiple users, authentication, or remote access.

## 2. Constraints

| # | Constraint | Why |
|---|---|---|
| C1 | **Use the language that is fastest to build with.** For this project that is Python with the standard library only: `proxy.py` and the existing code are already Python, and it needs no compile step, no package install and no new toolchain. The frontend is one HTML file with inline JS/CSS, with no build step and no CDN. A different language is allowed only if it is clearly faster to build with for a given part, and that choice is recorded in §9. | Build speed comes first: every step in §8 should be quick to write, run and test. Nothing to install, works offline. |
| C2 | **Forwarding behaves exactly as it does now.** The dashboard is never in the forwarding path: it only observes copies of the traffic. If the dashboard crashes, stops or falls behind, the agent and `proxy.py` are not affected. | The dashboard must never break or slow down the agent. |
| C3 | Everything binds to `127.0.0.1` only. | Traffic contains prompts, file contents, tokens. |
| C4 | Credentials are redacted *before* they are stored (`authorization`, `x-api-key`, `cookie`, `chatgpt-account-id`, and any header matching `*token*`/`*secret*`/`*account*`). | Nothing on disk or in the browser holds a usable credential. |
| C5 | Recorded content is untrusted. It is never inserted as HTML: use `textContent`, or a markdown renderer that escapes everything and allows only safe tags. | Tool results contain arbitrary file and web content. |
| C6 | **`proxy.py` is not changed** (not a line, not a flag), and the agents and `proxy.py` are not restarted or reconfigured. The dashboard must attach to a `proxy.py` that is already running and to agents that are already talking to it. | Long agent sessions are already in progress and must not be interrupted to be observed. |

## 3. Architecture

```
 claude ──HTTP (plain, loopback)──▶ proxy.py :8041 ──HTTPS──▶ api.anthropic.com
   (already running)                 (unchanged, already running)
            │
            └── passive copy of the loopback packets on port 8041
                     ▼
            dashboard.py: Capture ──▶ Recorder ──▶ in-memory store ──▶ traffic.jsonl (optional)
                                                       │
                                                       ▼
                                        Dashboard server :8042 ──SSE──▶ browser (dashboard.html)
```

- **Separate program, started any time.** `dashboard.py` runs as its own process. It does not import, wrap or replace `proxy.py`, and it can be started and stopped while `proxy.py` and the agents keep running.
- **Capture (how it sees traffic without touching `proxy.py`).** Between the agent and `proxy.py` the traffic is plain HTTP on `127.0.0.1:8041`. The dashboard listens passively on the loopback interface (a Linux `AF_PACKET` raw socket from the standard library, filtered to the `proxy.py` port or ports), reassembles each TCP connection, and parses the HTTP request and the streamed response from it. It only reads copies of packets, so it cannot delay or change them (C2). This needs `CAP_NET_RAW` (for example `sudo python3 dashboard.py`, or `setcap` on a dedicated interpreter); without it the dashboard exits with a clear message saying so.
- **Joining agents that are already running.** Connections that were already open when capture started are skipped until their next request (the start of a reply cannot be recovered). That costs nothing: every request from Claude Code and from Codex carries the **whole conversation so far**, so the first request captured from an agent already shows its full history, its system prompt and its tools. Exchanges that started before capture are counted in the header ("2 in-flight exchanges missed").
- **Ports and agents.** `--proxy-port` (default 8041) names the port `proxy.py` listens on. It can be given more than once, because Claude Code and Codex usually go through separate `proxy.py` instances (for example `python3 proxy.py` on 8041 for Claude Code and `python3 proxy.py --port 8043 --upstream https://chatgpt.com/backend-api/codex` for Codex). One dashboard shows all of them together. The capture never needs to see the HTTPS side, so nothing is decrypted.
- **Recorder.** For each exchange it records the request (method, path, redacted headers, body), and then the response: status, headers, raw bytes, and timing marks (request seen, first response byte, last byte; there is no "upstream connected" mark, because that happens inside `proxy.py`). It parses the Anthropic SSE stream *incrementally* so the dashboard can show the reply while it is still arriving.
- **Store.** An in-memory list of exchanges plus a content-addressed blob store (§3.1). It is thread-safe.
- **Persistence.** Off by default (D3). `--log traffic.jsonl` appends one line per finished exchange, with file mode `0600`. `--load traffic.jsonl` opens a recorded session in the dashboard without capturing anything (and without needing `CAP_NET_RAW`).
- CLI flags, all on `dashboard.py`: `--proxy-port`, `--dash-port`, `--log PATH`, `--load PATH`, `--harness-patterns PATH`. `proxy.py` keeps printing to its own terminal as it does today.
- **Relay (optional, not required).** A recording relay in front of `proxy.py` (`relay.py`) may be kept for agents started later without privileges, but it is never needed: agents that are already running are always handled by capture.

### 3.1 Deduplication (needed for this to work at all)

Every request repeats the system prompt, every tool definition, and all earlier messages. Over a session that is O(n²) bytes. To avoid it:

- Hash each system block, each tool definition, and each message (SHA-256 of canonical JSON), and store each one once.
- An exchange stores lists of hashes, not copies.
- The browser gets small **summaries** over SSE and fetches the full content only when a message is opened (`GET /api/blob/{hash}`), then caches it.

### 3.2 Dashboard HTTP API

| Endpoint | Returns |
|---|---|
| `GET /` | `dashboard.html` |
| `GET /api/events` | SSE stream: `exchange.start`, `exchange.delta` (reply streaming), `exchange.end`, `exchange.error`. Supports `Last-Event-ID` so a reconnect resumes with no gaps. |
| `GET /api/exchanges?since=ID` | Summaries (used on first load and after a reconnect). |
| `GET /api/exchanges/{id}` | Full parsed exchange (hash lists plus the reply). |
| `GET /api/exchanges/{id}/raw` | Raw request body and raw response bytes, exactly as they went over the wire. |
| `GET /api/blob/{hash}` | One system block, tool definition or message. |

## 4. Data model

### 4.1 Exchange

```jsonc
{
  "id": 42,
  "thread": "t3",                    // §4.3
  "terminal": "a1b2c3",              // short terminal ID, §4.3.1
  "agent": "subagent 2",             // main | subagent N | side call, §4.3.1
  "method": "POST", "path": "/v1/messages",
  "agent_app": "claude-code",        // claude-code | codex | unknown, §4.5
  "kind": "messages",                // messages | responses | count_tokens | other
  "t": { "start": "...", "connected_ms": 80, "ttfb_ms": 1450, "total_ms": 9200 },
  "status": 200,
  "request": {
    "model": "...", "max_tokens": 32000, "stream": true,
    "thinking": {...}, "metadata": {...}, "params": {...},   // everything else
    "system": ["h1", "h2"],          // block hashes, with cache_control kept
    "tools":  ["h7", "h8", ...],
    "messages": ["m1", "m2", ..., "m19"],
    "new_from": 17                   // index of the first message not seen earlier in this thread
  },
  "reply": {
    "id": "msg_...", "stop_reason": "tool_use",
    "content": [ {"type": "thinking"...}, {"type": "text"...}, {"type": "tool_use", "id": "toolu_..", "name": "Bash", "input": {...}} ],
    "usage": { "input_tokens": 12, "cache_creation_input_tokens": 900,
               "cache_read_input_tokens": 41000, "output_tokens": 380 }
  },
  "error": null                       // upstream error body, SSE `error` event, transport failure, or parse failure
}
```

### 4.2 Reply parsing

- Streaming replies: rebuild the message from `message_start`, `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta`, `thinking_delta`, `signature_delta`), `content_block_stop`, `message_delta` (stop reason, final usage), `message_stop`, and `error`. While a `tool_use` input is still streaming its JSON is incomplete, so show it raw until the block stops.
- Non-streaming JSON replies are parsed directly.
- Event types the parser doesn't know are kept and shown as raw events, never dropped.

### 4.3 Threads

Claude Code and Codex each send several separate conversations through `proxy.py`: the main agent loop, subagents, and small side calls (titles, summaries, token counts). Mixing them in one feed would make it unreadable, so each exchange is given a **thread**:

1. An exchange continues a thread when its `messages` start with the previous exchange's messages in that thread, with the same system prompt.
2. Otherwise it starts a new thread.
3. Each thread gets a label: *main* (the longest-lived thread with the most tools), *subagent N*, or *side call* (no tools or a single turn). The label can be renamed in the UI.

Threads never span terminals: two exchanges with different terminal IDs (§4.3.1) are always in different threads, even if their messages match.

`new_from` is worked out against the previous exchange in the same thread. If the prefix doesn't match (for example after the conversation is compacted), the exchange is marked **"history rewritten"** and the changed range is shown.

### 4.3.1 Terminal and agent attribution

Several Claude Code and Codex terminals can be watched at once, and each terminal can run subagents. Every thread, and so every conversation view, must say **which terminal** it came from and **which agent** sent it.

- **Terminal ID.** Taken from the client's session identifier. Claude Code: the `X-Claude-Code-Session-Id` header if present, otherwise the session part of `metadata.user_id`. Codex: the `session_id` header if present, otherwise `prompt_cache_key` in the body. The terminal chip also shows which app it is (`CC` or `CX`), so `T1 CC a1b2c3` and `T2 CX 9f8e7d` are never confused. It is shown shortened to its first 6 characters (`a1b2c3`) for readability, with the full ID on hover and in the detail. If two active terminals share the same 6 characters, both are lengthened until they differ. A request with no session identifier gets the terminal `?` and is still shown.
- **Terminal label.** Each terminal also gets a stable color and a short name (`T1`, `T2`, … in order of first appearance) that can be renamed in the UI, like thread labels.
- **Agent.** The agent that sent the exchange: *main*, *subagent N*, or *side call* (the thread label from §4.3). For a subagent, the thread is also linked to the tool call in the parent thread that launched it (an `Agent`/`Task` `tool_use` whose prompt matches the subagent's first user message). The subagent then shows its type and description from that call, e.g. `subagent 2 · Explore · "find auth code"`, plus a link "launched from #31 (main)". If no launching call is found, it shows as `subagent N · parent unknown`.
- Both are stored on every exchange (`terminal`, `agent`, §4.1), so they survive `--log` / `--load`.

### 4.4 Other traffic

If the path or body looks like neither the Anthropic Messages API nor the OpenAI Responses API, the exchange gets `kind: "other"`. It appears in the timeline and opens as pretty-printed JSON or text. Nothing more is parsed.

### 4.5 Codex (OpenAI Responses API)

Codex is a first-class agent, not "other" traffic. Its exchanges are parsed into the same internal model as Claude Code's, so every view, thread rule, diff, filter and statistic in this spec works for both. Only the parser differs.

- **Detection.** `POST …/responses` with a JSON body that has `input` (and usually `instructions`), or the `originator: codex_*` header, is Codex (`agent_app: "codex"`, `kind: "responses"`). `POST /v1/messages` is Claude Code. Detection is by request, so one `proxy.py` port may carry either.
- **Request mapping.**

  | Responses API (Codex) | Common model (as for Claude Code) |
  |---|---|
  | `instructions` | system prompt (one block) |
  | `input[]` items of type `message`, role `user` / `developer` | user messages (harness detection, D2, decides **You** vs **Harness**) |
  | `input[]` items of type `message`, role `assistant`, `output_text` | model text |
  | `reasoning` items (summary text; `encrypted_content` is never shown, only its size) | thinking |
  | `function_call` / `custom_tool_call` / `local_shell_call` (with `call_id`) | tool call |
  | `function_call_output` / `custom_tool_call_output` / `local_shell_call_output` | tool result, paired by `call_id` (§5.4) |
  | `tools[]` (`function` with `parameters`, plus built-in types such as `local_shell`, `web_search`) | tool definitions (built-in types are listed by type with no schema) |
  | `model`, `reasoning`, `store`, `include`, `parallel_tool_calls`, `prompt_cache_key`, … | request parameters |

  Codex sends the whole conversation in `input` on each request, so `new_from`, threads and "history rewritten" work as in §4.3.
- **Reply parsing.** The streamed events (`response.created`, `response.output_item.added` / `.done`, `response.output_text.delta`, `response.function_call_arguments.delta`, `response.reasoning_summary_text.delta`, `response.completed`, `response.failed`, `error`, …) rebuild the reply incrementally, like §4.2. Unknown event types are kept and shown raw, never dropped.
- **Usage and cost.** `input_tokens`, `input_tokens_details.cached_tokens` (shown as cache read), `output_tokens`, `output_tokens_details.reasoning_tokens`. `prices.json` holds OpenAI models as well; Codex on a ChatGPT subscription still shows the estimate, labelled as such.
- **Harness patterns.** The built-in list also covers Codex's injected context (for example `<environment_context>`, `<user_instructions>` and `AGENTS.md` contents sent as user or developer messages), overridable with `harness_patterns.json` like the rest.
- **Agents.** Codex threads are labelled *main* or *side call* by the rules in §4.3. If Codex runs sub-agents, they are labelled and linked to their launching call the same way as Claude Code subagents (§4.3.1); otherwise no subagent is invented.
- **Anything not understood** (an unknown body shape, a non-streaming variant, a WebSocket upgrade) is still recorded and shown raw with a "not parsed" badge, as in §6.

## 5. UI

### 5.1 Layout

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ● live  12 req · 1.2M in (94% cached) · 18k out · ~$0.84 · 3 errors  [⏸][⚙]│  header
├─────────────────────┬──────────────────────────────────────────────────────┤
│ filters / search    │  #42 · T1 a1b2c3 · main · claude-… · 200 · 9.2s      │
│ ─────────────────── │  ┌ What's new ─────────────────────────────────────┐ │
│ #42 main  ⟳ Bash ✓  │  │ ◧ tool result  Bash  (exit 0, 34 lines)  ▸      │ │
│ #41 main  ✎ Edit ✓  │  │ ◆ reply: "Tests pass. Next I'll…"  → Read ×2   │ │
│ #40 side  title     │  └─────────────────────────────────────────────────┘ │
│ #39 main  ✗ Bash    │  [Conversation] [System] [Tools 18] [Reply] [Raw]    │
│ …                   │  …                                                   │
├─────────────────────┴──────────────────────────────────────────────────────┤
│ stats drawer (collapsible)                                                 │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Left: timeline**, newest first. One row per exchange.
- **Right: detail** of the selected exchange.
- **Header:** session totals and connection state.
- **Bottom: stats** (§5.6), collapsible.
- Below about 900px wide the panes stack: the timeline becomes the main view, and the detail opens full screen with a back button.

### 5.2 Message kinds: the visual vocabulary

Every content block in the UI uses one consistent look per kind. The color and the icon/label are both always present, so the colors never carry the meaning alone. Colors are CSS variables with light and dark themes.

| Kind | Icon / label | Source |
|---|---|---|
| User prompt (typed by the human) | 👤 **You** | `user` text block that is not harness-injected |
| Harness-injected context | ⚙ **Harness** (muted) | `<system-reminder>`, hook output, and similar blocks inside `user` messages |
| Model text | ◆ **Model** | `assistant` text |
| Thinking | 💭 **Thinking** (muted, collapsed) | `thinking` / `redacted_thinking` |
| Tool call | ⟳ **Tool call · {name}** | `tool_use` |
| Tool result | ◧ **Result · {name}** | `tool_result` (the name comes from the matching call) |
| Error | ✗ red border | `tool_result.is_error`, HTTP ≥ 400, SSE `error`, transport failure |
| System prompt | 📜 **System** | `system` |

Telling the human's prompt apart from harness-injected text is important: in Claude Code both arrive as `user` messages. Detection uses a built-in list of patterns (tag names such as `system-reminder`, `command-name`, and so on) that can be overridden with `harness_patterns.json`. Text that matches no pattern is shown as **You** (D2). Each block has a "why is this marked harness?" tooltip that shows the rule that matched.

### 5.3 Timeline row

One line, with a second line on hover or when selected:

- `#id`, a terminal chip (`T1 a1b2c3`, colored per terminal), an agent chip (`main` / `subagent 2` / `side`, colored per thread), and the relative time;
- what triggered the request (the last new message): `👤 "fix the failing test…"` or `◧ Bash ✓`;
- the outcome: `◆ "Tests pass…"`, `⟳ Read ×2`, or `stop: end_turn`;
- small numbers: tokens in/out, latency, cost;
- a red left border and ✗ for any error; a spinner while the reply is still streaming.

Live behaviour:
- New rows are added at the top.
- If you have scrolled down or selected an older row, the view **doesn't move**. A "▲ 3 new" pill appears instead.
- **Follow mode** (on by default, `f` toggles it) keeps the newest exchange selected.
- **Pause** (⏸) freezes the view; events still buffer in the background.

### 5.4 Exchange detail

The detail header always shows the terminal (short ID and label) and the agent, with the subagent's launching call linked (§4.3.1). The Conversation view repeats this at its top, so a conversation is never shown without saying where it came from.

Sections in this order, each one clearly separated and labelled:

1. **What's new.** This is the default focus. It shows the messages from `new_from` onward (usually a tool result or a user prompt), the reply, and the things that changed in this request compared with the previous one in the thread (§5.5).
2. **Conversation.** The whole `messages` array as a chat transcript:
   - Earlier turns are collapsed into one "17 earlier messages ▸" bar that expands step by step.
   - **Each tool call is shown next to its result**, paired by `tool_use.id` / `tool_use_id`. The call appears in assistant turn *N*. Its result arrived in the request after it but is shown attached under the call, with a link "arrived in #43". A call whose result hasn't arrived yet shows "⧗ awaiting result".
   - Each message has an anchor, a copy button and a "raw JSON" toggle.
3. **System**, shown in full. Each system block is collapsible, shows its `cache_control` and size, and has a "changed since #N" badge.
4. **Tools.** A searchable list of tool definitions: the name, the first line of the description, and the parameter names. Expanding a tool shows its full description and its `input_schema` as a JSON tree. If the tool set changed since the previous request, the change is shown as a diff.
5. **Reply.** Content blocks in order (thinking, text, tool calls), `stop_reason`, and usage. While streaming, the text grows live and a tool call's input shows "receiving…".
6. **Request parameters.** `model`, `max_tokens`, `thinking`, `temperature`, `metadata`, the beta headers, and any other parameters.
7. **Raw.** The redacted request headers and body, the response headers, and the raw SSE events in order, all copyable.

### 5.5 "What changed since the previous request"

Compared with the previous exchange in the same thread:
- new messages (normally 1–2);
- system blocks added, removed or changed (shown as a line diff);
- tools added or removed, and changed descriptions or schemas;
- a different model or different parameters;
- history rewritten (compaction) or a cache hit-rate drop: a warning chip, because that is where cost jumps.

### 5.6 Statistics

All of these are drill-down entry points. Clicking a bar or a row filters the timeline.

- **Per request:** input tokens split into uncached, cache write and cache read; output tokens; time to first byte; total time; estimated cost.
- **Session:** totals; a cost-over-time line; cache hit rate.
- **Tool usage:** a table of tool name, calls, errors, mean and max time between the call and its result, and result size. Clicking a row filters the timeline to that tool.
- **Cost:** estimated from a price table per model family. Prices for both Anthropic and OpenAI models go in an editable `prices.json`, never hard-coded, and the cost is always labelled "estimate". Cache writes and reads are priced separately. A model missing from the table shows as "cost unknown", never as $0.

### 5.7 Filtering and search

- Filter chips: agent app (Claude Code / Codex), terminal, agent/thread, kind (user prompt / tool call / errors only / side calls hidden), tool name (multi-select), model, status.
- Full-text search across message text, tool inputs and results. Matches are highlighted, and `n`/`N` jump between them.
- Filters live in the URL hash, so a view can be bookmarked or reloaded.
- **Errors only** is one click (`e`). The header's error count is a link to it.

### 5.8 Formatting rules

- **Long text:** blocks over 12 lines (or 1,500 characters) collapse to a preview showing the first lines, a fade, and "show all (342 lines)". The expanded or collapsed state is kept per block while you navigate.
- **JSON:** a collapsible tree with syntax highlighting. It opens two levels deep, strings over 200 characters are truncated with an expand option, and there is "copy as JSON". Tool inputs that are strings holding JSON are parsed and shown as JSON.
- **Model text:** rendered as safe markdown (C5), with a raw toggle. Code fences use a monospace font, keep their whitespace, and scroll horizontally inside the block, never the whole page.
- **Tool results:** shown in monospace. Common shapes get light help: line-numbered file reads, `cat -n`-style output, and diff coloring for `+`/`-` lines.
- **Images** (`image` blocks): thumbnails that open at full size; base64 data is never dumped as text.
- **Sizes:** every collapsible block shows its size (lines, characters, and approximate tokens).
- **Times:** relative ("12s ago") with the absolute time on hover; durations in ms or s.

### 5.9 Keyboard

`j`/`k` next or previous exchange · `Enter` open · `[`/`]` previous or next message within an exchange · `/` search · `e` errors only · `f` follow · `p` pause · `r` raw · `?` help.

## 6. Robustness

- Requests that are invalid JSON, cut off, or not UTF-8 are still recorded and shown raw with a "could not parse" badge.
- **Capture gaps.** If packets are missed (for example under heavy load) and a connection cannot be reassembled, that exchange is shown with a "capture incomplete" badge and whatever was recovered, never silently dropped. Later requests in the same thread still show the full conversation, because each request carries all of it.
- If `proxy.py` is stopped and restarted, capture simply continues with the new connections; the dashboard does not need restarting.
- If the client disconnects partway through, the exchange is marked `aborted` together with whatever was received.
- Upstream errors (4xx/5xx, including `overloaded_error` and rate limits) show the error body prominently. `retry-after` and rate-limit headers appear in the detail.
- Memory: the store keeps the most recent 2,000 exchanges (configurable). Older ones stay in the JSONL log.
- Browser: the timeline only renders the rows on screen (virtualised), so it stays smooth with thousands of exchanges. Blobs are fetched lazily.
- SSE reconnects automatically with back-off. The header dot shows live, reconnecting or offline.

## 7. Acceptance criteria

1. With `python3 proxy.py` and one or more Claude Code agents **already running**, start `python3 dashboard.py` and open `http://localhost:8042`. The next request from each agent appears within 1s of being sent, with that agent's full conversation so far, and the reply text streams in live. Nothing is restarted.
2. With 50 or more exchanges, it takes one glance to say which exchange carried the human's prompt and which ones were tool loops, without opening anything.
3. For any tool call, its result is visible next to it, with a link to the request it arrived in.
4. The full system prompt and every tool definition can be read in full, and changes between requests are flagged.
5. Filtering on one tool name, or on errors only, gives the right subset, and the filter is in the URL.
6. `proxy.py` is unchanged (`git diff` on it is empty), and forwarding is unchanged: the same output for the same traffic, and no measurable delay added to time to first byte (< 5 ms) whether the dashboard is running, stopped, or crashed.
7. No credential appears in the store, the log, the API responses or the page.
8. `--load traffic.jsonl` replays a saved session in the same UI.
9. With two terminals running at once, each running a subagent, every timeline row and every conversation view shows the correct short terminal ID and the correct agent, and each subagent links to the call in its parent that launched it.
10. With Claude Code and Codex **both already running**, each through its own `proxy.py`, one dashboard shows both. Codex exchanges get the same views as Claude Code ones: You vs Harness, tool calls paired with their results, system prompt and tools, what's new, threads, terminal ID and stats.

## 8. Milestones

The initial build starts with a **very basic version that works end to end**, then adds the requirements of this spec gradually. Each step starts from a working dashboard, adds one slice of the spec, and ends with a working, tested dashboard. No step needs a later step to be useful.

0. **Basic version.** Traffic is recorded (C2–C4 apply from the first line of code) and the dashboard page lists exchanges live, newest first: `#id`, time, model, status. Clicking a row shows the redacted request and response as plain, escaped text (C5). No parsing, no deduplication, no threads, no styling beyond readable defaults. Acceptance: criteria 6 and 7 (§7) already hold.
1. **Recorder + store + JSONL:** deduplication (§3.1), incremental SSE parsing (§4.2), `--log` / `--load` (criterion 8).
2. **Dashboard API + live SSE + timeline + raw view** (§3.2, §5.1, §5.3; criterion 1).
3. **Parsed detail:** message kinds, the conversation view, tool call/result pairing (§5.2, §5.4; criteria 2–4).
4. **Threads + "what's new" + diffs** (§4.3, §5.5).
5. **Terminal and agent attribution** (§4.3.1; criterion 9).
6. **Codex support** (§4.5; criterion 10): the Responses API parser feeding the same common model. Once this step is done, every later step is checked against both Claude Code and Codex traffic.
7. **Stats, filters, search, keyboard, polish** (§5.6–§5.9; criterion 5), then robustness (§6).

Order rule: when a later requirement turns out to be needed earlier, add the smallest version of it at that point rather than building it in full ahead of time.

## 9. Decisions

Where there was a choice, the safest option was taken.

| # | Question | Decision | Why it is the safe choice |
|---|---|---|---|
| D1 | Dashboard on the API port, or on its own port? | **Its own process and its own port** (`dashboard.py`, `--dash-port`, default 8042, bound to `127.0.0.1`). The API port carries only API traffic. | No dashboard path can clash with or intercept an API path, and `proxy.py` stays untouched (C6). |
| D2 | Patterns for spotting harness-injected text: hard-coded or configurable? | **Built-in defaults, overridable** with an optional `harness_patterns.json`. A block that matches no rule is shown as **👤 You**. Every harness label shows the rule that matched it. | A wrong match can never hide a real human prompt behind the muted style, and every classification can be checked. |
| D3 | JSONL log on by default? | **Off by default.** It is written only with `--log PATH`, using file mode `0600`, with credentials already redacted (C4). On startup the terminal prints a warning that the log contains prompts and file contents. | Nothing sensitive is written to disk unless you ask for it. |
| D4 | "Export thread as Markdown"? | **Not in v1.** Copy buttons on single messages (§5.4) are enough. | Avoids an easy way to move whole sessions (code, secrets in tool output) out of the local tool. It can come back later with redaction built in. |
| D5 | How does the dashboard see traffic without changing `proxy.py` or restarting agents? | **Passive loopback capture** of port 8041 (§3). A relay is optional and never required. | It is the only option that reaches agents already pointed at `proxy.py`, and as an observer of packet copies it cannot break or slow forwarding. The cost is needing `CAP_NET_RAW`, which is asked for explicitly. |
| D6 | Separate Codex views, or one common model? | **One common internal model** with one parser per API (§4.5). | Every readability feature is built once and works for both agents, and a Codex exchange can never be shown with fewer safety checks (C4, C5) than a Claude Code one. |
