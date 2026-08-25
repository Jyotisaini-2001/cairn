# Loop — Progress & Roadmap

_Living status doc for cross-session continuity. Update the "Status log" at the
bottom every working session. Read this + `architecture.md` + `CLAUDE.md` before
starting work._

## Current status (2026-06-23)

- **Stack:** Python ≥3.10, Strands Agents, slack-bolt (Socket Mode), SQLite.
- **Provider:** migrating to **MiniMax M3** via MiniMax's Anthropic-compatible
  endpoint — **env-only change, no code change** (see `.env.example`, `architecture.md`).
- **Shape:** one process can run **several Slack apps** at once (e.g. `Loop` +
  `Rasputin_Loop`), each its own agent/persona; three entrypoints per app
  (`@mention`, DM, `/loop`); tools = `slack`, `slack_send_message`, `calculator`,
  `think` + long-term memory.
- **Production readiness:** harness landed (v0.3.0) — step tracing, enforcing
  guardrails, and a golden-set eval runner. Still missing: authz, PII redaction,
  thread context, semantic retrieval.

### MiniMax M3 migration — checklist
- [x] Confirm M3 reachable via Anthropic-compatible API (`https://api.minimax.io/anthropic`).
- [x] Document env vars + key source in `.env.example`.
- [ ] Fill real MiniMax key in local `.env` and smoke-test one `@Loop` mention.
- [ ] Confirm tool-calling works through MiniMax (slack lookup + add_memory round-trip).
- [ ] Decide whether to bump the code default model id off `claude-sonnet-4-6`.

---

## Production roadmap — the 5 Pillars of Production AI

Framework: _"Each pillar enables the next. Skip one and the whole thing breaks
in production."_ Below, each pillar is scored against Loop **today** and given
the next concrete moves.

### 01 — Evaluation · _Define success before code_  — ✅ in place (v0.3.0)
`loop-eval` runs `evals/golden.json` (~18 cases) through the real agent and scores
tool selection + reply content (must/must-not contain), prints a scorecard, and
exits non-zero on regression. Results persist to `eval_results`.
- [x] Golden set with per-case expectations (tools + content), per-category scoring.
- [x] CLI runner with CI exit code (`loop-eval`, `--limit`, `--case`, `--json`).
- [x] **Memory evals (25 cases):** provenance recall (answer derivable only from
      who/where metadata → `mem-recall-provenance-who`), semantic recall across
      wording (`mem-recall-semantic`, no shared tokens), selective save vs skip
      chit-chat, and recall correctness. `loop-eval` now **isolates memory** to a
      throwaway DB per run (eval_results still persist) so cases are deterministic
      and don't pollute production memory. New memory cases verified 7/7 live.
- [ ] Wire `loop-eval` into CI; add groundedness + cost metrics.
- [ ] Run Claude vs MiniMax M3 to quantify the migration.

### 02 — Observability · _See everything, always_  — ✅ in place (v0.3.0)
Per-interaction JSON telemetry (`loop.obs`) + an `interactions` row, **and**
per-step traces (`loop.trace`) for every model call, tool call, and reasoning
chunk + a `steps` row. `LOG_LEVEL=DEBUG` shows full thinking + tool I/O.
- [x] Structured logs per request (request id, channel, user, model, tokens, latency, outcome).
- [x] Step-level traces: reasoning + model API calls + tool inputs/outputs.
- [ ] Export traces to an OTLP backend (Phoenix/Langfuse); build a dashboard.
- [ ] Alert on error-rate / latency / quality drift.

### 03 — Data Foundation · _Question + Tracking data_  — ✅ much improved
`interactions` + `steps` + `eval_results` tables exist (substrate for 01/02), and
memory is now **episodic + provenance-stamped + full-text searchable**.
- [x] Stop dropping `metadata`; add the interaction/step event log.
- [x] **Provenance on every memory** — author/channel/team/source/thread_ts/date
  auto-stamped from the request (the `add_memory` tool can't carry metadata, so
  the store attaches it); surfaced back to the model via a custom injection format.
