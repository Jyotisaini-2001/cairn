# Cairn — Progress & Roadmap

_Living status doc for cross-session continuity. Update the "Status log" at the
bottom every working session. Read this + `architecture.md` + `CLAUDE.md` before
starting work._

## Current status (2026-06-23)

- **Stack:** Python ≥3.10, Strands Agents, slack-bolt (Socket Mode), SQLite.
- **Provider:** migrating to **MiniMax M3** via MiniMax's Anthropic-compatible
  endpoint — **env-only change, no code change** (see `.env.example`, `architecture.md`).
- **Shape:** one process can run **several Slack apps** at once (e.g. `Cairn` +
  `Rasputin_Loop`), each its own agent/persona; three entrypoints per app
  (`@mention`, DM, `/loop`); tools = `slack`, `slack_send_message`, `calculator`,
  `think` + long-term memory.
- **Production readiness:** harness landed (v0.3.0) — step tracing, enforcing
  guardrails, and a golden-set eval runner. Still missing: authz, PII redaction,
  thread context, semantic retrieval.

### MiniMax M3 migration — checklist
- [x] Confirm M3 reachable via Anthropic-compatible API (`https://api.minimax.io/anthropic`).
- [x] Document env vars + key source in `.env.example`.
- [ ] Fill real MiniMax key in local `.env` and smoke-test one `@Cairn` mention.
- [ ] Confirm tool-calling works through MiniMax (slack lookup + add_memory round-trip).
- [ ] Decide whether to bump the code default model id off `claude-sonnet-4-6`.

---

## Production roadmap — the 5 Pillars of Production AI

Framework: _"Each pillar enables the next. Skip one and the whole thing breaks
in production."_ Below, each pillar is scored against Cairn **today** and given
the next concrete moves.

### 01 — Evaluation · _Define success before code_  — ✅ in place (v0.3.0)
`cairn-eval` runs `evals/golden.json` (~18 cases) through the real agent and scores
tool selection + reply content (must/must-not contain), prints a scorecard, and
exits non-zero on regression. Results persist to `eval_results`.
- [x] Golden set with per-case expectations (tools + content), per-category scoring.
- [x] CLI runner with CI exit code (`cairn-eval`, `--limit`, `--case`, `--json`).
- [x] **Memory evals (25 cases):** provenance recall (answer derivable only from
      who/where metadata → `mem-recall-provenance-who`), semantic recall across
      wording (`mem-recall-semantic`, no shared tokens), selective save vs skip
      chit-chat, and recall correctness. `cairn-eval` now **isolates memory** to a
      throwaway DB per run (eval_results still persist) so cases are deterministic
      and don't pollute production memory. New memory cases verified 7/7 live.
- [ ] Wire `cairn-eval` into CI; add groundedness + cost metrics.
- [ ] Run Claude vs MiniMax M3 to quantify the migration.

### 02 — Observability · _See everything, always_  — ✅ in place (v0.3.0)
Per-interaction JSON telemetry (`cairn.obs`) + an `interactions` row, **and**
per-step traces (`cairn.trace`) for every model call, tool call, and reasoning
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
      follows up on stalled threads (`cairn/ambient.py`); pluggable discovery
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
measurement first."_ → For Cairn, this argues for doing **Pillar 02 (observability)
+ Pillar 01 (eval)** before adding features. Without them, a MiniMax M3 swap or a
prompt edit could silently regress quality and we'd never know.

### Suggested next sprint (now that the harness exists)
1. Wire `cairn-eval` into CI and run **Claude vs MiniMax M3** for a baseline diff.
2. Export traces to an OTLP backend (Phoenix/Langfuse) + a simple dashboard.
3. AuthZ allow-list + PII redaction before persistence (Pillar 05).

---

