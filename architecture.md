# Loop — Architecture

_Last updated: 2026-07-06 (v0.5.0: Jira auto-triage + MCP env forwarding fix). Keep this current; it is the cross-session source of truth for how Loop is wired._

Loop is a Slack-native AI agent built on [Strands Agents](https://strandsagents.com/).
When a user `@Loop`-mentions the bot or runs `/loop`, a single Strands agent
thinks, optionally calls a tool, and replies in-thread. There is **one brain**
(the agent) — no intent parser, no command router, no extraction pipeline.

## Runtime flow

```
Slack (@mention, DM, or /loop)
        │
        ▼
slack_bolt.App × N  (Socket Mode, slack_app.py — _discover_apps() builds one per
                     configured app: bare SLACK_BOT_TOKEN/SLACK_APP_TOKEN, and/or
                     SLACK_BOT_TOKEN_<NAME>/SLACK_APP_TOKEN_<NAME> pairs; each its
                     own socket thread, each tags telemetry "<name>:<entrypoint>")
   • app_mention  → three-stage shortcut check before the agent:
       1. BUG DETECTION: if _looks_like_bug(text) and JIRA_API_TOKEN set →
          _extract_bug_details() (focused LLM call → JSON) → save to _pending[thread_ts]
          → post confirmation message → return  (agent NOT called)
       2. JIRA CONFIRM: if text matches YES_RE and thread_ts in _pending →
          _create_jira_issue_direct() → resolves Slack mention to Jira assignee → return
       3. AGENT: all other messages → run_agent(..., app_name) → say() in thread
   • message      → log channel/user (tracking); answer only if DM (incl. file uploads)
   • /loop        → ack() → run_agent(...) → respond()
   • _collect_files(): download event["files"] via url_private_download + bot token
     (needs files:read); allow image/* + text/* + json, size-capped
   • UX: 👀 reaction on receipt → ✅ on success / ⚠️ on error
        │  wraps every call in observability.record(Interaction)
        ▼
loop.agent.run(prompt, attachments=…, context=…, request_id=…)   (agent.py)
   • prepend a [context] line (who/where) so tools can act
   • _build_message(): no files → str; else Strands content list
     [{text}, {image:…} (M3 multimodal) / inlined text for text/code/json/pdf(pypdf)]
   • set RequestState in a ContextVar (loop/context.py)
   • if LOOP_MCP_SERVERS set: enter MCPClient stdio context, merge its tools
        │
        ▼
Strands Agent
   • model       = AnthropicModel(client_args={api_key, base_url?}, model_id, max_tokens=2048)
   • system_prompt = Loop persona (Slack mrkdwn, concise, tool guidance)
   • tools       = [slack, slack_send_message, calculator, think,
                    jira_stage_ticket, jira_create_issue, jira_search_issues] (+ MCP tools)
   • memory      = MemoryManager(stores=[_memory_store()], injection=provenance fmt)
                   _memory_store(): sqlite FTS5 (default) | hybrid libSQL+vectors (RRF)
   • hooks       = [LoopTracer, Guardrails]   callback_handler = reasoning_callback
        │           │
        │           ├─ LoopTracer  → log + persist every model call / tool call / reasoning (steps table)
        │           └─ Guardrails  → cancel_tool on limit/repeat/risky-method/token breaches
        ▼
agent(prompt) → AgentRun(text + tokens + tool_calls + model_calls + guardrail_hits + reasoning)
        │
        ▼
final text → Slack thread;  one JSON telemetry line + interactions row recorded
```

## Components

| File | Responsibility |
|---|---|
| `loop/main.py` | Entrypoint (`loop` script). Loads `.env`, configures logging, calls `slack_app.start()`. |
| `loop/slack_app.py` | Slack Bolt app(s) over **Socket Mode**. `_discover_apps()` + `start()` run **one or many** apps concurrently (one socket thread each). Three handlers (`app_mention`, `message`, `/loop`); `message` tracks all channels + answers DMs (incl. file uploads). `_collect_files()` downloads attachments (`files:read`). Wraps every call in `observability.record` (entrypoint = `<app>:<kind>`). |
| `loop/agent.py` | Builds **one Strands `Agent` per app** (`get_agent(app_name)`), each with its own system prompt via `_system_prompt()` (base + optional `LOOP_PERSONA_<NAME>`). `run(prompt, attachments=…, app_name=…)` builds a string or multimodal content list via `_build_message()`, returns an `AgentRun` (text + telemetry), manages the per-request `RequestState`. `_split_reasoning()` strips MiniMax-M3's inline `<think>…</think>` deliberation out of the reply (it arrives as plain text, not an Anthropic reasoning block) → Slack sees the clean answer, the thinking goes to the `reasoning` column. |
| `loop/context.py` | `RequestState` + a `ContextVar` so the hooks can attribute steps and enforce per-request limits across one `run()`. |
| `loop/tracing.py` | `LoopTracer` (Strands `HookProvider`) + `reasoning_callback`: logs & persists each model call, tool call, and reasoning chunk. |
| `loop/guardrails.py` | `Guardrails` (`HookProvider`): enforcing tool-call limits via `cancel_tool`, with per-rule env toggles. |
| `loop/observability.py` | `Interaction` + `record()` (edge telemetry), `record_step()`, `record_eval()`; owns the `interactions` / `steps` / `eval_results` tables. The `interactions` row now also stores the per-turn `reasoning` (stripped `<think>`) and `response` (the reply we sent) — added by an idempotent column migration. |
| `loop/eval.py` | `loop-eval` CLI: runs `evals/golden.json` through the real agent and scores tool selection + reply expectations. |
| `loop/ambient.py` | **Ambient (proactive PM) mode** — off unless `LOOP_AMBIENT=on`. `maybe_start(configs)` (called from `slack_app.start()`) spawns one daemon thread per app. Each tick discovers stalled threads (pluggable `Discoverer`; default `discover_from_interactions` = threads the app touched whose last non-ambient activity is older than `LOOP_AMBIENT_STALE_HOURS`), re-checks real staleness via `conversations_replies`, skips recently-nudged threads, runs the agent (optionally on `LOOP_AMBIENT_MODEL`) to draft a nudge or `NOOP`, and posts via the **per-app** Web client (avoids the bare-`SLACK_BOT_TOKEN` identity of `slack_send_message`). Wrapped in `obs.record` with entrypoint `<app>:ambient`. Ambient never stages/creates Jira tickets — it may only *suggest* one in words; a human must ask. |
| `loop/jira_tool.py` | Native Jira tools: `jira_stage_ticket` (saves to in-process `_pending` dict, returns confirmation text), `jira_create_issue` (REST API create), `jira_search_issues`. `_create_jira_issue_direct` is the non-tool version called by `slack_app.py` on confirmation. **Project resolution** (`resolve_project_key`): a named project (key or plain name like "BAN") is matched against the live project list; unknown → falls back to `JIRA_PROJECT_KEY` (never 400s), and cannot create projects/spaces. **Assignee resolution** (`_resolve_assignee_email`): explicit email → `<@U…>` mention (`users.info`) → **bare name** ("Jyoti") matched against the workspace member list (`users.list`, unique exact-or-first-name) → email → Jira `user/search` → `accountId`; no unique match ⇒ filed unassigned and the reply says so. No follow-up questions. Bot IDs excluded from mention extraction. |
| `loop/action_items.py` | Detects commitments ("John will fix X by Friday") on observe-only channel messages (regex gate → focused model extraction of owner/task/deadline), stores them in the `action_items` table, and feeds ambient deadline nudges. |
| `loop/memory_capture.py` | **Passive long-term memory capture** (off unless `LOOP_MEMORY_CAPTURE=on`). On channel messages Loop only observes, a regex gate (`looks_memorable`, decisions + reused action-item patterns) → a focused model call distills a durable decision/commitment into one sentence and saves it to the long-term store with provenance — so recall works without an explicit `@Loop` turn (gaps B/I/K). |
| `loop/storage.py` | `SqliteMemoryStore` (default backend) — Strands `MemoryStore` over local SQLite. **Episodic + provenance-stamped:** every memory auto-tagged with who/where/when (from `RequestState`); recall is **FTS5** (BM25 + recency) with a sanitized query + `LIKE` fallback; exact-content dedup on write. |
| `loop/vector_store.py` | `HybridMemoryStore` (opt-in, `LOOP_MEMORY_BACKEND=hybrid`) — same contract over **libSQL/Turso**: fuses FTS5 (BM25) + native vector search (`F32_BLOB`/`vector_top_k`) via **Reciprocal Rank Fusion** for hybrid Agentic-RAG recall. Same provenance + dedup. |
| `loop/embeddings.py` | Pluggable text embedder for the hybrid store: `fastembed` (local, no key — default), `minimax`/`openai` (hosted), `hashing` (dep-free fallback). |
| `loop/__init__.py` | Version marker (`0.3.1`). |

## Model provider — env-selected factory (no vendor lock-in)

`agent._build_model()` is a **provider factory** chosen by `LOOP_MODEL_PROVIDER`
(default `anthropic`). This is Loop's core differentiator vs single-vendor Slack
agents: the brain is swappable by env, never hardcoded.

- `anthropic` (default) — Strands `AnthropicModel` (the Anthropic SDK). Also serves
  any **Anthropic-wire-compatible** endpoint (MiniMax M3, native Claude, proxies)
  via `ANTHROPIC_BASE_URL`. Reads `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`.
- `litellm` — LiteLLM multi-provider gateway (one class, many providers; model id
  like `openai/gpt-4o`). `openai` — OpenAI / OpenAI-compatible. `bedrock` — AWS
  Bedrock (works out of the box; boto3 ships with strands). `ollama` — local /
  self-hosted (sovereign). `gemini` — Google Gemini.
- Non-`anthropic` SDKs are **lazy-imported** (only required when selected); install
  via `pip install "loop[providers]"`. Model id is provider-neutral via `LOOP_MODEL`
  (falls back to `ANTHROPIC_MODEL`); response cap via `LOOP_MAX_TOKENS` (def 2048).
- **Model routing:** `_build_model(model_id)` / `get_agent(app, model_id)` /
  `run(..., model_id=…)` accept an override (cached under its own key) so ambient
  background scans can run on a cheaper model (`LOOP_AMBIENT_MODEL`) — the cost lever.

**Current target: MiniMax M3** (the default `anthropic` path), env:
`ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`, `ANTHROPIC_MODEL=MiniMax-M3`,
key in `ANTHROPIC_API_KEY`. Note: only Anthropic-wire-compatible providers are
truly env-only; OpenAI/Bedrock/etc. go through the factory branches above.

## Database (storage layer)

**Engine:** local **SQLite** (`sqlite3` stdlib), file at `DATABASE_PATH`
(default `./data/loop.db`). No external DB/service. WAL journal mode; a
`threading.Lock` guards writes because the connection is shared across the Bolt
handler threads (`check_same_thread=False`).

**Schema (tables):**

```sql
-- long-term memory (loop/storage.py) — episodic + provenance-stamped
CREATE TABLE memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    metadata TEXT,                      -- full provenance dict (JSON)
    kind TEXT, author TEXT, channel TEXT, team TEXT, source TEXT, thread_ts TEXT,
                                        -- who/where/which-app, promoted to columns for scoping
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
-- FTS5 mirror for ranked recall; kept in sync by an AFTER INSERT trigger.
CREATE VIRTUAL TABLE memory_fts USING fts5(
    content, content='memory_entries', content_rowid='id');

-- telemetry / data foundation (loop/observability.py)
CREATE TABLE interactions (             -- one row per Slack interaction (edge)
    id, request_id, ts, entrypoint, team, channel, channel_type, "user",
    thread_ts, prompt, model_id, input_tokens, output_tokens, total_tokens,
    tool_calls, model_calls, guardrail_hits, latency_ms, outcome, error,
    reasoning,        -- the model's <think> deliberation, stripped from the reply
    response );       -- the final reply text we sent back (auditable, was unstored)
CREATE TABLE steps (                    -- one row per model call / tool call / reasoning / guardrail
    id, request_id, ts, seq, kind, name, detail, tokens, duration_ms, outcome );
CREATE TABLE eval_results (             -- one row per golden case per loop-eval run
    id, run_id, ts, case_id, category, model_id, passed, latency_ms, tool_calls, reasons );
```

All telemetry tables are opened on a separate connection from the memory store
(rollback/`DELETE` journal mode — keeps `loop.db` self-contained + openable in
external SQLite viewers; single process, so WAL concurrency isn't needed) and
created/migrated lazily on first write.

**How memory is used:**
- `SqliteMemoryStore.add(content, metadata)` — auto-stamps provenance from the
  active `RequestState` (author/channel/team/source/thread_ts) into columns +
  the JSON blob, then INSERTs. The `add_memory` *tool* only lets the model pass
  content, so this is **where who/where/when gets attached** — the agent never
  has to (and can't) supply it.
- `SqliteMemoryStore.search(query)` — FTS5 `MATCH` ranked by `bm25()` then
  recency, capped at `max_search_results` (5). The query is reduced to safe
  prefix-OR tokens (`_fts_match`) — stopwords dropped, no FTS5 syntax injection;
  empty → `LIKE` fallback. Returns `MemoryEntry` carrying the provenance metadata.
- **Injection format** (`agent._format_memories`) — recalled memories are
  injected before each model call as a `<memory>` block where each `<entry>`
  carries `from="@user" in="#channel" when="date"`, so the model can weigh and
  cite provenance. (The Strands default format drops metadata; this restores it.)
- `writable=True`, `extraction=False` — the agent is the filter: it decides what
  is worth persisting via `add_memory` (system prompt steers it to save
  decisions/facts/preferences/episodes, skip chit-chat); nothing is auto-extracted.

**Memory backends (env `LOOP_MEMORY_BACKEND`):**
- `sqlite` (default) — FTS5 lexical recall, zero deps, no API key.
- `hybrid` (aliases `libsql`/`turso`/`vector`) — libSQL/Turso **hybrid** recall:
  FTS5 + semantic vectors fused with RRF (`loop/vector_store.py`); needs
  `pip install -e ".[vector]"` + embeddings (`fastembed` local, no key by
  default). Verified live (memory add→recall through MiniMax M3). See
  `docs/vector-memory.md`.

**Memory scope (per-channel isolation, Claude-Tag-style):** `LOOP_MEMORY_SCOPE`
(default `channel`; `team`/`global`; per-app `LOOP_MEMORY_SCOPE_<NAME>`) controls
what recall can see. A fact saved in #support is not recalled in #data-team. It's a
**recall-time filter** (no migration — channel/team are already stamped): the active
scope is read from `reqctx.memory_scope()` *inside* each store's `search()` (Strands'
`MemoryManager` forwards only `max_search_results`, so scope can't be passed through
it). SQLite store adds `AND channel = ?` to FTS/LIKE; the hybrid store scopes the
lexical leg in SQL and **post-filters the semantic leg** (`vector_top_k` can't take a
WHERE). Dedup is scoped to match (same fact in two channels = two rows under `channel`).

**Remaining data-layer gaps (see `progress.md` Pillar 3):**
- Dedup is exact-content only; near-duplicate (semantic) dedup is a future step.
- Scope is a single-tenant filter; per-workspace **DB** isolation is enterprise-track.

## Jira auto-triage (Gap F — manual data movement)

`slack_app.py` runs a deterministic shortcut **before** the agent for bug reports
(MiniMax M3 does not reliably call tools proactively from system-prompt instructions):

1. **Detection** — `_looks_like_bug(text)` regex matches crash/error/fail/etc.
2. **Extraction** — `_extract_bug_details()` makes a focused single-turn LLM call
   (no tools, just JSON completion) → `{summary, description, priority, slack_mention}`.
3. **Staging** — details saved to `_pending[thread_ts]` (in-process dict).
4. **Confirmation** — bot posts the details and asks "@Bot yes to confirm".
5. **Creation** — `@Bot yes` reply triggers `_create_jira_issue_direct()` which:
   - Calls `_slack_email_from_user_id()` to resolve `<@UXXXXXXX>` → email
   - Calls Jira `user/search` → `accountId`
   - POSTs to Jira REST API `/rest/api/3/issue` with assignee

Env vars: `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `TOOLSETS=jira_issues`.
Each app's own bot user id is auto-registered at startup (`auth.test` →
`loop.llm.register_bot_id`) and excluded from mention extraction so the bot
doesn't assign tickets to itself — nothing workspace-specific is hardcoded
(`LOOP_BOT_USER_IDS` can seed extra ids).

## Extensibility — MCP tools at runtime

Set `LOOP_MCP_SERVERS="<command> <args...>"` (a stdio command). On each request
the agent enters the `MCPClient` context, loads that server's tools, and exposes
them alongside the built-ins. Note: when MCP is active, the agent is **rebuilt
per request** (the cached agent is only used on the no-MCP path).

**Env forwarding fix (2026-07-05):** `_mcp_client()` now passes `env=os.environ.copy()`
to `StdioServerParameters` so MCP server subprocesses inherit credentials
(`JIRA_*`, `BRAVE_API_KEY`, etc.) from the parent process. Without this, MCP
servers that read credentials from env (like `mcp-atlassian`) returned zero tools.

## Harness (observability · guardrails · evals)

- **Observability (Pillar 02):** every interaction emits a JSON telemetry line +
  an `interactions` row; every model call, tool call, and reasoning chunk emits a
  `loop.trace` line + a `steps` row. `LOOP_TRACE=off` silences logs (DB still
  written); `LOG_LEVEL=DEBUG` adds full reasoning + tool I/O. Logger names:
  `loop.obs`, `loop.trace`, `loop.guard`.
- **Guardrails (Pillars 04/05):** `Guardrails` cancels a tool call before it runs
  when a per-request limit trips — max tool calls, repeated identical call, risky
  Slack method, or token budget — each independently toggled by env. Enforcing by
  default; the model receives the cancel reason and replies gracefully.
- **Evaluation (Pillar 01):** `loop-eval` runs `evals/golden.json` through the
  real agent, scores tool selection + reply content, prints a scorecard, persists
  `eval_results`, and exits non-zero on regression (CI gate).

## What Loop does NOT have yet

No authn/authorization (allow-list of workspaces/users), no PII redaction before
persistence, no rate limiting across requests, no thread/conversation history
passed to the agent (each turn is stateless apart from long-term memory), and no
export of traces to an external OTEL backend. These map to the deferred items in
`progress.md`.