- [x] **FTS5 retrieval** — BM25 + recency, sanitized query, `LIKE` fallback.
      No new deps, no API key. Tested in `tests/test_memory.py`.
- [x] **Hybrid semantic retrieval** — `CAIRN_MEMORY_BACKEND=hybrid` adds libSQL/
      Turso native vector search fused with FTS5 via RRF (`vector_store.py`),
      pluggable embeddings (`fastembed` local, no key). Merged to `main`, verified
      live (memory add→recall through MiniMax M3). See `docs/vector-memory.md`.
- [x] Exact-content dedup on write (both backends).
- [x] Per-scope **filtering** in `search()` — `CAIRN_MEMORY_SCOPE` (channel default
      / team / global; per-app override) gives Claude-Tag-style per-channel memory
      isolation. Recall-time filter read from `reqctx.memory_scope()` inside each
      store; hybrid store post-filters the semantic leg; dedup scoped to match.
      Tested (`test_channel_scope_*`) + verified live on both backends.
- [ ] Near-duplicate (semantic) dedup; the model can still re-save a re-phrased
      fact on recall (prompt nudge added; exact dups are caught).

### 04 — Orchestration · _Patterns that scale_  — ⚠️ improving
Guardrails add runaway-loop, repeat, and token-budget circuit breakers. Still
synchronous/blocking; agent rebuilt per request when MCP is active; no thread context.
- [x] Circuit breakers (max tool calls, repeats, token budget) via Strands hooks.
- [x] **Provider factory** (`CAIRN_MODEL_PROVIDER`): env-selected model backend
      (anthropic/litellm/openai/bedrock/ollama/gemini) — no vendor lock-in — with
      per-run model routing (`model_id` override) for cost-cheap background work.
- [x] **Ambient mode** (`CAIRN_AMBIENT`, off by default): daemon-per-app loop that
      follows up on stalled threads (`loop/ambient.py`); pluggable discovery
      (narrow default), live staleness re-check, re-nudge guard, per-app posting.
- [ ] Timeouts, retries with backoff, graceful degradation on provider errors.
- [ ] Non-blocking Slack handling (ack fast, process async).
- [ ] Cache the MCP-augmented agent; the MCP client is single-server today (one
      stdio server) — running a sandbox MCP *and* another server needs a small
      `_mcp_client()` extension.

### 05 — Governance · _What keeps you in production_  — ⚠️ partial
The `interactions` table is now an audit trail (who/what/which tools), and a risky
Slack-method blocklist + cost circuit breaker are enforced.
- [x] Audit log of interactions; blocklist for destructive Slack methods; token cap.
- [ ] AuthZ: allow-list workspaces/channels/users; scope tools per caller.
- [ ] PII redaction before persistence; retention/deletion policy.
- [ ] Per-user/day cost caps; document secret rotation.

---

## Why observability is the priority — the ROI case study

Six weeks post-launch numbers from the reference study (`Observability_study_ROI.png`):

| Metric | Result | Note |
|---|---|---|
| Accuracy | **87%** | target was 85% |
| Deflection rate | **62%** | ~$1M annual savings |
| Response time | **65% ↓** | customer wait time |
| CSAT | **4.4/5** | vs 4.2 for humans |

The point isn't the launch numbers — it's what happened next:

> Week 6: accuracy dropped 87% → 81%. **Dashboard flagged it within 4 hours.**
> Traced to a policy update that didn't propagate to the knowledge base —
> **identified in 90 minutes. Fixed in 2 days.** Accuracy restored, **3 new test
> cases added to prevent recurrence.**

_"Observable. Debuggable. Improvable. That's what you get when you build
measurement first."_ → For Loop, this argues for doing **Pillar 02 (observability)
+ Pillar 01 (eval)** before adding features. Without them, a MiniMax M3 swap or a
prompt edit could silently regress quality and we'd never know.