## Status log
- **2026-08-27 (feat/cairn)** — **Jira reframed to serve the direction.**
  **(A) Dedup by meaning.** The existing check was keyword-based (`summary ~`
  over the first six words), which misses the actual case: three people report
  one bug in three wordings and none of them match each other. Recent open issues
  are now compared by embedding against the proposed summary — the same tool the
  memory store uses for near-duplicates — with the keyword search kept as a
  fallback. Local embeddings, so it costs a Jira search and CPU: no model call,
  no quota. This closes `gaps.txt` gap A.
  **(B) A ticket carries the decision it came from.** Filing attaches the most
  relevant decision from memory with its provenance, so whoever picks up the
  ticket does not re-derive context settled in Slack weeks ago. Read straight from
  the store, so filing never depends on model quota.
  **(C) Both fail open.** A dedup or lookup failure lets the ticket through —
  filing a duplicate is annoying, failing to file the bug is worse.
  Switchable: `CAIRN_JIRA_DEDUP`, `CAIRN_JIRA_DEDUP_DISTANCE`, `CAIRN_JIRA_CITE`.
  **Tests 278** (+11). Not verified against live Jira yet — the unit tests use
  stub embeddings and stub issue lists.
- **2026-08-26 (feat/cairn, governance)** — **PII redaction: credentials are
  stripped before a memory is written.** Memories are stored word for word, and a
  decision is exactly the kind of message that carries one — "ok let's go with the
  new provider, key is sk-live-…". Without this the key landed in the database,
  was embedded into a vector, and would be read back months later by anyone who
  asked the right question.
  **(A) At the store, not the caller.** `cairn/redact.py` is applied inside both
  memory stores' `add()`, which is the single point every write passes through:
  the agent's `add_memory`, the direct `remember()` path, passive capture and
  backfill. Anywhere else and one of those four stays uncovered.
  **(B) Before the embedding, not after.** The vector is derived from the text,
  so redacting afterwards would leave the secret encoded in the embedding even
  though the stored text looked clean.
  **(C) The memory stays readable.** A match becomes a labelled placeholder
  (`[redacted:api-key]`), so the decision still says what was decided and the
  reader can see that a credential was there. What was stripped is recorded in
  the entry's metadata, so it is auditable.
  **(D) Slack refs are protected.** `<@U…>` / `<#C…>` read as long tokens to a
  naive matcher, and losing them would break the citation the memory exists for.
  Covers provider keys (OpenAI/Stripe-style, Slack, GitHub, AWS, Google), JWTs,
  private-key blocks, passwords inside connection strings, self-naming secrets
  (`password: …`), card numbers and emails. `CAIRN_REDACT=off` disables it;
  `CAIRN_REDACT_KINDS` narrows the categories, since some teams genuinely want an
  email kept.
  **Tests 267** (+16). Verified live against the real store.
  **Deliberately not done yet:** authz (an allow-list of who may use Cairn). The
  workspace currently has one human in it, so it buys nothing today — but it is
  the other half of Pillar 05 and blocks any real deployment.
- **2026-08-26 (feat/cairn, evals)** — **Closed the measurement gap: +6 golden
  cases, and a full run is now actually finishable on a free tier.**
  **(A) The golden set covers the new work** (29 → 35). Three **supersession**
  cases run in order — state the original, state the revision, then ask — and the
  revision deliberately does *not* restate the old value, so a pass proves the
  store retired the old row rather than the model reasoning its way there. Two
  **decision-log** cases pin the boundary from both sides: a question about the
  record must reach for the log, a "why did we…" must not. One **provenance**
  case guards the central claim. (Capture, dedup and backfill stay in `tests/` —
  they are not single-turn behaviours, so the golden runner cannot score them.)
  **(B) `cairn-eval --resume`.** A free-tier quota is tens of requests per day and
  35 cases need ~40 model calls, so the set cannot finish in one sitting; without
  resume the next day's allowance goes on re-proving yesterday's passes and the
  set never completes. Results append to the same `run_id`, so the scorecard is
  one comparable run. Only *passes* are skipped — a failure is the thing you are
  fixing, and one failure is not a verdict.
  **(C) Two traps that resume exposed.** The isolated memory DB had to become
  **stable per run**: with a fresh throwaway DB, every `mem-recall-*` would fail
  for want of a memory a skipped `mem-add-*` had already written — false failures
  that look exactly like a regression. And an exhausted quota now **halts the
  whole run** instead of recording the remaining cases as failures, printing the
  exact resume command.
  **(D) `--model` override**, since free-tier quotas are per model — rotating
  models is how a full run finishes in one day.
  **Tests 251.** Verified live: a 2-case run, then `--resume` correctly skipped
  both and ran only the third, appending to the same run id.
  **Open:** an actual full-set baseline still needs to be run and recorded here.
