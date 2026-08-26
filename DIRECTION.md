# DIRECTION — what we are building, and why

_Decided 2026-08-24. This document sets the single direction for the project and
is the tie-breaker when a future change could pull it sideways. Read it with
`architecture.md` (how it's wired) and `progress.md` (where it stands)._

**Status of the name:** adopted. Slack app, agent identity and env vars are
`Cairn`; the `loop/` Python package still carries the old name (internal only).

---

## 1. The decision

The project commits to **ONE** direction:

> ### Organizational memory — decisions are made in Slack and then lost in Slack.

Everything else in the repo becomes supporting cast. Jira triage, ambient nudges,
and action items **stay**, but they now serve this story instead of competing
with it.

### Why a decision was needed

`gaps.txt` collapses 12 observed Slack gaps into three root gaps — **Knowledge**,
**Automation**, **Proactivity**. The codebase had built something in each:

| Feature | Root gap | Verdict |
|---|---|---|
| Memory + provenance + hybrid search | Knowledge | **keep — this is the direction** |
| Passive memory capture | Knowledge | **keep — needs rework (see §4)** |
| Jira auto-triage | Automation | demote to supporting |
| Ambient nudges | Proactivity | demote; earn back in Phase 4 |
| Action items | Proactivity | demote to supporting |
| MCP tools | Automation | infrastructure, keep as-is |

Three products at ~60% each. This document picks one and finishes it.

### Why Knowledge, and not the other two

- **It is the foundation the other two stand on.** Automation without
  understanding files the wrong ticket. Proactivity without understanding is
  spam. Choosing Knowledge is sequencing, not abandoning.
- **It is our strongest existing 60%.** Provenance stamping, hybrid BM25+vector
  retrieval, per-channel scoping, passive capture — all real, all verified live.
  The other two directions are a regex and a cron loop.
- **Slack genuinely cannot do it.** Slack search matches words and returns
  messages. It cannot answer a question. (`gaps.txt` B: _"Search needs exact
  words, not meaning."_)
- **Automation is crowded** (Slack's Jira connector, Workflow Builder, Zapier,
  Linear) and demands a precision we do not have — a wrong ticket costs more
  than no ticket.
- **Proactivity is unrecoverable if wrong.** A bot that interrupts you wrongly
  gets muted permanently. Note we already distrusted it: ambient is off by
  default and forbidden from creating tickets.

---

## 2. The problem, stated properly

**An organization's decisions are made in Slack and then lost in Slack.** The
conversation happened, the decision was real, and months later nobody can find
it, nobody remembers why, and it is re-litigated — or quietly re-done
differently.

Where it bites:

| Moment | What happens today |
|---|---|
| New hire, week 2 | Asks "why Postgres and not Dynamo?" — someone answers it for the fourth time. |
| Design review, month 6 | "Didn't we already decide this?" Nobody can produce it. The debate re-runs. |
| Someone leaves | The code stays; the reasoning goes with them. |
| New project | The same mistake, because the postmortem is in a thread nobody can find. |

**The mechanism of the loss:** decisions do not look like decisions. A real one
looks like _"yeah ok let's just do that"_ — message 47 of a thread titled
something else, in a channel you were not in, eight months ago. Keyword search
cannot find that, because decisions are not keyword-shaped.

---

## 3. The product and the wedge

> **Your team's decisions, answerable — with a source.**

Four questions it must answer well:

1. **"Why did we pick X?"** — the decision, the reasoning, who, where, when
2. **"How do we do X here?"** — onboarding, tribal knowledge (`gaps.txt` I)
3. **"What did we decide last week?"** — a decision log (`gaps.txt` K)
4. **"Has this been decided already?"** — surfaced _before_ the debate restarts

Question 4 is how Proactivity is earned back later: same engine, pushed instead
of pulled.

### The wedge is the citation, not the search

```
Slack search:  "postgres"  ->  38 messages containing the word "postgres"

Cairn:  "why did we pick Postgres?"
        -> "Postgres over MongoDB for the billing service, for transaction
            guarantees; the team accepted a migration cost to get correctness.
            — @rahul in #eng, 12 March, in the schema thread"
```

**An AI answer without a source is a rumour. With who/where/when it is
evidence** — someone can open the thread and verify.

This is structurally ours to win: provenance is stamped by the store from
`RequestState` (`agent.py::_apply_provenance`), never supplied by the model. The
`add_memory` tool cannot carry metadata, so the model **cannot** fabricate a
citation. Most RAG systems bolt citations on afterwards and get them wrong.

---

## 4. Honest audit — what is real, what is missing

### Strong today
- Provenance that cannot be faked (store-side, not model-side).
- Hybrid retrieval: FTS5/BM25 + vectors fused with RRF — correct architecture,
  verified live.
- Passive capture exists (`memory_capture.py`), two-stage filter, cheap.
- Per-channel scope isolation works on both backends.

### Gaps, ranked

**RED — these decide whether the direction works**

1. **No history backfill.** Capture only sees messages after it is switched on.
   An organization's memory is the last three years of `#eng`; we have none of
   it. This is the difference between a demo and a product.
2. **Capture unit is the message, not the thread.** Only the concluding line
   matches the regex and gets saved — the reasoning that produced it is thrown
   away. Directly contradicts `gaps.txt` E (_"misses the why behind decisions"_).
   The unit of capture must be the **thread**.
3. **No supersession.** March _"we're going with Postgres"_ and June _"we moved
   billing to Dynamo"_ sit in memory as equal facts. Recall can return March's
   answer, confidently, with a citation. **A wrong answer wearing evidence is
   worse than no answer.**

**ORANGE**

4. **The memorability gate misses real agreements.** `_DECISION_RE` catches
   "we decided" / "let's go with"; it misses "yeah, do that", "fine, ship it",
   "+1", "approved". Most real agreements are boring.
5. **Channel scope fights org memory.** Default `channel` scope hides an `#eng`
   decision from `#general` — right for privacy, wrong for onboarding. Scope
   must become a deliberate policy, not a default.

**YELLOW**

6. Recall capped at 5 injected results — thin for "what did we decide last quarter".
7. No thread context — cannot ask a follow-up about the answer just given.

---

## 5. Roadmap

**Phase 0 — foundations** — ✅ **done 2026-08-26** _(verified live)_
- [x] **Thread context** (`CAIRN_THREAD_CONTEXT`) — the parent thread's recent
      turns are passed to the agent, so follow-ups resolve. Bounded by turn count
      + a 4k char cap; a missing scope degrades to no context, never a failed
      reply.
- [x] **Non-blocking Slack handling** (`CAIRN_ASYNC`) — events are acked
      immediately and the turn runs on a worker pool. Inline, an agent turn
      overran Socket Mode's ~3s deadline and Slack re-delivered the event, so the
      user got the same answer twice.
- [x] **Retries with backoff** (`CAIRN_MODEL_RETRIES`) — transient provider
      failures (429 / 5xx / timeouts) are retried, then reported in words
      ("the model is busy — try me again in a moment"). Previously a MiniMax 429
      storm reached the user as silence.
- [x] **Staged Jira tickets persist** — `_pending` is SQLite-backed with a 7-day
      TTL. A ticket waits on a human typing "yes"; any restart in between used to
      drop it silently, so the "yes" did nothing.

_Two bugs surfaced while doing this, both worth more than the features:_
- **Shared conversation state.** The cached Strands `Agent` accumulates
  `messages` across calls, and there is one per app — so every user's turns from
  every channel fed into every later request (cross-channel leakage, and a prompt
  that grows until it blows the context window). It was masked by MCP mode
  rebuilding the agent each request; turning MCP off for speed exposed it. Each
  turn now starts clean; continuity comes from thread context (correctly scoped)
  and long-term memory.
- **Thread context fetched with the wrong ts.** The fetch used the message's own
  ts — right for *replying* (it roots Cairn's answer under the mention), wrong
  for *reading*: `conversations_replies` on a reply's ts returns only that
  message. It never errored — it returned 0 turns and the model compensated by
  calling the Slack API to read the channel, producing correct answers at 3
  model calls and 30k input tokens instead of 1 and 10k. Fixed to fetch from the
  thread parent; pinned by a regression test.

**Phase 1 — capture what is actually there** — ✅ **done 2026-08-26** _(verified live)_
- [x] **Shared conversation reader** (`loop/conversation.py`) — context, capture
      and backfill all read Slack through one path, so a captured memory always
      corresponds to a conversation Cairn could also have read. Added the
      **channel-context fallback**: a mention that is not in a thread now gets
      recent channel messages (people @-mention in the channel and follow up in
      the channel; that path previously got nothing and the model spent 3 calls /
      30k tokens reconstructing context itself — now 1 call / 10k).
- [x] **Thread-level capture** — the whole conversation is distilled, not the
      line that tripped the gate, and the prompt asks explicitly for the **why**
      and what was rejected. A cooldown stops an active thread being re-distilled
      on every message. Verified live: four channel messages with no `@Cairn`
      produced *"…decided to merge the error rate dashboards and drop the
      per-service split, prioritizing fewer dashboards over perfect
      granularity"*, with provenance, cited back on a later question.
- [x] **A far better gate** — the first cut matched four verbs after "let's" and
      missed *"ok let's merge them into one and drop the per-service split"*; a
      live test produced **zero** captures from four decision messages. Now:
      stated decisions, `let's <anything>`, decision verbs on a thing, policy
      calls, and short assent (`agreed`, `+1`, `sgtm`) — with a **hedge guard**,
      since "we could split it per-service" trips the verbs while settling
      nothing. Permissive by design: a missed decision is lost silently and for
      good, a false positive costs one model call the extractor then rejects.
- [x] **Backfill** (`cairn-backfill`) — walks a channel's history, groups it into
      conversations (threads, and runs of messages separated by silence), and
      distils the candidates. `--dry-run` uses the free gate only; `--limit` caps
      *model calls*, since free tiers are tens of requests per day; processed
      conversations are recorded so a re-run continues instead of re-paying.

_Two bugs surfaced, one of them the worst this product could have:_
- **Cairn was learning from itself.** Its replies are full of decision language
  ("We decided to use Postgres because…", "Saved to memory: …", "Per @alice, the
  team agreed to…") because it reports decisions back. A live dry-run showed 14
  candidates, **7 of which were Cairn's own essays**. Feeding those into memory
  produces a citation whose source is the citation — fatal for a provenance
  product. Bot turns are now excluded from the gate on both paths, and backfill
  attributes a memory to the human who stated the decision rather than the last
  turn (which is usually Cairn's reply).
- **`extract_json` was hardcoded to Anthropic's HTTP wire format** while its
  docstring claimed it followed the configured provider. It worked by accident
  while MiniMax was Anthropic-compatible and 401'd the moment we moved to Gemini,
  so every passive capture failed silently. It now goes through the provider
  factory like everything else (`CAIRN_EXTRACT_MODEL` can route these small
  mechanical calls to a cheaper model).

_Still open from Phase 1:_ near-duplicate memories. Backfilling a decision that
was also saved live produces a re-phrased near-copy; exact-content dedup does not
catch it. Belongs with the supersession work in Phase 3.

**Phase 2 — answer questions properly** _(gaps 5, 6)_
- A cited question-answering surface, not just memories injected into chat
- Scope as an explicit policy
- Deeper recall for log-style questions

**Phase 3 — the moat** — ✅ **done 2026-08-26** _(verified live)_
- [x] **Near-duplicate collapsing.** The same decision saved live and then again
      by backfill comes back re-phrased every time, and exact-content dedup
      catches none of it: after one evening the store held 7 memories carrying 3
      facts, the Postgres decision appearing four times in four wordings. On
      write, the closest live memories are compared and a duplicate collapses
      into the existing row — keeping the *richer* wording, since a fuller
      account of the same decision is worth having and a paraphrase is not.
- [x] **Supersession.** A decision that replaces an older one retires it:
      `superseded_by` is set, and `_fetch` — the single choke point both
      retrieval legs pass through — skips retired rows. The row is kept, so the
      history stays auditable; it just stops being an answer.
- [x] **Chain-aware.** Decisions form chains (24h → 4h → 8h → 6h). Comparing
      against only the closest memory retires the middle link and leaves the
      *original* live — still well-sourced, still confidently wrong. A live run
      did exactly that, so each write is now checked against several neighbours.
- [x] **`cairn-backfill --compact`** replays the same classification over
      memories written before any of this existed. Took the live store from 10
      rows / 3 facts to 3 rows / 3 facts.

_Why thresholds alone could not do this._ Measured on the real store's own
vectors: same fact re-phrased **0.12–0.15**, same subject with the answer changed
**0.179**, different subject **0.49–0.60**. The first two bands nearly touch —
"we cap review at 24 hours" and "we moved review to 4 hours" *are* textually
similar — so distance only decides how much to ask: below `CAIRN_DEDUP_DISTANCE`
collapse for free, below `CAIRN_RELATED_DISTANCE` spend one model call to
classify duplicate/replaces/independent, above it just store.

_The classification is a model call, so it is **non-deterministic** — compacting
the live store twice gave opposite verdicts on the same pair. Three things
contain that:_
- **`temperature=0`** on every classification call. These are judgements, not
  writing; sampling variety here just means the same question gets different
  answers on different days. (Strands keeps params inside the model's config, so
  it must be merged via `update_config` or the factory's token cap is dropped.)
- **Verdicts are cached** by the pair of texts. Same texts, same question — the
  only thing that would change the answer is the model's own randomness, so
  re-asking is a re-roll rather than a re-examination. Measured: 2.4s → 0.0s on
  a repeat.
- **`--recheck` bypasses the cache.** Caching *locks in* a verdict, including a
  wrong one, and the verdict most likely to be wrong is "independent" — it
  leaves both rows live, so nothing later would correct it. This is the escape
  hatch for exactly that.

_And it stays conservative by construction:_ an unclear or failed answer falls
back to "independent", because a stale row is a much smaller problem than a live
decision wrongly retired, which stops being answerable at all and is far harder
to notice.

_Not done, and only worth it if this misfires in practice:_ majority voting over
three calls (3× the cost), deterministic rules for common shapes ("same subject,
same unit, different number" needs no model call), and a human confirmation step
before retiring a decision.

**Phase 4 — earn proactivity back**
- Surface a relevant past decision when a debate restarts. Same engine, pushed.

Jira is reframed throughout: **file a ticket that cites the decision it came from.**

---

## 6. Rules that follow from this direction

- **The citation is the product.** No un-sourced answers. If provenance is
  missing, say so rather than answering anyway.
- **Recall correctness beats recall volume.** A confident stale answer is the
  worst possible failure mode; prefer "superseded" or "unsure" over a clean lie.
- **The unit of knowledge is the thread**, not the message.
- **Anything that does not serve "answer a question with a source" is supporting
  cast** and must not grow at the direction's expense.
- Existing project norms still hold: every feature extends `evals/golden.json`
  (or `tests/` for non-single-turn work), and `progress.md` is updated each
  session.

---

## 7. Stack decisions (settled, not revisiting for now)

| | Choice | Note |
|---|---|---|
| Model | **MiniMax M3**, unchanged | Provider stays env-selected. `CAIRN_MODEL_PROVIDER` already supports anthropic / litellm / openai / bedrock / ollama / gemini. |
| Storage | **SQLite (+ hybrid libSQL vectors)**, unchanged | Correct for this scale; zero ops. Postgres+pgvector or DynamoDB/OpenSearch are the enterprise path, deferred. |
| Model benchmarking | Open task | Run `loop-eval` across MiniMax / Kimi / Qwen via one OpenRouter key and record the numbers here. |

---

## 8. Open decisions

- [x] Adopt the name `Cairn`? — **yes.** Slack app, `SYSTEM_PROMPT` identity and
      all 247 `LOOP_*` env vars are renamed. Still outstanding: the `loop/`
      package dir, `pyproject.toml`, the console script, `loop.*` logger
      prefixes, and the prose in README/architecture — one deliberate pass.
- [x] Start with Phase 0 or Phase 1? — **Phase 0 first** (done 2026-08-26); its
      reliability gaps would otherwise have been rediscovered the hard way, and
      thread context is a prerequisite for Phase 1's thread-level capture.
- [ ] Should scope default change from `channel` to something wider for
      onboarding-style questions?
