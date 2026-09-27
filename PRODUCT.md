# Cairn — the product, in one place

_Everything about what this is, why it is built the way it is, and how to talk
about it. Written to be read cold, months later, before an interview._

Companion docs: `DIRECTION.md` (what it is for), `architecture.md` (how it is
wired), `progress.md` (what happened and when), `VERIFY.md` (how to test it).

---

## 1 · The thirty-second version

> Every team makes decisions in Slack and then loses them. Six months later
> nobody can find why the review window is four hours, so the debate runs again.
> Search cannot help, because decisions do not look like decisions — they look
> like "yeah ok let's do that", buried in message 47 of a thread about something
> else.
>
> Cairn reads channel conversations without being mentioned, works out which
> ones contained a decision, and stores the decision *with its reasoning* and
> its source. Ask it later and it answers with who decided it, where, and when.
>
> It is named after the stacks of stones hikers leave on a trail so the next
> person finds the path.

**Two things make it more than a search wrapper**, and they are the answer to
"what is the heart of this project":

1. **The citation is something the model cannot fabricate**, because the model is
   never given the field.
2. **It knows when a decision has been overturned**, and stops answering with the
   old one.

Everything else is in service of those two.

---

## 2 · The problem, precisely

Three failures, all the same root cause:

| When | What happens |
|---|---|
| New hire, week 2 | Asks a question whose answer already exists; someone types it out for the fourth time |
| Design review, month 6 | "Didn't we decide this?" Nobody can produce it. The debate re-runs |
| Someone leaves | The code stays; the reasoning goes with them |

**Why search fails.** Slack search matches *words*. A real decision is
`"ok let's just do that"` — no keywords, no marker, no structure. And the thing
people actually need is the **why**, which is in the twelve messages *before* the
conclusion, not in the conclusion itself.

**Why "just write it down" fails.** Every tool in this space asks the team to
mark decisions as they happen. That demos well and dies in week two, because the
moment a decision is made is exactly the moment nobody wants to stop and file it.
A memory that depends on people remembering to use it is not a memory.

---

## 3 · How it works, end to end

```
  A conversation happens in a channel. Nobody mentions Cairn.
        │
        ▼
  GATE          cheap pattern check — is this even a decision?      (no model call)
        │
        ▼
  READ          the whole conversation, not the one line that matched
        │
        ▼
  DISTIL        one model call: what was decided, WHY, what was rejected
        │
        ▼
  REDACT        strip API keys, passwords, emails                   (before embedding)
        │
        ▼
  STAMP         who / which channel / when — attached by the storage layer
        │
        ▼
  DEDUP         is this the same decision already stored?
  SUPERSEDE     or does it replace one? → retire the old row
        │
        ▼
  STORE         SQLite, with a vector for meaning-based search

  ─────────────────────────────────────────────────────────────────

  Someone asks a question in Slack, weeks later.
        │
        ▼
  RETRIEVE      keyword search + meaning search, fused; retired rows excluded
        │
        ▼
  ANSWER        the decision, its reasoning, and its source
```

---

## 4 · The AI concepts used — what, where, and why

This is the glossary to have ready. Every term below is used *in the codebase*,
not aspirationally.

