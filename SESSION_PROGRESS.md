# Session progress — marketplace-prep (2026-07-10)

**Branch:** `session/marketplace-prep` (from `development` @ 5940f68).
**Goal:** finishing touches + verification before Slack marketplace submission (hackathon).
**If you are a fresh session picking this up: read this file top to bottom, then `progress.md`.**

## Decisions taken this session
1. **Base = `development` HEAD, NOT 663e9a2.** The post-663e9a2 breakage (loop.db
   unopenable, readonly-DB telemetry, WAL confusion) was *environmental*, not code:
   `data/loop.db` + `data/loop_vec.db` are **tracked in git** (deliberate `.gitignore`
   exceptions), so every checkout/stash swaps the DB inode under a live process →
   wedged connections → "readonly database" → the whole WAL saga. HEAD also contains
   fixes worth keeping (name-first assignee, <think> stripping, dedup refactor).
   Fix the root cause (untrack DBs) instead of rewinding 11 commits.
2. Live verification is mandatory: run `loop` itself + read its logs (the failure
   mode of previous sessions was never running the app).
3. Ambient mode is ON in `.env` — force `LOOP_AMBIENT=off` for any test runs so the
   bot doesn't post nudges into the workspace mid-test.

## Task list (status updated as work lands)
- [ ] 1. Session branch + this doc committed
- [ ] 2. Audit commits post-663e9a2; compile "what was I building" list
- [ ] 3. Untrack data/*.db from git; fix .gitignore (root-cause fix)
- [ ] 4. Observability: interactions.reasoning saved as "" — diagnose live, fix
- [ ] 5. Jira assign-by-name ("ishan" → email) — verify live against real workspace
- [ ] 6. Hardcoded-credential sweep (code, evals, docs, committed DBs, history)
- [ ] 7. Run `loop` live, read logs, confirm telemetry + both apps connect
- [ ] 8. Hackathon HTML page (capabilities, pitch, Knowns/Unknowns map)

## Facts a fresh session needs
- `.env` (gitignored, DO NOT commit): 2 Slack apps (rasputin = bare tokens,
  loop = `_LOOP` suffixed), MiniMax M3 via `ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`,
  `LOOP_MEMORY_BACKEND=hybrid`, Jira = jyotisworkspace-33955498.atlassian.net,
  auth user js.saini267@gmail.com (this is why tickets "default" to that email —
  it's the reporter/auth identity, not an assignee fallback; code has NO default-email
  assignee fallback: unresolved → unassigned + visible flag).
- `.env` header says tokens were shared in chat → **rotate all tokens before
  marketplace submission**.
- Committed `data/loop.db` is a stale snapshot (schema even lacks the
  reasoning/response columns — they're added by runtime migration).
- No stale `loop` processes were running at session start (checked ps).

## 🚨 CRITICAL DISCOVERY — a GHOST PROCESS is answering Slack events
A second `loop` instance built from ~663e9a2-era code (staging message has no
`Project:` line — matches `git show 096f67e:loop/slack_app.py`) is connected to
the SAME Rasputin app token from ANOTHER machine (not this one: lsof/ps clean).
Slack Socket Mode round-robins events across connections, so ~half of all live
tests hit pre-fix code. **This explains every "the fix didn't work" report from
previous sessions** (assignee misrouting, thinking leaks, etc.).
**Fix: regenerate the Rasputin app-level token (and Loop's too) at
api.slack.com/apps → Basic Information → App-Level Tokens, update `.env`.**
The ghost then can't reconnect. Until that happens, any live Slack test may be
answered by the ghost — check the local log for the request id to attribute.

## Verified live this session (my process, via log request ids)
- 2 apps connect (loop + rasputin), auth.test auto-registers bot ids.
- Mention → agent → calculator ×2 → in-thread reply; telemetry row with
  reasoning_chars=246 (thinking-param fix works in production path).
- `_slack_email_from_name`: "ishan"→bha***@gmail.com, "Jyoti"→js.***@gmail.com,
  "Jaimin"→vil***@gmail.com, unknown→None (files unassigned, NO default email);
  name beats echoed requester mention; both emails resolve to Jira accountIds.
- Vector DB healthy: 6 rows, 384-dim embeddings, provenance stamped.

## Fixes landed this session
1. b3eb3f2 — data/*.db untracked from git (the WAL/readonly root cause).
2. 198a6dc — thinking param (LOOP_THINKING_BUDGET, default 1024) so
   interactions.reasoning fills; MiniMax stopped emitting inline <think> and
   only returns thinking blocks when asked. + BOT_IDS unhardcoded (auth.test
   self-registration + LOOP_BOT_USER_IDS seed).
3. (pending commit) placeholder ids in docstrings; architecture.md updated.

## What the commit history says you were building (task 2)
The arc, in order — each stage kept the "one Strands agent is the only brain" rule:
1. **MVP: a Slack memory layer** (60ee78d→6ea173f) — MCP, Block Kit, follow-ups;
   heuristics replaced by a real LLM agent loop.
2. **Strands-native rewrite** (84010f0) — Python, one agent, tools = slack/memory.
3. **Production harness v0.3** (2bc7194→663e9a2) — multi-app (Loop+Rasputin), file/
   multimodal input, telemetry (interactions+steps), guardrails, golden evals.
   Framing: the 5 Pillars of Production AI; observability+evals before features.
4. **Memory as the moat** (b168c5b→62bed64, v0.4) — episodic provenance-stamped
   memory, FTS5, hybrid libSQL vectors + RRF, dedup, native Slack Assistant pane.
5. **Provider freedom** (696da6c/f473a4c) — env-selected provider factory (6
   backends), per-channel memory isolation, ambient PM mode. Positioning: the
   open/self-hostable answer to Claude Tag.
6. **Jira auto-triage** (200f5f3→) — bug detect → stage → human "yes" → create,
   with smart assignee routing (Gap F: manual data movement).
7. **Finishing work post-663e9a2** — action items, multi-MCP, jira search/dedup,
   think-hiding + reasoning persistence, live project-key resolution,
   assign-by-bare-name, passive memory capture, telemetry self-heal, dedupe
   refactor.

**Post-663e9a2 features: verification status**
- Jira assign-by-name + name-first priority: ✅ live (KAN-22 → ISHAN KUMAR).
- Project resolution ("KAN"), dedup check, staging, confirm flow: ✅ live.
- Thinking hidden from Slack + persisted to DB: ✅ live (fixed this session —
  MiniMax now requires the thinking param; see LOOP_THINKING_BUDGET).
- Telemetry auto-reconnect: ✅ unit-tested (4 tests); root cause also removed
  (DBs untracked).
- Action items / passive memory capture / multi-MCP: ✅ unit-tested; OFF in
  current .env (LOOP_ACTION_ITEMS / LOOP_MEMORY_CAPTURE unset, no MCP servers
  configured). Enable before demoing those.
- Journal mode DELETE (external viewers): ✅ code + live DB opens fine.
- Redundant-code note: 5940f68 already deduped the append-era duplication
  (llm.py now centralizes model-JSON calls); c6f6cc9 was a DB-blob-only dup
  commit of 096f67e — moot now that DBs are untracked.

## Eval-runner truth bugs found by running the full golden set (23/28 → see below)
1. **Phantom tool attribution** — `_result_to_run` fell back to
   `result.metrics.tool_metrics` (LIFETIME-cumulative on a cached agent) when a
   turn used no tools → smalltalk turns "inherited" every tool ever used. Fixed:
   tracer state is authoritative. This also cleaned production telemetry.
2. **Conversation-history carryover** — eval reused one cached agent across
   cases, so case N saw case N-1's turns (the "zephyr leaked cross-channel"
   failure was history, NOT a memory-store leak; the store's channel filter is
   correct). Fixed: fresh agent per case.
3. **Stale case** — `mem-add-buildbreak` ("build is broken") predates the
   Jira-priority rule; staging a ticket IS the designed behavior. Repurposed as
   the Jira golden case (first one!); added `mem-add-onboarding` to keep
   memory-add coverage. Set = 29 cases.

**Marketplace-relevant implication of (2) in PRODUCTION:** one cached agent per
app carries conversation history across ALL channels/users until restart —
memory recall is channel-scoped, but raw chat history is not. Fine for one team;
a privacy seam for multi-workspace distribution. Flagged in the artifact's
unknowns; per-channel/thread history (or a conversation-manager reset) is the
named fix, not built this session.

## Jira "default email" explained (user question)
Tickets are FILED AS the API token's owner (reporter = js.saini267@gmail.com =
JIRA_USERNAME) — Jira basic-auth semantics, not a code fallback. Assignee never
falls back to any default: named person → email → account, else unassigned +
visible flag. To change the reporter, use a token from a different Jira account.

## Credential sweep result (task 6)
- Code: clean after BOT_IDS fix. Tests use fake @x.com fixtures (fine).
- `.env` never committed; no tokens anywhere in git history; DBs carry no secrets.
- README/.env.example use placeholders only.
- ⚠️ `.env` header says live tokens were once shared in chat → ROTATE before
  marketplace: Slack bot+app tokens (both apps), MiniMax key, Jira API token.

## Final scoreboard (end of session)
- Unit tests: 46/46. Golden set: 26/29 (89.7%), tool-selection 90%, on MiniMax
  M3, scored truthfully. 3 misses = 2 M3-nondeterminism flakes (calc-compound,
  mem-recall-provenance-who — both pass on retry) + mem-add-buildbreak, which
  caught real escalation (no-thread eval context → model jumped from stage to
  create and FILED KAN-23). Evals are now Jira-inert by default
  (`--use-real-jira` opts back in); the case passes (stages, never creates).
- Live: KAN-22 assigned to ISHAN KUMAR from bare name "ishan" — full pipeline.
- Artifact (hackathon field guide) published:
  https://claude.ai/code/artifact/592bb186-c99b-4dbd-a952-299e1287c7a8
- A `loop` process (current code, LOOP_AMBIENT=off) was LEFT RUNNING (pid 39369,
  log: scratchpad loop_live.log) so current-code answers keep flowing; restart
  with plain `loop` for ambient per .env. Test tickets created today: KAN-19..23
  (KAN-21 "Bug" and others were the GHOST's work; KAN-23 was the eval before the
  Jira-inert fix) — clean up in Jira at leisure.

## Round 2 (same day, user follow-ups) — all verified
- **Thinking persisted per interaction:** confirmed in loop.db — every row since
  the fix has reasoning (445/246/1316 chars); row 16 was the pre-fix control.
- **Ambient verified live:** daemon scanned #new-channel, correctly NOOPed a
  resolved thread (telemetry row `rasputin:ambient`, reasoning captured). Note:
  ambient IGNORES bot-authored messages by design, and a NOOP scan arms the
  re-nudge guard for that thread. Posting path: live-verified 2026-07-01 + 5
  unit tests. To demo a nudge: human asks an unanswered question, stale=0.
- **Passive capture → vector DB verified + ENABLED:** capture() distilled a
  decision into loop_vec.db row 7 (kind=decision, provenance, embedding).
  `LOOP_MEMORY_CAPTURE=on` now set in .env. Before this, ONLY agent turns
  (@mention/DM/assistant) wrote memory; ambient never writes memory.
- **Slack screenshot:** the 4 bug-report messages in #new-channel were this
  session's tests (round-robin probe + KAN-22 flow) — safe to delete.
- **Jaimin:** not in the Jira workspace (user confirmed) — explains any
  assign-to-Jaimin failure; Slack-side resolution works, Jira account lookup
  can't. Test assignments with Jyoti/ishan. NOTE: Jyoti's email == JIRA_USERNAME
  (auth identity), so ishan (KAN-22) is the cleaner no-confound proof.
- **Creds:** all load from .env (load_dotenv in main.py/eval.py); .env.example
  never read by code; 13/13 required vars set in .env.
- **Showcase pages repo-tracked:** docs/presentation.html (new keynote-scroll
  design for the stage) + docs/field-guide.html (knowledge base). README gained
  Showcase links, mermaid runtime flowchart + loop.db ER diagram; stale
  "committed loop.db" claim fixed; users:read.email scope documented.
  Artifacts: presentation https://claude.ai/code/artifact/41a07752-4981-4697-9f47-2149fbe354cb ·
  field guide https://claude.ai/code/artifact/592bb186-c99b-4dbd-a952-299e1287c7a8
- **No loop process left running** (test processes stopped). Start: `loop`
  (or `python -m loop.main`). Stop: Ctrl+C, or `pkill -f "loop.main"`.

## Log
- (start) Investigated repo, read jira_tool/observability/agent/tracing/storage,
  confirmed DB-in-git root cause, created branch + this doc.
- Ran `loop` live (LOOP_AMBIENT=off), both apps connected; discovered ghost
  process mid-test via old-format staging reply + missing log line.
- Fixed reasoning telemetry (thinking param), BOT_IDS hardcoding, eval truth
  bugs (phantom tools, history carryover), eval Jira side-effects. Verified
  KAN-22 end-to-end. Published artifact. All 8 session tasks complete.