- **2026-08-26 (feat/cairn, Phases 2 + 4)** — **All five phases of `DIRECTION.md`
  are now done.**
  **(A) Phase 2 — the decision log** (`cairn/decisions.py`). Recall had only ever
  been a side effect of chatting: a few memories injected before a reply. Right
  for "why did we pick Postgres?", wrong for "what did we decide last week",
  which wants a list, ordered, sourced, and longer than a prompt can carry. The
  log reads the store directly — deterministic, cannot invent an entry, and still
  works when the model quota is spent (which, on the free tier, it regularly is).
  Understands time windows ("last week", "past 3 days"), which semantic search
  structurally cannot answer. Two entry points: a `decision_log` tool for the
  agent, and `/cairn` (alias `/loop`) which answers straight from the log with no
  model call when the ask is about the record.
  **(B) Scope became an explicit policy** (`context.scope_policy` / `scope_for`).
  The log runs outside an agent turn, so it needed the isolation rules without a
  live `RequestState` — otherwise a slash command would have been a way around
  per-channel memory isolation.
  **(C) Phase 4 — resurfacing** (`cairn/resurface.py`, off by default). When
  someone re-opens a settled question, Cairn posts the decision and its source
  in-thread. Deferred until now on purpose: a bot that interrupts wrongly gets
  muted and never recovers. The bar is a question (not a statement — a decision
  being made is capture's job), a strong match against a **live** decision at a
  tighter threshold than recall's (nothing judges the output; the unprompted
  message *is* the output), and once per decision per channel per cooldown. The
  wording offers rather than polices — a team may be revisiting something on
  purpose.
  **Tests 245.** **Open:** the golden set still has no cases for capture, dedup,
  supersession, the decision log or resurfacing — a full `cairn-eval` run is
  structurally impossible on Gemini's free tier (29 cases, ~30-40 model calls, 20
  per day per model), so this needs paid quota. Also still open: the `cairn/`
  package rename, and README/architecture prose still says "Cairn".
