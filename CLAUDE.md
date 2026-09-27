# CLAUDE.md — Cairn project harness

This file is auto-loaded each Claude Code session. It is the **entry point** for
cross-session continuity. Read the linked docs before doing substantive work.

## Read first (cross-session memory)
0. **`PRODUCT.md`** — what Cairn is, why it is built this way, the AI concepts
   used and where, honest strengths and gaps. The one document to read if you
   need the whole picture (or are explaining the project to someone).
0b. **`DIRECTION.md`** — the single committed direction (organizational memory:
   "decisions are made in Slack and lost in Slack"). Read it first; it is the
   tie-breaker when a change could pull the project sideways.
1. **`architecture.md`** — how Cairn is wired (runtime flow, components, the
   SQLite memory store, the model-provider env contract).
2. **`progress.md`** — current status, the MiniMax M3 migration checklist, and
   the production roadmap mapped to the 5 Pillars of Production AI.
3. **`.env.example`** — the env contract (Slack tokens + where to get them,
   MiniMax M3 vs native Anthropic, storage, MCP, logging).

**When you finish a working session, update `progress.md` (Status log) and, if
the wiring changed, `architecture.md`.** Keep these honest — they are what the
next session relies on.

## What Cairn is
Organizational memory for Slack: it passively captures decisions (with their
reasoning) from channel conversations and answers "why did we…" with a source
the model cannot fabricate. One Strands agent is the only brain. Entrypoints:
`@mention`, DM, `/cairn` + `/loop` slash commands, passive capture, and the
optional Slack Assistant pane. Tool groups via `CAIRN_TOOLS` (default
`slack,jira,calculator,decisions`; `think` optional), plus long-term memory and
optional MCP servers.

## Key facts (so a fresh session doesn't re-derive them)
- **Provider is env-selected** by `CAIRN_MODEL_PROVIDER` (`agent.py::_build_model`):
  `anthropic` (default; also serves MiniMax M3 via its Anthropic-compatible
  endpoint `https://api.minimax.io/anthropic`), `gemini`, `openai`, `bedrock`,
  `ollama`, `litellm`. **The local `.env` currently runs `gemini`** — MiniMax
  credits ran out mid-project and the switch was env-only.
- **Database = local SQLite** (`DATABASE_PATH`, default `./data/cairn.db`).
  `CAIRN_MEMORY_BACKEND=hybrid` (the local setting) adds libSQL vector search in
  `cairn_vec.db`, fused with FTS5/BM25 via RRF; `sqlite` is FTS5 only. Every
  memory carries provenance (author/channel/team/date) stamped by the store, not
  the model. Replaced decisions are retired via `superseded_by`, never deleted.
  Recall is scoped by `CAIRN_MEMORY_SCOPE` (default `channel`; `team`/`global`).
  Credentials are redacted in both stores' `add()` before embedding.
- **Authorization** lives in the DB, managed from Slack (`/cairn allow|deny|who`);
  only the bootstrap `CAIRN_ADMINS` is in `.env`. Empty lists = no restriction.
- **Transport = Slack Socket Mode** — each app needs a bot token (xoxb) + an
  app-level token (xapp). **Multi-app:** one process runs N apps via
  `SLACK_BOT_TOKEN_<NAME>`/`SLACK_APP_TOKEN_<NAME>` pairs (bare
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` still works as one app). Per-app behavior via
  `CAIRN_PERSONA_<NAME>`. The running app's identity is decided by which tokens are
  loaded — verify with `auth.test`. The workspace has two apps: `Cairn`
  (`A0BC0NPB9LN`) and `Rasputin_Loop` (`A0BCVHES936`); the user wants BOTH to run.
- A Socket Mode app needs **Event Subscriptions** (`app_mention`, `message.im`)
  configured per app, or it connects but receives no events ("silent bot").
- Entry: `cairn/main.py` → `slack_app.start()`. Agent: `cairn/agent.py::run()`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[vector]"
cp .env.example .env   # fill in Slack tokens + a model key
cairn
```

**Known trap — the editable install.** Hatchling writes its path file to
`.venv/lib/python3.12/site-packages/_editable_impl_cairn.pth`, and on this setup
`site.py` does not apply it: `pip list` shows the package installed and
`import cairn` works *from the repo directory* (cwd is on the path), so nothing
looks wrong until a console script — `cairn`, `cairn-eval`, `cairn-backfill` —
fails with `ModuleNotFoundError` from anywhere else. Fix:

```bash
sp=.venv/lib/python3.12/site-packages
rm -f $sp/_editable_impl_cairn.pth
printf '%s\n' "$(pwd)" > $sp/cairn-dev-path.pth
```

Also note **moving the project folder breaks the venv** — venv scripts hardcode
their absolute path in the shebang. Delete and recreate it after any move.

## Git identity (IMPORTANT)
- **Commit as `Jyoti Saini <sainijyoti042002@gmail.com>`.** This is the repo-local
  `user.name` / `user.email`; keep it. Use `git -C <repo>` rather than `cd`.
- **NEVER commit with `jyoti@adapts.ai` (or any `*@adapts.ai`) address or git
  config.** If you find that identity set, replace it with the address above.
- **Do NOT add a `Co-Authored-By` trailer** to commits in this repo.

## Working norms
- Keep changes minimal and match the existing terse, single-brain style — no new
  intent parsers / routers / extraction pipelines.
- Provider choice stays env-driven; don't hardcode a provider in code.
- Before adding features, weigh the roadmap in `progress.md`: observability
  (Pillar 02) and evaluation (Pillar 01) are the highest-ROI gaps.
- **Every new feature extends the eval set.** The golden dataset
  (`evals/golden.json`) is required — add case(s) for the new behavior and at
  least partial tests. Features that aren't single-turn agent behaviors (infra
  like the provider factory; background loops like ambient mode) are covered by
  unit tests in `tests/` instead, since the golden runner scores one agent turn.
