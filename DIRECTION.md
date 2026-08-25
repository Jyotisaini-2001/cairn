# DIRECTION — what we are building, and why

_Decided 2026-08-24. This document sets the single direction for the project and
is the tie-breaker when a future change could pull it sideways. Read it with
`architecture.md` (how it's wired) and `progress.md` (where it stands)._

**Status of the name:** proposed `Cairn`, not yet adopted. Package is still `loop`.

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

**Phase 0 — foundations** _(everything after depends on these)_
- Thread context passed to the agent (`conversations_replies`, last N turns)
- Non-blocking Slack handling (ack fast, process async)
- Timeouts + retries with backoff on provider errors
- Persist staged Jira tickets (`_pending` is in-process; lost on restart)

**Phase 1 — capture what is actually there** _(the real unlock; gaps 1, 2, 4)_
- Thread-level capture: distill a whole thread, keeping the **why**
- Backfill existing channel history
- A far better memorability gate than a keyword regex

**Phase 2 — answer questions properly** _(gaps 5, 6)_
- A cited question-answering surface, not just memories injected into chat
- Scope as an explicit policy
- Deeper recall for log-style questions

**Phase 3 — the moat** _(gap 3)_
- Decision supersession: timeline-aware memory; newer decisions mark older ones
  superseded, and recall says so
- Contradiction detection: _"this contradicts @alice's March decision"_

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

- [ ] Adopt the name `Cairn`? (rename `loop/` package, `pyproject.toml`, script
      entry, and the agent's self-description in `SYSTEM_PROMPT`)
- [ ] Start with Phase 0 (foundations) or Phase 1 (content, so there is something
      to demo)?
- [ ] Should scope default change from `channel` to something wider for
      onboarding-style questions?
