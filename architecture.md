# Cairn — Architecture

_Last updated: 2026-08-27. Keep this current; it is the cross-session source of
truth for how Cairn is wired. Read `DIRECTION.md` first — it says what Cairn is
**for**, which is what most of the design below is answering to._

Cairn is a Slack-native AI agent built on [Strands Agents](https://strandsagents.com/)
whose job is **organisational memory**: decisions get made in Slack and lost in
Slack, and Cairn makes them answerable — with a source.

When a user `@Cairn`-mentions the bot or runs `/cairn`, a single Strands agent
thinks, optionally calls a tool, and replies in-thread. There is **one brain**
(the agent) — no intent parser, no command router, no extraction pipeline.

Three things run *without* being addressed: passive capture distils decisions
from conversations Cairn merely observes; resurfacing offers a past decision when
a settled question is re-opened; and the decision log answers questions about the
record without a model call at all.

## Runtime flow

```
Slack (@mention, DM, /cairn, or a message Cairn merely observes)
        │
        ▼
slack_bolt.App × N  (Socket Mode, slack_app.py — _discover_apps() builds one per
                     configured app: bare SLACK_BOT_TOKEN/SLACK_APP_TOKEN and/or
                     SLACK_BOT_TOKEN_<NAME>/SLACK_APP_TOKEN_<NAME> pairs; each its
                     own socket thread, each tags telemetry "<name>:<entrypoint>")
   │
   │  Every handler returns immediately and hands the work to a thread pool
   │  (_dispatch, CAIRN_ASYNC). Socket Mode wants an ack within ~3s and an agent
   │  turn takes longer, so running inline made Slack re-deliver the event and
   │  the user got the same answer twice.
   │
   • app_mention  → shortcut checks before the agent:
       1. BUG DETECTION: _looks_like_bug(text) and JIRA_API_TOKEN set →
          _extract_bug_details() (focused LLM call → JSON) → find_duplicate()
          → either "already tracked: KEY" or stage in _pending[thread_ts] → return
          (agent NOT called — the model does not reliably call the tool itself)
       2. JIRA CONFIRM: text matches YES_RE and thread has a staged ticket →
          _create_jira_issue_direct(); an assignee/priority named in the
          confirmation ("yes assign to jyoti") overrides what was staged
       3. ISSUE UPDATE: "set KAN-25 to high priority" → _update_issue_direct()
       4. AGENT: everything else → run_agent(..., app_name) → say() in thread
   • message (observe-only, not a DM):
       – action items ("John will fix X by Friday") → action_items
       – passive capture: gate on the message, distil the whole CONVERSATION
       – else resurfacing: a re-opened question may get its past decision back
   • message (DM) → same agent path, plus attachments
   • /cairn        → a question ABOUT the record is answered straight from the
                     decision log (no model call); anything else goes to the agent
   • _collect_files(): download event["files"] via url_private_download + bot token
     (needs files:read); images/text/json/PDF, size-capped
   • UX: 👀 on receipt → ✅ on success / ⚠️ on error
        │  wraps every call in observability.record(Interaction)
        ▼
conversation.read_context()   ← the shared reader (cairn/conversation.py)
   • in a thread  → that thread's recent turns (from the thread PARENT ts)
   • not in one   → recent channel messages (people @-mention in the channel and
                    ask the follow-up there too; without this that path got
                    nothing and the model spent ~3x the tokens reconstructing it)
   • bounded by turn count AND a char cap; a missing scope degrades to no
     context, never a failed reply
        │
        ▼
cairn.agent.run(prompt, attachments=…, context=…, request_id=…)   (agent.py)
   • prepend a [context] line (who/where) + the conversation so far
   • _build_message(): no files → str; else Strands content list
     [{text}, {image:…} / inlined text for text/code/json/pdf(pypdf)]
   • set RequestState in a ContextVar (cairn/context.py)
   • if CAIRN_MCP_SERVERS set: enter MCPClient stdio context, merge its tools
        │
        ▼
_invoke(agent, message)   ← retries + a clean slate
   • agent.messages = [] before each turn, under a lock. A cached Strands Agent
     accumulates messages, and there is one per app — without this, every user's
     turns from every channel fed into every later request.
   • transient failures (429 / 5xx / timeouts) retried with backoff, honouring
     the provider's own retryDelay hint; an exhausted DAILY quota fails fast
     (QuotaExhaustedError) because waiting cannot help
        │
        ▼
Strands Agent
   • model       = provider factory (CAIRN_MODEL_PROVIDER) — see below
   • system_prompt = Cairn persona: cite who/where/when; an answer without a
                     source is a rumour
   • tools       = env-selected groups (CAIRN_TOOLS): slack, jira, calculator,
                   decisions, think (+ MCP tools). Memory tools always on.
   • memory      = MemoryManager(stores=[_memory_store()], injection=provenance fmt)
   • hooks       = [CairnTracer, Guardrails]   callback_handler = reasoning_callback
        │           │
        │           ├─ CairnTracer → persist every model call / tool call / reasoning
        │           └─ Guardrails  → cancel_tool on limit/repeat/risky-method/token breaches
        ▼
agent(prompt) → AgentRun(text + tokens + tool_calls + model_calls + guardrail_hits + reasoning)
        │
        ▼
final text → Slack thread;  one JSON telemetry line + interactions row recorded
```

## Writes into memory

Every path that stores a memory goes through one store `add()`, which is why the
two rules below can be enforced once rather than per caller:

```
add_memory (agent tool) ─┐
remember() (explicit)    ├──►  store.add()  ──►  redact()  ──►  embed  ──►  dedup /
passive capture          │                       (secrets)                 supersede
backfill                 ┘                                                   │
                                                                             ▼
                                                          memory_entries (live rows only
                                                          are ever recalled; retired rows
                                                          keep superseded_by for audit)
```

* **Redact before embedding.** The vector is derived from the text, so stripping a
  key afterwards would leave it encoded in the embedding.
* **Rank is not relevance.** Dedup and supersession compare by distance, and the
  ambiguous band is settled by one model call, cached per pair of texts.

## Components

| File | Responsibility |
|---|---|
| `cairn/main.py` | Entrypoint (`cairn` script; `loop` kept as an alias). Loads `.env`, configures logging, calls `slack_app.start()`. |
| `cairn/slack_app.py` | Slack Bolt app(s) over **Socket Mode**. `_discover_apps()` + `start()` run **one or many** apps concurrently (one socket thread each). Handlers for `app_mention`, `message` and `/cairn` (`/loop` aliased); `message` tracks all channels, answers DMs, and drives passive capture + resurfacing. Every handler returns immediately via `_dispatch` (thread pool) so Socket Mode is acked inside its ~3s window. Checks Jira auth at startup, because Jira fails quietly. `_collect_files()` downloads attachments (`files:read`). Wraps every call in `observability.record` (entrypoint = `<app>:<kind>`). |
| `cairn/agent.py` | Builds **one Strands `Agent` per app** (`get_agent(app_name)`), each with its own system prompt via `_system_prompt()` (base + optional `CAIRN_PERSONA_<NAME>`). `run(prompt, attachments=…, app_name=…)` builds a string or multimodal content list via `_build_message()`, returns an `AgentRun` (text + telemetry), manages the per-request `RequestState`. `_enabled_tools()` reads `CAIRN_TOOLS`, because every tool's schema is re-sent on every request — an unused tool is a permanent latency and cost tax (`think` alone was ~2.1k tokens). `_invoke()` owns retries and resets `agent.messages` per turn. `_split_reasoning()` strips MiniMax-M3's inline `<think>…</think>` deliberation out of the reply (it arrives as plain text, not an Anthropic reasoning block) → Slack sees the clean answer, the thinking goes to the `reasoning` column. |
| `cairn/context.py` | `RequestState` + a `ContextVar` so the hooks can attribute steps and enforce per-request limits across one `run()`. |
| `cairn/tracing.py` | `CairnTracer` (Strands `HookProvider`) + `reasoning_callback`: logs & persists each model call, tool call, and reasoning chunk. |
| `cairn/guardrails.py` | `Guardrails` (`HookProvider`): enforcing tool-call limits via `cancel_tool`, with per-rule env toggles. |
| `cairn/observability.py` | `Interaction` + `record()` (edge telemetry), `record_step()`, `record_eval()`; owns the `interactions` / `steps` / `eval_results` tables. The `interactions` row now also stores the per-turn `reasoning` (stripped `<think>`) and `response` (the reply we sent) — added by an idempotent column migration. |
| `cairn/eval.py` | `cairn-eval` CLI: runs `evals/golden.json` (35 cases) through the real agent, scoring tool selection + reply expectations. `--resume` continues a run (appending to the same `run_id`, skipping only passes) and `--model` overrides the model, because a free-tier quota of 20/day/model cannot finish ~40 calls in one sitting. An exhausted quota halts the run rather than marking the remainder failed. |
| `cairn/ambient.py` | **Ambient (proactive PM) mode** — off unless `CAIRN_AMBIENT=on`. `maybe_start(configs)` (called from `slack_app.start()`) spawns one daemon thread per app. Each tick discovers stalled threads (pluggable `Discoverer`; default `discover_from_interactions` = threads the app touched whose last non-ambient activity is older than `CAIRN_AMBIENT_STALE_HOURS`), re-checks real staleness via `conversations_replies`, skips recently-nudged threads, runs the agent (optionally on `CAIRN_AMBIENT_MODEL`) to draft a nudge or `NOOP`, and posts via the **per-app** Web client (avoids the bare-`SLACK_BOT_TOKEN` identity of `slack_send_message`). Wrapped in `obs.record` with entrypoint `<app>:ambient`. Ambient never stages/creates Jira tickets — it may only *suggest* one in words; a human must ask. |
| `cairn/jira_tool.py` | Native Jira tools: `jira_stage_ticket` (saves to the SQLite-backed `_pending` store), `jira_create_issue`, `jira_search_issues`. `find_duplicate()` is the single dedup entry point (semantic, keyword fallback); `_related_decision()` attaches a genuinely relevant decision with its source; `check_auth()` reports a dead token at startup. `_create_jira_issue_direct` is the non-tool version called by `slack_app.py` on confirmation. **Project resolution** (`resolve_project_key`): a named project (key or plain name like "BAN") is matched against the live project list; unknown → falls back to `JIRA_PROJECT_KEY` (never 400s), and cannot create projects/spaces. **Assignee resolution** (`_resolve_assignee_email`): explicit email → `<@U…>` mention (`users.info`) → **bare name** ("Jyoti") matched against the workspace member list (`users.list`, unique exact-or-first-name) → email → Jira `user/search` → `accountId`; no unique match ⇒ filed unassigned and the reply says so. No follow-up questions. Bot IDs excluded from mention extraction. |
| `cairn/action_items.py` | Detects commitments ("John will fix X by Friday") on observe-only channel messages (regex gate → focused model extraction of owner/task/deadline), stores them in the `action_items` table, and feeds ambient deadline nudges. |
| `cairn/memory_capture.py` | **Passive capture** (off unless `CAIRN_MEMORY_CAPTURE=on`). A permissive gate runs on the triggering message (`looks_memorable`: stated decisions, `let's <anything>`, decision verbs, short assent like `+1`/`sgtm`, minus a hedge guard for "we could…"), then the **whole conversation** is distilled — the decision is the last line and the reasoning is in the lines before it. A cooldown stops an active thread being re-distilled per message. Cairn's own messages never trip the gate: its replies are full of decision language because it reports decisions back, and capturing those would feed its output into its own memory. |
| `cairn/storage.py` | `SqliteMemoryStore` (default backend) — Strands `MemoryStore` over local SQLite. **Episodic + provenance-stamped:** every memory auto-tagged with who/where/when (from `RequestState`); recall is **FTS5** (BM25 + recency) with a sanitized query + `LIKE` fallback; exact-content dedup on write. |
| `cairn/vector_store.py` | `HybridMemoryStore` (`CAIRN_MEMORY_BACKEND=hybrid`) — same contract over **libSQL/Turso**: FTS5 (BM25) + native vector search (`F32_BLOB`/`vector_top_k`) fused via **Reciprocal Rank Fusion**. Also owns **dedup and supersession** on write: the closest live neighbours are compared, a duplicate collapses into the existing row (keeping the richer wording) and a replacement retires the old one via `superseded_by`. Chain-aware, because decisions chain (24h → 4h → 8h) and comparing only against the closest leaves the *original* live. `_fetch` — the one choke point both retrieval legs pass through — skips retired rows. |
| `cairn/embeddings.py` | Pluggable text embedder for the hybrid store: `fastembed` (local, no key — default), `minimax`/`openai` (hosted), `hashing` (dep-free fallback). |
| `cairn/conversation.py` | The **shared reader**. `to_turns()` normalises Slack messages (strip mentions, drop join/leave noise, label Cairn as `you`, bound by turns + chars); `read_thread()` / `read_channel()` fetch; `read_context()` routes. Context, capture and backfill all read through it, so a captured memory always corresponds to a conversation Cairn could also have read. |
| `cairn/decisions.py` | The **decision log**: a sourced, chronological listing read straight from the store — deterministic, cannot invent an entry, and works when the model quota is spent. Understands time windows ("last week", "past 3 days"), which semantic search cannot answer because relevance has nothing to do with recency. Exposed as the `decision_log` tool and behind `/cairn`. |
| `cairn/resurface.py` | **Proactivity, earned** (off unless `CAIRN_RESURFACE=on`). A re-opened question that a *live* decision answers gets that decision back, in-thread, with its source. Fires only on questions (a decision being made is capture's job), only on a strong match at a tighter threshold than recall's (nothing judges the output — the unprompted message *is* the output), and once per decision per channel per cooldown. |
| `cairn/redact.py` | Strips credentials **before** a memory is written and **before** it is embedded. Provider keys, JWTs, private-key blocks, passwords in connection strings, self-naming secrets, card numbers, emails. Matches become labelled placeholders so the decision stays readable; Slack `<@U…>`/`<#C…>` refs are protected because they are the citation. |
| `cairn/backfill.py` | `cairn-backfill`: walks a channel's history, groups it into conversations (threads, and runs of messages separated by silence), and distils the candidates through the same gate and extractor as live capture. `--dry-run` uses the free gate only; `--limit` caps *model calls*; processed conversations are recorded so a re-run continues rather than re-pays. `--compact` instead replays dedup/supersession over memories written before those existed. |
| `cairn/llm.py` | The small "ask the model for JSON" calls the deterministic detectors make (bug details, action items, capture, relation classification). Built through the **provider factory** — it previously spoke Anthropic's HTTP wire format directly, which silently 401'd every capture the moment the project moved to Gemini. Runs at `temperature=0`; `CAIRN_EXTRACT_MODEL` can route it somewhere cheaper. |
| `cairn/jev.py` | Opt-in (`CAIRN_CLASSIFIER=jev` + `TYPESAFE_API_KEY`) client for **Jev**, TypeSafe AI's System One model: a typed question in, a choice + calibrated confidence out, no sampling. Plain urllib to `POST /v1/systemone`, one retry on 429/529/5xx, `None` on any failure. Used by `vector_store._classify` for duplicate/replaces/independent: a destructive verdict below `CAIRN_JEV_MIN_CONFIDENCE` (0.85, TypeSafe's high-stakes bar) keeps both; two yes/no checks in the same call (temporary? same case?) must come back clearly permanent (<0.2) and clearly the same case (>0.8) before anything retires, per TypeSafe's yes/no bands. Jev unavailable falls through to the LLM path. Scored by `python -m evals.measure_classifier`. |
| `cairn/__init__.py` | Version marker. |

## Model provider — env-selected factory (no vendor lock-in)

`agent._build_model()` is a **provider factory** chosen by `CAIRN_MODEL_PROVIDER`
(default `anthropic`). This is Cairn's core differentiator vs single-vendor Slack
agents: the brain is swappable by env, never hardcoded.

- `anthropic` (default) — Strands `AnthropicModel` (the Anthropic SDK). Also serves
  any **Anthropic-wire-compatible** endpoint (MiniMax M3, native Claude, proxies)
  via `ANTHROPIC_BASE_URL`. Reads `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`.
- `litellm` — LiteLLM multi-provider gateway (one class, many providers; model id
  like `openai/gpt-4o`). `openai` — OpenAI / OpenAI-compatible. `bedrock` — AWS
  Bedrock (works out of the box; boto3 ships with strands). `ollama` — local /
  self-hosted (sovereign). `gemini` — Google Gemini.
- Non-`anthropic` SDKs are **lazy-imported** (only required when selected); install
  via `pip install "cairn[providers]"`. Model id is provider-neutral via `CAIRN_MODEL`
  (falls back to `ANTHROPIC_MODEL`); response cap via `CAIRN_MAX_TOKENS` (def 2048).
- **Model routing:** `_build_model(model_id)` / `get_agent(app, model_id)` /
  `run(..., model_id=…)` accept an override (cached under its own key) so ambient
  background scans can run on a cheaper model (`CAIRN_AMBIENT_MODEL`) — the cost lever.

**Currently running on Google Gemini** (`CAIRN_MODEL_PROVIDER=gemini`,
`GEMINI_API_KEY`, `CAIRN_MODEL=gemini-flash-lite-latest`). The project began on
MiniMax M3 via its Anthropic-compatible endpoint and moved when those credits ran
out — which is the factory earning its keep, and also how two latent bugs
surfaced: the `gemini` branch had never been exercised and passed `max_tokens`
where Gemini wants `max_output_tokens`, and `llm.py` was speaking Anthropic's
wire format directly while claiming to follow the configured provider.

Only Anthropic-wire-compatible providers are truly env-only; the others go
through the factory branches above. Note that Strands keeps provider params
inside the model's **config**, so they must be merged via `update_config` —
assigning `.params` is silently ignored and drops the factory's token cap.

**Failure handling** (`_invoke`): transient errors (429 / 5xx / timeouts) are
retried with backoff that honours the provider's own `retryDelay` hint — Gemini
asks for ~30s on a rate limit, and backing off 0.6s just burns the attempts. A
**per-day** quota is different: it does not clear today, so it fails fast as
`QuotaExhaustedError` and the user is told that rather than "try again in a
moment", which would be a lie. Free-tier quotas are **per model**, so switching
`CAIRN_MODEL` buys a fresh allowance.

## Database (storage layer)

**Engine:** local **SQLite** (`sqlite3` stdlib), file at `DATABASE_PATH`
(default `./data/cairn.db`). No external DB/service. WAL journal mode; a
`threading.Lock` guards writes because the connection is shared across the Bolt
handler threads (`check_same_thread=False`).

**Schema (tables):**

```sql
-- long-term memory (cairn/storage.py) — episodic + provenance-stamped
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

-- telemetry / data foundation (cairn/observability.py)
CREATE TABLE interactions (             -- one row per Slack interaction (edge)
    id, request_id, ts, entrypoint, team, channel, channel_type, "user",
    thread_ts, prompt, model_id, input_tokens, output_tokens, total_tokens,
    tool_calls, model_calls, guardrail_hits, latency_ms, outcome, error,
    reasoning,        -- the model's <think> deliberation, stripped from the reply
    response );       -- the final reply text we sent back (auditable, was unstored)
CREATE TABLE steps (                    -- one row per model call / tool call / reasoning / guardrail
    id, request_id, ts, seq, kind, name, detail, tokens, duration_ms, outcome );
CREATE TABLE eval_results (             -- one row per golden case per cairn-eval run
    id, run_id, ts, case_id, category, model_id, passed, latency_ms, tool_calls, reasons );
```

All telemetry tables are opened on a separate connection from the memory store
(rollback/`DELETE` journal mode — keeps `cairn.db` self-contained + openable in
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

**Memory backends (env `CAIRN_MEMORY_BACKEND`):**
- `sqlite` (default) — FTS5 lexical recall, zero deps, no API key.
- `hybrid` (aliases `libsql`/`turso`/`vector`) — libSQL/Turso **hybrid** recall:
  FTS5 + semantic vectors fused with RRF (`cairn/vector_store.py`); needs
  `pip install -e ".[vector]"` + embeddings (`fastembed` local, no key by
  default). Verified live (memory add→recall through MiniMax M3). See
  `docs/vector-memory.md`.

**Memory scope (per-channel isolation, Claude-Tag-style):** `CAIRN_MEMORY_SCOPE`
(default `channel`; `team`/`global`; per-app `CAIRN_MEMORY_SCOPE_<NAME>`) controls
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

### Tables added since the memory store

| Table | Purpose |
|---|---|
| `memory_entries.superseded_by` / `.superseded_at` | A decision that replaced this one. The row is kept — history stays auditable — it just stops being an answer. Added by an idempotent `ALTER TABLE`. |
| `memory_relations` | Cached `duplicate` / `replaces` / `independent` verdicts, keyed by the **pair of texts**. The judgement is a model call and therefore non-deterministic (compacting the live store twice gave opposite verdicts on one pair), so re-asking an unchanged question is a re-roll rather than a re-examination. `--recheck` bypasses it, because a cached "independent" leaves both rows live and nothing later would correct it. |
| `pending_tickets` | Staged Jira tickets, with a 7-day TTL. A staged ticket waits on a human typing "yes", which can be hours; when this was a process dict, any restart in between dropped it silently and the "yes" did nothing. |
| `backfill_state` | Conversations already processed by `cairn-backfill`, so a re-run continues instead of re-paying for the same threads. |

## Jira — supporting cast (gaps A / F)

Jira no longer competes with the direction; it serves it. A ticket should carry
the decision it came from, and three people reporting one bug should produce one
ticket.

`slack_app.py` runs a deterministic shortcut **before** the agent for bug reports,
because a small model does not reliably call a tool from a system-prompt
instruction, however emphatically it is written:

1. **Detection** — `_looks_like_bug(text)` regex matches crash/error/fail/etc.
2. **Extraction** — `_extract_bug_details()` makes a focused single-turn LLM call
   (no tools, just JSON completion) → `{summary, description, priority, slack_mention}`.
3. **Dedup** — `find_duplicate()`: recent open issues are compared to the
   proposed summary **by meaning** (local embeddings — no model call, no quota),
   with the old keyword JQL as fallback. Verified live: "signing in never
   completes, the page just spins" matched "the login page hangs forever after
   submitting credentials" at distance 0.090, sharing no keywords. This is the
   **one** entry point — both creation paths must use it, because wiring a better
   check into only the agent tool left the deterministic path (the one people
   actually hit) still filing re-worded duplicates.
4. **Staging** — saved to `pending_tickets` in SQLite, with a TTL.
5. **Confirmation** — bot posts the details and asks "@Cairn yes to confirm".
   An assignee or priority named in the confirmation ("yes assign to jyoti",
   "make it urgent") overrides what was staged; leftover text is appended to the
   description.
6. **Creation** — `_create_jira_issue_direct()`:
   - attaches a **related decision** from memory, if one is genuinely close
     (measured, not assumed — see below), with its provenance
   - resolves the assignee **name-first**: a bare "Jyoti" → workspace member list
     → email → Jira `accountId`. No unique match ⇒ filed unassigned and the reply
     says so, rather than guessing.
   - POSTs to `/rest/api/3/issue`

**Citing a decision needs a relevance bar, not just a rank.** With a small store
*something* always comes back from a search, and a live run attached a dashboards
decision to a CSV-export bug. Measured against the real store: unrelated 0.472,
relevant 0.418 and 0.233 — a usable gap only 0.054 wide. The threshold sits just
under the unrelated case rather than just over the relevant one, because a
missing citation is a smaller loss than a confidently wrong one on a ticket
someone will act on.

**`check_auth()` runs at startup** — see the operational traps below.

Env vars: `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `TOOLSETS=jira_issues`.
Each app's own bot user id is auto-registered at startup (`auth.test` →
`loop.llm.register_bot_id`) and excluded from mention extraction so the bot
doesn't assign tickets to itself — nothing workspace-specific is hardcoded
(`CAIRN_BOT_USER_IDS` can seed extra ids).

## Extensibility — MCP tools at runtime

Set `CAIRN_MCP_SERVERS="<command> <args...>"` (a stdio command). On each request
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
  `cairn.trace` line + a `steps` row. `CAIRN_TRACE=off` silences logs (DB still
  written); `LOG_LEVEL=DEBUG` adds full reasoning + tool I/O. Logger names:
  `cairn.obs`, `cairn.trace`, `cairn.guard`.
- **Guardrails (Pillars 04/05):** `Guardrails` cancels a tool call before it runs
  when a per-request limit trips — max tool calls, repeated identical call, risky
  Slack method, or token budget — each independently toggled by env. Enforcing by
  default; the model receives the cancel reason and replies gracefully.
- **Evaluation (Pillar 01):** `cairn-eval` runs `evals/golden.json` (35 cases)
  through the real agent, scores tool selection + reply content, prints a
  scorecard, persists `eval_results` **with the model that produced them**, and
  exits non-zero on regression. Coverage follows the direction: supersession (3),
  decision log (2), provenance (1), memory and scope (15). Capture, dedup and
  backfill are covered by `tests/` instead — they are not single-turn behaviours,
  so the golden runner cannot score them.
- **Unit tests:** 284 in `tests/`, no model calls, run in ~1s.

## Known operational traps

These cost real time and are easy to hit again:

* **Ghost processes.** Slack round-robins Socket Mode events across every
  connected instance, so a second copy running old code or old credentials
  answers roughly half your messages — and looks like an intermittent bug.
  `pgrep -fl "venv/bin/cairn" | wc -l` should print `1`.
* **Jira fails quietly.** An expired token still returns **200 with empty
  results** from the search endpoints, so dedup reports "no duplicates" and
  project lookup reports "no projects". Only `/myself` distinguishes "nothing
  there" from "not logged in"; `check_auth()` runs it at startup.
* **The editable install.** Hatchling's `.pth` is not always applied; imports
  still work from the repo directory, so only console scripts fail, and only from
  elsewhere. See `CLAUDE.md` for the fix.
* **Moving the project folder breaks the venv** — scripts hardcode their absolute
  path in the shebang. Delete and recreate it.
* **Never `git checkout` `data/*.db` while the app runs** — it swaps the inode
  under the running process.

## What Cairn does NOT have yet

**No authorization.** Anyone in the workspace can invoke Cairn — there is no
allow-list of users or channels. This is the last open half of Pillar 05 and it
blocks any real deployment.

**No eval baseline has ever been run.** The harness and the 35 cases exist and
the runner resumes, but no full pass has completed: ~40 model calls against a
free-tier quota of 20 per day per model. Until that number exists, the honest
statement is "it works when tested by hand" — which is how thirteen real bugs
were found, and does not scale.

**Also missing:** per-user cost caps and a retention/deletion policy; two-way
Jira (ticket updates do not flow back into Slack); OTLP export of traces to an
external backend; and the MCP-augmented agent is still rebuilt per request, so
MCP costs a subprocess spawn on every message.

These map to the deferred items in `progress.md` and `DIRECTION.md`.