### Agent
A model that can decide to use tools, in a loop: think → call a tool → read the
result → think again → answer. Built on **Strands Agents** (AWS's framework).

**Where:** `cairn/agent.py`. **Why a framework:** the loop, tool schemas,
provider abstraction and hook points are all things you would otherwise write
badly yourself.

**Worth knowing:** there is *one* agent — no intent classifier, no router, no
pipeline of specialised sub-agents. The model decides; guardrails constrain it.

### Tools
Ordinary Python functions the model may call. The function's docstring *is* the
instruction manual the model reads.

**Where:** `slack`, `jira_*`, `calculator`, `decision_log`, plus memory.
**Why switchable** (`CAIRN_TOOLS`): every tool's schema is re-sent on **every**
request, so an unused tool is a permanent latency and cost tax. Removing one
unused tool cut ~2,100 tokens per message.

### Ingestion
Getting raw material into the system. Here: reading Slack conversations —
live (passive capture) and historical (`cairn-backfill`).

**Where:** `cairn/conversation.py` is the single reader; capture, backfill and
the agent's context all use it, so a captured memory always corresponds to a
conversation Cairn could also have read.

### Chunking / distillation
Most RAG systems chop documents into fixed-size chunks. Cairn does not, because
the unit here is a *decision*, not a paragraph.

**Where:** `cairn/memory_capture.py`. A model call turns a whole conversation
into two to four sentences: what was decided, why, and what was rejected.
**Why it matters:** storing the raw transcript would preserve the words and lose
the point; storing only the concluding line preserves the point and loses the
reason. The distillation is the product.

### Embeddings
Text turned into a list of numbers, so "meaning" becomes "distance". Two texts
about the same thing land near each other even with no shared words.

**Where:** `cairn/embeddings.py`, default **fastembed** — runs locally, no API
key, no per-call cost. **Why local:** a team installing this into their own Slack
should not need a second vendor account.

### Vector search / semantic search
Finding by meaning. "signing in never completes" finds "the login page hangs
after submitting credentials" — no shared keywords.

**Where:** libSQL's native vector search inside SQLite.

### Keyword search (BM25)
The classic ranking algorithm — matches literal words, weighted by rarity.

**Where:** SQLite FTS5. **Why keep it:** vector search is bad at exact tokens.
Ticket keys, person names, error codes — only keyword search finds those.

### Hybrid retrieval + RRF
Both searches run, and **Reciprocal Rank Fusion** merges the two ranked lists:
each result scores `1/(k + rank)` from each list, and the sums decide.

**Where:** `cairn/vector_store.py`. **Why:** neither half is sufficient alone,
and RRF combines rankings without needing the two scores to be comparable —
which they are not.

### RAG (Retrieval-Augmented Generation)
The general pattern: fetch relevant documents, put them in the prompt, let the
model answer from them.

**Where:** this is what happens before every reply. **How Cairn differs from
textbook RAG** — three ways, and this is a good interview answer:
1. What is retrieved is a *distilled decision*, not a document chunk.
2. The citation is attached by the store, not generated by the model.
3. Superseded entries are excluded, so retrieval is time-aware.

### Memory — short-term and long-term
- **Short-term:** the current conversation. Cairn **re-reads the Slack thread**
  each turn rather than accumulating history inside the agent.
- **Long-term:** the decision store. Permanent, sourced, searchable by meaning.

**Where:** `cairn/conversation.py` and `cairn/storage.py` / `vector_store.py`.

**Why short-term is re-read rather than accumulated:** the agent is cached per
Slack app, and a Strands `Agent` accumulates its own message list. Left alone,
every user's turns from every channel fed into every later request —
cross-channel leakage and a prompt that grows without bound. Each turn now starts
clean (`agent.messages = []`) and continuity comes from the thread, which is
correctly scoped by construction.

### Provenance
Who said it, where, when — attached to every memory.

**Where:** `cairn/agent.py::_apply_provenance`, read from Slack's event payload.
**Why it is the centrepiece:** see §5.

### Guardrails
Code that can cancel a tool call *before it runs*.

**Where:** `cairn/guardrails.py` — caps tool calls per request, blocks repeated
identical calls, enforces a token budget, and refuses destructive Slack methods
(`chat.delete`, `conversations.archive`, `admin.*`).
**Why:** you cannot make a model behave through instructions. You put a
checkpoint in the path of every action instead.

### Observability / tracing
Every model call, tool call and reasoning step is recorded to SQLite.

**Where:** `cairn/tracing.py`, `cairn/observability.py` — the `interactions` and
`steps` tables. **Why it earned its place:** most of the real bugs in this
project were found by reading these rows, not by reading code. One example: an
answer looked perfect while the telemetry showed 3 model calls and 30k tokens
where 1 and 10k should have done — a silent inefficiency no test would catch.

### Evals
A test suite for *behaviour* rather than code: scenarios run through the real
agent, scored on which tools fired and what the reply contained.

**Where:** `evals/golden.json` (35 cases), `cairn-eval`.
**Why they are not unit tests:** the model is non-deterministic, so the result is
a score to track, not a pass/fail. A well-run eval set answers "did my change
make it worse?", which nothing else can.

### Non-determinism
The same question can get different answers. Not a bug — how the technology
works.

**Where it bites:** the duplicate/replacement classifier gave *opposite verdicts
on the same pair* across two runs. **What was done about it:** §6.

### Model-provider abstraction
Anthropic, Gemini, OpenAI, Bedrock, Ollama and LiteLLM behind one environment
variable.

**Where:** `cairn/agent.py::_build_model`. **Why it earned its keep:** MiniMax
credits ran out mid-project and the whole system moved to Gemini without a code
change. It also exposed two latent bugs — the Gemini branch had never been run,
and a helper was speaking Anthropic's wire format directly while claiming to
follow the configured provider.

### MCP (Model Context Protocol)
A standard letting tools live in separate programs any AI app can plug into —
USB for AI tools.

**Where:** `CAIRN_MCP_SERVERS`. **Status:** supported, off by default, because
the current implementation rebuilds the agent per request.

---

## 5 · The heart: two ideas

If you remember nothing else about this project, remember these two — and that
**both are enforced by structure, not by instruction.**

### 5.1 · A citation the model cannot fake

Most RAG systems ask the model to produce the answer *and* its source together.
That makes the citation a thing the model can be **wrong about** — and a
fabricated source arrives in exactly the same confident formatting as a real
one. The one part a reader would use to check the rest is the part most likely
to be invented.

In Cairn, who/where/when comes from **Slack's event payload**, read by ordinary
code before the model is involved, and attached by the storage layer. The memory
tool the model can call takes **one argument: the text.** There is no field for
an author, so it cannot supply a wrong one.

> An AI answer without a verifiable source is a rumour. A rumour with a fake
> citation is worse than one without.

**Rejected: let the model cite its own sources.** Simpler, and what most systems
do. It also means the trustworthy-looking part is the least trustworthy part.

### 5.2 · Memory that forgets, following the chain

Most memory systems only add, because appending is easy and forgetting is a
judgement call. The cost is invisible at first and then permanent: a stale
decision, correctly cited, served as current — worse than returning nothing,
because the citation makes it look checked.

When a new decision replaces an earlier one, the old row is **retired**
(`superseded_by`), not deleted — history stays auditable, it just stops being an
answer. And retirement follows **chains**: 24h → 4h → 8h → 6h leaves exactly one
live answer, not four. Comparing only against the closest memory retires the
middle link and leaves the *original* live, which a live run did before it was
fixed.

One line enforces it, in the single place both retrieval paths pass through:

```sql
WHERE id IN (...) AND superseded_by IS NULL
```

**Rejected: append-only memory.** Cheaper to build, and the failure never
announces itself. You find out when someone acts on a decision the team reversed.

---

## 6 · Designing around non-determinism

Deciding whether a new decision *replaces* an old one is a model call, so it is
not repeatable. Compacting the live store twice gave **opposite verdicts on the
same pair**.

The options were to keep tuning until it looked stable, or to build something
that stays correct while it is unstable. Three things:

1. **`temperature=0`** on classification calls — take the most likely answer
   rather than sampling. These are judgements, not writing.
2. **Verdicts are cached** by the pair of texts. Same texts, same question — the
   only thing that could change the answer is the model's own randomness, so
   re-asking is a re-roll, not a re-examination. (2.4s → 0.0s on a repeat.)
   `--recheck` bypasses the cache, because caching *locks in* a verdict and the
   one most likely to be wrong is "independent" — it leaves both rows live, so
   nothing later would correct it.
3. **The tie-break is biased toward keeping.** Unclear or failed → keep both.

That bias is the whole point, and it comes from the failure modes being wildly
asymmetric: **wrongly retiring a live decision is invisible and unrecoverable** —
the answer simply stops existing and nobody knows to look. One redundant row is
neither.

### The measurement, and why it is the strongest thing to quote

Thresholds were **measured**, not chosen. First pass, against a store holding
three memories, suggested a narrow gap. Re-measured against a 44-decision corpus
with real topic overlap:

```
same decision, reworded ....... 0.176 – 0.266
replaced (answer changed) ..... 0.236 – 0.332   ← overlaps the above
independent, same topic ....... 0.460 – 0.569
```

The first two bands **overlap**. Distance cannot distinguish "the same decision
said differently" from "the decision changed", and no threshold will. What *does*
separate cleanly is related vs not — a 0.128 gap. So similarity decides **how
much to ask**, and a model call decides **what the relation is**.

Re-runnable: `python -m evals.measure_thresholds`.

---

## 7 · Why not an off-the-shelf memory layer

**mem0, Zep, LangMem** all do extraction, storage, semantic retrieval, and some
form of conflict resolution. Starting with one would have been faster.

Three things they do not give you:

| | |
|---|---|
| **Provenance the model cannot touch** | their memories are extracted text; the source is generated alongside the answer |
| **Supersession chains** | conflict resolution exists, but not "retire every earlier version and leave one live answer" |
| **Slack-native passive capture** | reading conversations nobody addressed to the bot, and distilling the *conversation* rather than the message |

**The honest framing:** knowing the landscape and choosing to build is
respectable. Not knowing it is not. Name them.

---

## 8 · Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | |
| Agent framework | **Strands Agents** | AWS-backed, lightweight, provider-agnostic, real hook points |
| Slack | **slack_bolt**, Socket Mode | no public URL or webhook endpoint needed |
| Model | provider factory — currently **Gemini** | one env var; migrated live from MiniMax M3 |
| Store | **SQLite** + **libSQL** vector search | one file, no service to run, no second vendor |
| Embeddings | **fastembed** (local) | no API key, no per-call cost |
| Retrieval | FTS5 (BM25) + vectors, fused with **RRF** | neither half suffices alone |
| Integrations | **Jira REST**, **MCP** | |
| Docs | pypdf, multimodal image input | |

**Scale of the codebase:** 23 modules, ~7,100 lines of Python, ~3,200 lines of
tests, **306 unit tests**, 35 golden eval scenarios, 71 environment switches.

---

## 9 · Where it is strongest, and where it is not

### Strongest
- **The two structural guarantees** (§5). Not features — properties.
- **The harness.** Step-level tracing, enforcing guardrails, an eval runner.
  Most side projects have none of this.
- **Provider independence, proven** by an actual mid-project migration.
- **Decisions are measured, not guessed** — and the measurements are recorded in
  the code with their reasoning.
- **Honest documentation.** `progress.md` records what broke and why, including
  mistakes. That is unusual and worth pointing at.

### Needs improvement — say these before you are asked
- **No eval baseline has ever been run.** The harness and 35 cases exist; no full
  pass has completed, because ~40 model calls against a free tier of 20/day/model
  cannot finish in one sitting. Until that number exists the honest claim is
  "verified by hand".
- **Tested at tiny scale.** The live store has held single-digit memories.
  Thresholds were re-measured against a 44-decision corpus, which is far better
  than three and still synthetic.
- **Several surfaces have never run in production use** — the slash command, DMs,
  resurfacing. Untested code is not working code.
- **Single-workspace.** No multi-tenancy, no per-workspace isolation.
- **Cost is unbounded per user.** No spend caps, no retention policy.
- **Distillation quality is unmeasured.** A model decides what a conversation
  "decided"; nobody has scored how often it is right.

---

## 10 · How it scales

**What already scales:** retrieval is indexed (FTS5 + vector index); capture is
one model call per conversation, not per message; the gate is a regex, so most
messages cost nothing; guardrails cap per-request spend; every heavy step runs
off the Slack event thread.

**What breaks first, in order:**

1. **SQLite write concurrency** — fine for one workspace, wrong for many.
   → PostgreSQL + pgvector; the memory store is an interface with two
   implementations already, so this is one new class.
2. **Model cost at volume** — every message is a potential model call.
   → cheaper model for classification (`CAIRN_EXTRACT_MODEL` exists), per-team
   caps, batching.
3. **Retrieval precision at thousands of decisions** — the top-5 window gets
   crowded. → re-ranking, and scope filters that already exist per channel/team.
4. **One process per workspace** — → queue-based workers; the async dispatch
   layer is already the seam.

**What does not need to change:** the direction, the two guarantees, and the
store interface.

---

## 11 · Integrations, present and possible

**Today:** Slack (mentions, DMs, slash command, passive listening, AI pane),
Jira (create, search, update, assignee resolution from a bare first name,
semantic duplicate detection, decisions cited on tickets), MCP (any compliant
tool server), six model providers.

**Natural next:** GitHub (cite the decision behind a PR), Notion/Confluence
(ingest existing docs as decisions), Linear, calendar (capture decisions from
meeting notes), a web UI for browsing the decision log.

**The pattern:** anything that can be read becomes ingestion; anything that can
be written becomes a place to cite a decision.

---

## 12 · Who it helps, and how

**New joiners** stop asking questions whose answers exist. **Teams** stop
re-litigating settled debates. **Leads** get a decision log they never had to
maintain. **Organisations** stop losing reasoning when people leave.

The measurable claim, if asked for one: an engineer at a 50-person company loses
a couple of hours a week to questions that already have answers. That is the
number to *investigate*, not to assert — it is a plausible estimate, not
something this project has measured.

---

## 13 · Direction and future plan

**The committed direction is organisational memory** (`DIRECTION.md`). Two other
directions were identified in `gaps.txt` and deliberately deferred:

- **Automation** (natural language → Jira/GitHub) — crowded, and needs precision
  the detector did not have.
- **Proactivity** (nudges, alerts, digests) — highest risk: a bot that interrupts
  wrongly gets muted permanently, and nudging well requires understanding first.

**The sequencing argument held.** Proactivity became viable *because* the
knowledge layer existed — "someone is re-opening a settled question, here is what
was decided" is only possible once decisions are understood. Jira likewise: "file
a ticket that cites the decision it came from" was impossible before.

**Next, in order:**
1. Run the eval baseline and record the number.
2. Soak: use it daily so the store fills with real conversations.
3. Re-measure thresholds against that real store.
4. Multi-tenancy and cost caps, if it goes further than one workspace.

---

## 14 · Interview questions to expect

**"What is the hardest problem you solved?"**
Deciding whether a new decision replaces an old one. Distance cannot separate
"same fact reworded" from "the answer changed" — the bands overlap. So similarity
decides how much to ask and a model call decides the relation, and because that
call is non-deterministic, the system is built to stay correct while it is
unstable.

**"Why not use mem0 / Zep / LangMem?"**
§7. Name them first, then the two things they do not do.

**"How do you know it works?"**
306 unit tests for the deterministic parts, and a 35-scenario eval set that runs
through the real agent for the behavioural ones. **Be honest:** the eval baseline
has not been run to completion yet — free-tier quota — so the current claim is
verified by hand.

**"What would you do differently?"**
Run the eval set from the start rather than building the harness and not using
it. And measure thresholds against realistic data first — the initial numbers
came from three memories and did not survive contact with a proper corpus.

**"What broke in production?"**
Plenty, and it is all in `progress.md`. Best examples: an agent cached per app
was accumulating every user's conversation across every channel; a dedup fix went
into one of two code paths and the other one — the one people actually hit — kept
filing duplicates; a helper spoke one provider's HTTP format directly while
claiming to be provider-agnostic, and 401'd silently the day the provider changed.

**"How is this different from just using ChatGPT with search?"**
It reads conversations nobody addressed to it, it stores the reasoning rather
than the transcript, its citations cannot be fabricated, and it knows when a
decision has been overturned. The last two are the ones nothing else does.

**"What is the weakest part?"**
Distillation quality is unmeasured — a model decides what a conversation
"decided", and nobody has scored how often it is right. That is the next eval to
write.

---

## 15 · Numbers worth remembering

| | |
|---|---|
| Unit tests | **306** |
| Golden eval scenarios | **35** |
| Modules / lines | 23 / ~7,100 Python, ~3,200 test |
| Env switches | 71 |
| Model providers supported | 6 |
| Latency, typical answer | ~4s |
| Token cost per message | ~5,000 in |
| Tokens saved by removing one unused tool | ~2,100 per message |
| Semantic duplicate detection, real Jira | matched at 0.090 distance, **no shared keywords** |
| Threshold gap between "related" and "not" | 0.128 |
| Bands that overlap and cannot be thresholded | duplicate 0.176–0.266 vs replaced 0.236–0.332 |