### Suggested next sprint (now that the harness exists)
1. Wire `loop-eval` into CI and run **Claude vs MiniMax M3** for a baseline diff.
2. Export traces to an OTLP backend (Phoenix/Langfuse) + a simple dashboard.
3. AuthZ allow-list + PII redaction before persistence (Pillar 05).

---

## Status log
- **2026-07-10 (session/marketplace-prep)** — **Marketplace-prep hardening; two
  root causes killed; everything re-verified live** (see `SESSION_PROGRESS.md`
  for the full session record). **(1) DBs untracked from git** — `data/loop.db`
  + `loop_vec.db` were deliberate `.gitignore` exceptions; every checkout swapped
  the inode under the running app → the entire WAL/readonly saga. Runtime data
  is now local-only. **(2) Reasoning telemetry fixed** — MiniMax's endpoint
  drifted: it no longer leaks inline `<think>` text and only returns `thinking`
  blocks when the request opts in. `_build_model` now sends
  `thinking={enabled, CAIRN_THINKING_BUDGET (default 1024)}`; verified live
  (reasoning persisted for real Slack turns). **(3) GHOST PROCESS found** — an
  old (~663e9a2-era) Loop instance on another machine shares the Rasputin app
  token; Socket Mode round-robins events to it, which is why past fixes "didn't
  work" live. Evict by regenerating the app-level tokens. **(4) Assign-by-name
  verified end-to-end in production**: "ishan should fix this" → staged →
  in-thread yes → **KAN-22 created, assignee ISHAN KUMAR** (bare name → Slack
  email → Jira accountId; unknown names file unassigned — no default-email
  fallback; the "default email" users see is the *reporter* = JIRA_USERNAME,
  inherent to API-token auth). **(5) BOT_IDS unhardcoded** — each app
  self-registers its bot user id via `auth.test` at startup (works in any
  workspace; `CAIRN_BOT_USER_IDS` seeds extras). **(6) Credential sweep clean** —
  no secrets in code/history/DBs; `.env` tokens were once shared in chat →
  ROTATE all before submission. Tests 46/46. Hackathon field-guide artifact
  (capabilities/triggers, demo script, positioning, knowns/unknowns) published.