- **2026-08-26 (feat/cairn, Phase 3)** — **Memory hygiene: near-duplicates
  collapse, replaced decisions retire.** The store had become the argument for
  this: 7 memories carrying 3 facts, the Postgres decision stored four times in
  four wordings (saved live, then re-read by backfill), and "cap review at 24
  hours" sitting beside "moved it to 4 hours" as equally-cited truths.
  **(A) Both problems, one mechanism.** On write, the closest live memories are
  compared by cosine distance. A duplicate collapses into the existing row
  (keeping the richer wording); a replacement retires the old row via
  `superseded_by`. `_fetch` — the one choke point both retrieval legs pass
  through — skips retired rows, so a stale decision can never come back *with a
  citation*, which is the worst failure this product has.
  **(B) Thresholds alone cannot separate the cases.** Measured on the real
  store's vectors: same fact re-phrased 0.12–0.15, answer changed 0.179,
  different subject 0.49–0.60. The first two nearly touch, so distance only
  decides how much to ask — free collapse below `CAIRN_DEDUP_DISTANCE`, one model
  call to classify below `CAIRN_RELATED_DISTANCE`, store above it.
  **(C) Chain-aware.** A live run retired the middle of a 24h→4h→8h chain and
  left the *original* live. Each write is now checked against several neighbours;
  adding "6 hours" correctly retired all three earlier versions.
  **(D) `cairn-backfill --compact`** replays this over pre-existing rows: took the
  live store from 10 rows / 3 facts to 3 rows / 3 facts.
  **(E) Containing the non-determinism.** The classification is a model call, so
  compacting twice gave opposite verdicts on the same pair. Three fixes:
  `temperature=0` on all classification calls (they are judgements, not writing —
  and Strands keeps params in the model config, so it must be merged via
  `update_config` or the factory's token cap is silently dropped); **verdicts are
  cached** by the pair of texts, since re-asking an unchanged question is a
  re-roll not a re-examination (2.4s → 0.0s measured); and `--recheck` bypasses
  the cache, because caching locks in a verdict and the one most likely to be
  wrong is "independent" — it leaves both rows live, so nothing later would
  correct it. Still conservative: unclear or failed → "independent".
  **Tests 203.**
  **Open:** the `cairn/` package rename; `cairn-eval` still not re-run since the
  Gemini swap (and the golden set has no dedup/supersession cases yet).
- **2026-08-26 (feat/cairn, later)** — **Phase 1 done: Cairn now learns from
  conversations nobody addressed to it, including ones that predate it.** See
  `DIRECTION.md` for the full write-up.
  **(A) One reader** (`cairn/conversation.py`) for context, capture and backfill,
  so a captured memory always corresponds to a conversation Cairn could also have
  read. Brought the **channel-context fallback** with it — a mention outside a
  thread now gets recent channel messages (1 model call / 10k tokens, versus 3 /
  30k when the model had to reconstruct context via the Slack API itself).
  **(B) Thread-level capture** — the whole conversation is distilled, and the
  prompt asks for the *why* and what was rejected, not just the conclusion.
  Verified live: four channel messages, no `@Cairn`, produced a memory carrying
  the reasoning ("prioritizing fewer dashboards over perfect granularity"), cited
  back correctly on a later question.
  **(C) The gate was the bottleneck.** A live test produced **zero** captures
  from four obvious decision messages: the regex matched four verbs after "let's"
  and missed "ok let's merge them into one and drop the per-service split".
  Rewritten to be permissive (a missed decision is lost silently and for good; a
  false positive costs one model call the extractor rejects) with a hedge guard
  so "we could split it per-service" does not count.
  **(D) `cairn-backfill`** walks channel history, groups it into conversations,
  and distils candidates. `--dry-run` is free (gate only), `--limit` caps model
  calls, and processed conversations are recorded so re-runs continue rather than
  re-pay.
  **Two bugs, one serious.** (1) **Cairn was about to learn from itself** — its
  replies are full of decision language because it reports decisions back, and a
  dry-run showed 7 of 14 candidates were its own essays. That would produce
  citations whose source is the citation. Bot turns are now excluded on both
  paths, and backfill attributes to the human, not the last turn. (2)
  **`extract_json` was hardcoded to Anthropic's wire format** while claiming to
  follow the configured provider — it 401'd on Gemini, so every passive capture
  failed silently. Now routed through the provider factory.
  **Tests 181** (+52 today). **Open:** near-duplicate memories (backfill
  re-phrases something already saved live; exact dedup misses it) — belongs with
  Phase 3 supersession. Also still open: the `cairn/` package rename, and
  `cairn-eval` has not been re-run since the Gemini swap.
- **2026-08-26 (feat/cairn)** — **Direction set, renamed to Cairn, Phase 0
  foundations done and verified live.** See `DIRECTION.md` (now read-first in
  `CLAUDE.md`) for the decision and roadmap. The project was three ~60% products
  (knowledge / automation / proactivity, per `gaps.txt`); it now commits to
  **organizational memory** — "decisions are made in Slack and lost in Slack" —
  with Jira and ambient demoted to supporting cast.
  **(A) Cairn.** New Slack app created from `docs/slack-app-manifest.yml`
  (requests `channels:history` + `message.channels`, which is what will let it
  read conversations it was not mentioned in). Agent identity now states the
  direction: cite who/where/when, an answer without a source is a rumour. 247
  `CAIRN_*` env vars → `CAIRN_*`. The `cairn/` package keeps its name for now.
  **(B) Provider swap, forced.** MiniMax credits ran out mid-session ("Token Plan
  usage limit reached"), so we moved to Gemini's free tier — and found the
  `gemini` branch of the provider factory had **never been exercised**: it passed
  `max_tokens`, which Gemini's `GenerateContentConfig` rejects
  (`max_output_tokens`). Also `gemini-2.5-flash` is retired; Google's own 404
  named `gemini-3.6-flash`. Both fixed. This is the first real proof the provider
  factory earns its keep.
  **(C) Latency: ~27s → ~4s.** The cost was input tokens, and the bulk was **tool
  schemas** (6.1k tokens), re-sent on every request. Dropped `think` (2.1k,
  unused — Gemini reasons natively) and compressed the Jira prompt block. MCP was
  also rebuilding the whole agent per request (`_mcp_clients` path), so it is off
  by default now.
  **(D) `CAIRN_TOOLS`.** Rather than delete capabilities, tool groups
  (slack/jira/calculator/think) became env switches — an unused tool is permanent
  latency and cost, but the decision to drop one should be reversible. Measured:
  none=468, slack=1.7k, default=4.0k, +think=6.1k tokens/request.
  **(E) Phase 0 shipped** — thread context, async dispatch, retries with backoff,
  SQLite-backed staged tickets. Details + the two bugs they surfaced are in
  `DIRECTION.md`; briefly: (1) the **cached agent was accumulating every
  conversation across all channels** (masked by MCP's per-request rebuild), and
  (2) **thread context fetched with the reply's own ts instead of the thread
  parent**, silently returning 0 turns while the model compensated via the Slack
  API at 3× the model calls and 3× the tokens. Both fixed and pinned by tests.
  **Tests 96** (was 46): +7 tool switches, +13 thread context, +7 resilience,
  +8 pending store. **Verified live on `gemini-3.6-flash`:** decision saved and
  recalled from differently-worded phrasing with provenance; threaded follow-up
  ("expand on the second one") answered from thread context at 1 model call /
  10k tokens, versus 3 / 30k before the fix.
  **Open:** thread context only fires inside a thread — a mention in the channel
  (how people actually talk) still gets nothing, so a **recent-channel-history
  fallback** is the next obvious win. Also: the `slack` tool is tool-happy; the
  `cairn/` package rename; `cairn-eval` has not been re-run since the Gemini swap.
- **2026-07-10 (session/marketplace-prep)** — **Marketplace-prep hardening; two
  root causes killed; everything re-verified live** (see `SESSION_PROGRESS.md`
  for the full session record). **(1) DBs untracked from git** — `data/cairn.db`
  + `cairn_vec.db` were deliberate `.gitignore` exceptions; every checkout swapped
  the inode under the running app → the entire WAL/readonly saga. Runtime data
  is now local-only. **(2) Reasoning telemetry fixed** — MiniMax's endpoint
  drifted: it no longer leaks inline `<think>` text and only returns `thinking`
  blocks when the request opts in. `_build_model` now sends
  `thinking={enabled, CAIRN_THINKING_BUDGET (default 1024)}`; verified live
  (reasoning persisted for real Slack turns). **(3) GHOST PROCESS found** — an
  old (~663e9a2-era) Cairn instance on another machine shares the Rasputin app
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
  `python -m cairn.main` process from Jun-24 (old WAL-mode code)** was still running
  alongside the new app, holding an unlinked `cairn.db` inode + `-wal`/`-shm` — the
  classic "silent/duplicate bot" + DB-contention cause. Kill stale processes and
  run exactly one. (2) Rewriting `data/cairn.db` with **git while the app was live**
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
  only). New `cairn/memory_capture.py` (off unless `CAIRN_MEMORY_CAPTURE=on`): decisions/
  commitments in channels Cairn merely **observes** are distilled (regex gate → focused
  model call) and saved to the long-term (vector) store **with provenance**, so
  "what did we decide about X" recall works without an `@Cairn` turn (gaps B/I/K).
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
  **(B) Native Jira tools** — `cairn/jira_tool.py`: `jira_stage_ticket` (saves to
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
    **(C) Ambient PM mode** — new `cairn/ambient.py`, off unless `CAIRN_AMBIENT=on`;
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
    limitation noted. **Evals:** `cairn-eval` 25/28 live on MiniMax-M3; +3
    `memory-scope` golden cases (channel isolation through the real agent, all pass);
    3 failures are M3 tool-happiness on smalltalk/safety (pass on retry, unrelated to
    the diff). **Open:** `cairn-eval` provider A/B + CI gate; enterprise track (authz,
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
  "playbook in Notion under SRE") **with provenance citation**, and `cairn-eval`
  memory cases pass on the hybrid backend. (2) **Exact-content dedup** on write +
  prompt nudge to not re-save on recall (model can still re-phrase → near-dup is a
  known follow-up). (3) **Slack AI Assistant** (`CAIRN_SLACK_ASSISTANT=on`):
  suggested prompts + "is thinking…" status, routed to the same agent; both apps
  connected live and attached the assistant cleanly. `.env` set to
  `hybrid`+`fastembed`+assistant-on; `pip install -e ".[vector]"`. Bumped to
  v0.4.0. Added **7 memory evals** (provenance recall, semantic recall across
  wording, selective save vs skip-chit-chat); `cairn-eval` now isolates memory to a
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
  is additive + idempotent; verified against a copy of the committed `cairn.db`
  (existing row re-indexed, recall works across wording). **Experimental, on
  branches (not merged):** `feat/vector-memory-turso` (libSQL/Turso native vector
  search + pluggable embeddings — fastembed default = no key) and
  `feat/slack-assistant` (native Slack "Assistant" AI surface). Hackathon: Cairn
  qualifies today via **MCP server integration** (`CAIRN_MCP_SERVERS`); the
  Assistant branch adds a clean **Slack AI capabilities** claim.
- **2026-06-24 (pm)** — **Multi-app support.** `slack_app._discover_apps()` +
  `start()` now run N Slack apps in one process (each its own Bolt App + Socket
  Mode thread), via `SLACK_BOT_TOKEN_<NAME>`/`SLACK_APP_TOKEN_<NAME>` pairs (bare
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` still works). One agent per app
  (`get_agent(app_name)`), each with optional `CAIRN_PERSONA_<NAME>` system-prompt
  override — the seam for giving Cairn vs Rasputin_Loop different jobs. Telemetry
  `entrypoint` is now `<app>:<kind>`. Diagnosed the "@Rasputin_Loop silent" issue:
  tokens were correct but **stale `loop` processes** (one on Cairn's old env) were
  answering `@Cairn`, and Rasputin_Loop almost certainly lacks **Event
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
  `.env` had Cairn's tokens in `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` and the
  Rasputin_Loop bot token in an unread var — fixed the bot token, flagged the
  xapp to regenerate under app `A0BCVHES936`. **Open:** verify M3's
  *Anthropic-compatible* endpoint accepts image blocks (else fall back to MiniMax
  OpenAI-compatible endpoint for images).
- **2026-06-23 (pm)** — Shipped the **v0.3.0 harness**. Fixed the `_extract_text`
  dict bug (replies were "_(no response)_"). Added a `message` handler (channel
  tracking + DM support, kills Bolt 404s). **Observability:** edge telemetry
  (`interactions`) + step tracing (`steps`) for every model call / tool call /
  reasoning chunk (`cairn.obs`/`cairn.trace`). **Guardrails:** enforcing tool-call
  limits (max calls, repeats, risky Slack methods, token budget) via Strands
  hooks + `cancel_tool`, per-rule env toggles. **Evals:** `evals/golden.json`
  (~18 cases) + `cairn-eval` runner (`eval_results` table, CI exit code).
  New modules: `context.py`, `tracing.py`, `guardrails.py`, `eval.py`. Persisted
  memory `metadata`. Updated `.env.example`, `architecture.md`.
- **2026-06-23 (am)** — Adjusted Cairn for **MiniMax M3** (Anthropic-compatible
  endpoint, env-only). Rewrote `.env.example` with Slack-token + MiniMax-key
  guidance. Created the cross-session harness: `CLAUDE.md`, `architecture.md`,
  this file. Mapped current state to the 5 Pillars and the observability ROI study.