- **2026-07-07 (fix/db-wal-mode, assignee-priority fix)** — **The "assigns to the
  requester, not the named person" bug (from the KAN-11/KAN-12 screenshots) is
  fixed.** Root cause found in the live `steps` trace: the agent passed BOTH the
  correct `assignee_name="Jyoti"` AND `slack_mention="<@requester>"` (it echoes the
  requester's `<@id>` from the context line), and `_resolve_assignee_email` gave
  `slack_mention` priority over `assignee_name` → it resolved the requester's email
  and never looked Jyoti up. **Fix:** resolution is now **name-first** —
  `assignee_name` (the user's stated owner) → `assignee_email` → `slack_mention`.
  A resolvable name wins over an echoed mention/email. Prompt + tool docstring also
  now say "pass only `assignee_name` for a named person; never pass the requester's
  mention." **Verified live** against the real workspace: `assignee_name='Jyoti' +
  requester mention` → Jyoti's email (js.saini267), `Jaimin` → villa30six (the agent
  had wrongly said Jaimin wasn't found — the resolver finds him). +2 jira tests
  (name-first, requester-mention-loses); suite green.
- **2026-07-07 (fix/db-wal-mode, live-test follow-up)** — **Live run surfaced two
  environment issues + one resilience gap; fixed the code gap.** (1) A **stale
  `python -m loop.main` process from Jun-24 (old WAL-mode code)** was still running
  alongside the new app, holding an unlinked `loop.db` inode + `-wal`/`-shm` — the
  classic "silent/duplicate bot" + DB-contention cause. Kill stale processes and
  run exactly one. (2) Rewriting `data/loop.db` with **git while the app was live**
  (stash/checkout during the merge) swapped the inode under the long-running
  process, wedging its cached connection → every telemetry write hit
  `sqlite3.OperationalError: attempt to write a readonly database` (a fresh process
  writes fine; the new `reasoning`/`response` columns exist). **Fix:**
  `observability._execute_write()` now **reconnects once on `OperationalError`**
  (drops the stale handle via `_reset_conn()`, `_db()` rebinds to the current
  on-disk inode, retries) — telemetry self-heals from a swapped/stale/transient-
  readonly DB instead of losing every row; a persistent failure is still swallowed.
  All three writers (`_persist`, `record_step`, `record_eval`) route through it.
  +`tests/test_observability.py` (4). Note: don't `git checkout`/commit `data/*.db`
  while the app runs. **Also confirmed from the run:** thinking did NOT leak
  (`reasoning_chars: 0`, clean `response_preview`); Jira create + assignee/account
  resolution ran live (KAN-9/KAN-10). The "assigned to requester not Jyoti" case
  was a test-flow artifact — the `yes` was a NEW mention, not a threaded reply, so
  the staged ticket (with assignee Jyoti) wasn't used; reply `yes` **in-thread**.
- **2026-07-07 (fix/db-wal-mode)** — **Four essential optimizations of existing
  behavior (from the Slack screenshots + `gaps.txt` review). Unit-tested; live
  verification on Rasputin_Loop still pending.**
  **(1) Thinking no longer leaks into the reply (Pillar 02).** MiniMax-M3 via the
  Anthropic-compatible endpoint streams its chain-of-thought as plain `<think>…
  </think>` text (never an Anthropic reasoning block, so `reasoning_callback` never
  fired — confirmed zero `reasoning` steps in the live DB). `agent._split_reasoning()`
  now strips those blocks (incl. unclosed/stray-tag cases) → Slack sees only the
  final answer; a system-prompt line also tells the agent not to narrate deliberation.
  The thinking is preserved as **two new `interactions` columns** — `reasoning`
  (the stripped `<think>`) and `response` (the reply we sent, which was previously
  **never persisted**) — via the existing idempotent column migration.
  **(2) Jira project key resolves live, never 400s (Gap A/F).** `resolve_project_key()`
  matches a named project (key OR plain name like "BAN") against the live
  `/project/search` list; unknown → falls back to `JIRA_PROJECT_KEY` with a note in
  the reply. Still cannot (by design) create projects/spaces. Project name is now
  extracted in `_extract_bug_details` and carried through staging.
  **(3) Assignee resolves from a bare name, no follow-up (Gap A).** `_slack_email_from_name()`
  matches a name ("Jyoti") against the workspace member list (`users.list`, unique
  exact-or-first-name) → email → Jira account. Priority: explicit email → `<@U…>`
  mention → name. Per the decision taken, **no unique match ⇒ filed unassigned + a
  visible flag** (never guesses, never asks for an email). Wired through both the
  agent tool (`jira_create_issue`/`jira_stage_ticket` gained `assignee_name`) and the
  deterministic staging path.
  **(4) Ambient + memory extended.** Ambient (per decision) **never** stages/creates
  Jira tickets — it may only *suggest* one in words; a human must ask (prompt tweak
  only). New `loop/memory_capture.py` (off unless `CAIRN_MEMORY_CAPTURE=on`): decisions/
  commitments in channels Loop merely **observes** are distilled (regex gate → focused
  model call) and saved to the long-term (vector) store **with provenance**, so
  "what did we decide about X" recall works without an `@Loop` turn (gaps B/I/K).
  Scoped by `CAIRN_MEMORY_SCOPE`; exact-content dedup on the store side.
  **Tests:** +22 (`test_reasoning` 6, `test_jira` 10, `test_memory_capture` 6), all
  green; existing suites still 17/17. Golden set: `<think>`/`</think>` added to
  `must_not_contain` on `identity` + `reasoning-tradeoff` as a leak regression guard.
  `.env.example` now documents the (previously undocumented) `JIRA_*` block +
  `CAIRN_MEMORY_CAPTURE`. **Open:** live round-trip on Rasputin_Loop (thinking hidden,
  Jyoti auto-assigned, BAN/KAN resolved); near-dup memory dedup for captured decisions.
- **2026-07-06 (feat/jira-auto-triage → development)** — **Jira auto-triage shipped
  (Gap F: manual data movement). Verified live on Rasputin_Loop.** Four workstreams:
  **(A) MCP env forwarding fix** — `_mcp_client()` now passes `env=os.environ.copy()`
  to `StdioServerParameters`; without this, MCP servers (`mcp-atlassian`, `mcp-server-fetch`)
  started but returned zero tools (credentials not visible to subprocess). Verified
  `mcp-atlassian` loads 49 tools with fix, 0 without.
  **(B) Native Jira tools** — `loop/jira_tool.py`: `jira_stage_ticket` (saves to
  in-process `_pending` dict keyed by thread_ts, returns confirmation text),
  `jira_create_issue` (REST API with assignee resolution), `jira_search_issues`,
  `_create_jira_issue_direct` (called directly from slack_app, bypasses agent).
  Registered as first-class Strands tools in `agent.py`.
  **(C) Deterministic bug-detection shortcut in slack_app.py** — MiniMax M3 does not
  reliably call tools proactively from system-prompt instructions (stop_reason=end_turn,
  tool_calls=[] even with CRITICAL RULE headers). Solution: `_looks_like_bug()` regex
  in `on_mention` intercepts bug reports before the agent runs; `_extract_bug_details()`
  makes a focused LLM call for JSON extraction only; `_pending[thread_ts]` stores staged
  ticket; `@Bot yes` reply directly calls `_create_jira_issue_direct()`. Agent bypassed
  for both detection and creation — 100% reliable.
  **(D) Smart assignee routing** — `<@UXXXXXXX>` extracted from raw Slack text (bot user
  IDs excluded), `users.info` fetches email, Jira `user/search` resolves account ID,
  ticket assigned automatically. Verified live: KAN-3 created, assigned to Jyoti Saini.
  **Open:** description still shows raw `<@U...>/<#C...>` IDs (cosmetic); near-dup dedup
  for repeated bug reports; per-project-key config per app.
- **2026-07-01 (feat/provider-factory-claude-tag → main)** — **Claude-Tag-inspired
    capabilities on a provider-agnostic stack — merged to main, verified live.** Built in
    response to Anthropic's Claude Tag launch; the USP is the one thing Claude Tag
    can't be — open + provider-agnostic + self-hostable. Four workstreams:
    **(A) Provider factory** — `agent._build_model()` is now env-selected
    (`CAIRN_MODEL_PROVIDER`: anthropic default / litellm / openai / bedrock / ollama
    / gemini), non-anthropic SDKs lazy-imported behind a new `[providers]` extra;
    default path unchanged (MiniMax M3 still works; verified live, MiniMax-M3 reply
    through the factory). `CAIRN_MODEL`/`CAIRN_MAX_TOKENS` added; `model_id` override
    threaded through `_build_model`/`get_agent`/`run` for cost-routed runs. Bedrock
    constructs with zero new deps (boto3 vendored).
    **(B) Per-channel memory isolation** — `CAIRN_MEMORY_SCOPE` (channel default /
    team / global; per-app `CAIRN_MEMORY_SCOPE_<NAME>`). Recall-time filter via new
    `reqctx.memory_scope()`, applied in `storage.py` (FTS+LIKE) and `vector_store.py`
    (lexical leg in SQL, **semantic leg post-filtered** since `vector_top_k` can't
    take a WHERE); dedup scoped to match. No migration (channel/team already stamped).
    Verified live on both backends incl. semantic isolation; +2 regression tests
    (`test_channel_scope_*`), suite 9/9.
    **(C) Ambient PM mode** — new `loop/ambient.py`, off unless `CAIRN_AMBIENT=on`;
    `maybe_start()` spawns a daemon per app from `slack_app.start()`. **Two discovery
    modes** via `CAIRN_AMBIENT_DISCOVERY`: `interactions` (narrow default = threads we
    touched, from the telemetry DB) and `channels` (**full-channel scan** via
    `conversations_history`, needs `channels:read`+`channels:history`). Live staleness
    re-check via `conversations_replies`, re-nudge guard, posts via the **per-app** Web
    client (sidesteps the bare-`SLACK_BOT_TOKEN` identity of `slack_send_message`),
    wrapped in `obs.record` (`<app>:ambient`). Unit tests in `tests/test_ambient.py`
    (5). **Verified LIVE 2026-07-01:** Rasputin_Loop, full-channel scan over
    #new-channel, posted two well-formed PM nudges in-thread on stalled questions
    (`rasputin:ambient` rows recorded, MiniMax-M3, no guardrail hits).
    **(D) Sandbox via MCP** — documented only: point `CAIRN_MCP_SERVERS` at a
    code-exec MCP server (zero core code; per-request rebuild). Single-server MCP
    limitation noted. **Evals:** `loop-eval` 25/28 live on MiniMax-M3; +3
    `memory-scope` golden cases (channel isolation through the real agent, all pass);
    3 failures are M3 tool-happiness on smalltalk/safety (pass on retry, unrelated to
    the diff). **Open:** `loop-eval` provider A/B + CI gate; enterprise track (authz,
    PII, per-workspace DB isolation, ambient rate/cost caps, self-hosted in-VPC
    sandbox). `.env.example` documents every new var.
- **2026-06-24 (v0.4.0)** — **Hybrid RAG memory + Slack AI Assistant merged to
  `main`, verified live.** With the MiniMax key (in `ANTHROPIC_AUTH_TOKEN`) and
  `assistant:write` granted on Rasputin_Loop, both branches were tested and
  merged. (1) **Hybrid memory** (`CAIRN_MEMORY_BACKEND=hybrid`): libSQL/Turso FTS5
  + native vector search (`F32_BLOB`/`vector_top_k`) fused via **RRF**; embeddings
  pluggable, default `fastembed` (local, **no key**; confirmed MiniMax `embo-01`
  needs only the key — **no Group ID** on api.minimax.io). Verified end-to-end: a
  live agent turn saved then **semantically recalled** ("runbook for outages" →
  "playbook in Notion under SRE") **with provenance citation**, and `loop-eval`
  memory cases pass on the hybrid backend. (2) **Exact-content dedup** on write +
  prompt nudge to not re-save on recall (model can still re-phrase → near-dup is a
  known follow-up). (3) **Slack AI Assistant** (`CAIRN_SLACK_ASSISTANT=on`):
  suggested prompts + "is thinking…" status, routed to the same agent; both apps
  connected live and attached the assistant cleanly. `.env` set to
  `hybrid`+`fastembed`+assistant-on; `pip install -e ".[vector]"`. Bumped to
  v0.4.0. Added **7 memory evals** (provenance recall, semantic recall across
  wording, selective save vs skip-chit-chat); `loop-eval` now isolates memory to a
  throwaway DB per run via `CAIRN_MEMORY_DB_PATH`/`CAIRN_VECTOR_DB_PATH` (eval_results
  still persist) — new cases 7/7 green live. Note: MiniMax M3 sometimes re-saves a
  re-phrased fact on a recall turn (exact dups deduped; not asserted in CI).
  **Open:** human round-trip in the Rasputin_Loop assistant *pane* (needs the
  Agents&AI-Apps feature + assistant_thread_* events subscribed).
- **2026-06-24 (later)** — **Episodic memory overhaul (Pillar 03), shipped to
  `main` — no new deps, no API key.** `SqliteMemoryStore` now: (1) **stamps
  provenance** on every memory (author/channel/team/source/thread_ts/date) pulled
  from `RequestState` — the `add_memory` tool only carries content, so the store
  attaches who/where/when itself; (2) recalls via **FTS5** (`bm25()` + recency)
  with a sanitized prefix-OR query (`_fts_match`, injection-safe) and a `LIKE`
  fallback if the SQLite build lacks FTS5; (3) surfaces provenance back to the
  model through a custom Strands **injection format** (`agent._format_memories`)
  that tags each `<entry>` with `from/in/when` (the default format drops
  metadata). System prompt gained **memory discipline** (save decisions/facts/
  prefs/episodes; skip chit-chat — the agent is the filter). Added
  `tests/test_memory.py` (6 cases, all green; runnable without pytest). Migration
  is additive + idempotent; verified against a copy of the committed `loop.db`
  (existing row re-indexed, recall works across wording). **Experimental, on
  branches (not merged):** `feat/vector-memory-turso` (libSQL/Turso native vector
  search + pluggable embeddings — fastembed default = no key) and
  `feat/slack-assistant` (native Slack "Assistant" AI surface). Hackathon: Loop
  qualifies today via **MCP server integration** (`CAIRN_MCP_SERVERS`); the
  Assistant branch adds a clean **Slack AI capabilities** claim.
- **2026-06-24 (pm)** — **Multi-app support.** `slack_app._discover_apps()` +
  `start()` now run N Slack apps in one process (each its own Bolt App + Socket
  Mode thread), via `SLACK_BOT_TOKEN_<NAME>`/`SLACK_APP_TOKEN_<NAME>` pairs (bare
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` still works). One agent per app
  (`get_agent(app_name)`), each with optional `CAIRN_PERSONA_<NAME>` system-prompt
  override — the seam for giving Loop vs Rasputin_Loop different jobs. Telemetry
  `entrypoint` is now `<app>:<kind>`. Diagnosed the "@Rasputin_Loop silent" issue:
  tokens were correct but **stale `loop` processes** (one on Loop's old env) were
  answering `@Loop`, and Rasputin_Loop almost certainly lacks **Event
  Subscriptions** (`app_mention`/`message.im`). `.env.example` documents the
  multi-app + persona convention. Verified discovery + persona via a scratch test.
- **2026-06-24** — Shipped **v0.3.1: file attachments / multimodal input**.
  `slack_app._collect_files()` downloads Slack attachments via
  `url_private_download` + bot token (needs **`files:read`** scope); allows
  `image/*`, `text/*`, `application/json`, size-capped by `CAIRN_MAX_FILE_BYTES`.
  `agent.run(prompt, attachments=…)` → `_build_message()` builds a Strands
  multimodal content list — images go to **MiniMax M3 natively**, text/code/json
  inlined. DM `file_share` uploads now answered (subtype no longer dropped).
  System prompt + `.env.example` + `architecture.md` updated. PDFs extracted via
  **pypdf** (lightweight) and inlined as text. Diagnosed the silent-bot issue:
  `.env` had Loop's tokens in `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` and the
  Rasputin_Loop bot token in an unread var — fixed the bot token, flagged the
  xapp to regenerate under app `A0BCVHES936`. **Open:** verify M3's
  *Anthropic-compatible* endpoint accepts image blocks (else fall back to MiniMax
  OpenAI-compatible endpoint for images).
- **2026-06-23 (pm)** — Shipped the **v0.3.0 harness**. Fixed the `_extract_text`
  dict bug (replies were "_(no response)_"). Added a `message` handler (channel
  tracking + DM support, kills Bolt 404s). **Observability:** edge telemetry
  (`interactions`) + step tracing (`steps`) for every model call / tool call /
  reasoning chunk (`loop.obs`/`loop.trace`). **Guardrails:** enforcing tool-call
  limits (max calls, repeats, risky Slack methods, token budget) via Strands
  hooks + `cancel_tool`, per-rule env toggles. **Evals:** `evals/golden.json`
  (~18 cases) + `loop-eval` runner (`eval_results` table, CI exit code).
  New modules: `context.py`, `tracing.py`, `guardrails.py`, `eval.py`. Persisted
  memory `metadata`. Updated `.env.example`, `architecture.md`.
- **2026-06-23 (am)** — Adjusted Loop for **MiniMax M3** (Anthropic-compatible
  endpoint, env-only). Rewrote `.env.example` with Slack-token + MiniMax-key
  guidance. Created the cross-session harness: `CLAUDE.md`, `architecture.md`,
  this file. Mapped current state to the 5 Pillars and the observability ROI study.
