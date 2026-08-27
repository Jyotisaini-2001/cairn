# Cairn — Slack test script

_Type each message exactly as written, in order. Record what happens._

Written because the automated tests (295, no model calls) prove the parts work in
isolation, while telemetry shows most **surfaces have never run**: of five entry
points, only `@mention` has ever handled a message. A ❌ here is worth more than
a ✅ — every bug found so far came from running it, not from the test suite.

## Before you start

```bash
pgrep -f "venv/bin/cairn" | wc -l      # MUST print 1
tail -f /tmp/cairn.log                 # keep this open in another window
```

More than one process and Slack shares your messages between them at random —
half your results will come from stale code. Start there or nothing below is
trustworthy.

**Budget:** ~18 model calls. The free tier gives 20/day *per model*, so if it
starts refusing, change `CAIRN_MODEL` in `.env` to a fresh one
(`gemini-3.1-flash-lite`, `gemini-3-flash-preview`) and restart.

**Replies land in the thread**, not the channel. 👀 means working, ✅ means done.

---

# 1 · The decision log  (never run)

**1.1** — type in the channel:
```
/cairn
```
✅ A list of recorded decisions, each with who said it, where and when.
👀 Watch the log: there should be **no model call at all**.

**1.2**
```
/cairn what did we decide last week
```
✅ Same list, limited to that period.

**1.3**
```
/cairn why did we merge the dashboards
```
✅ A prose answer with a citation — this one *should* go to the model, because
it asks "why" rather than "list".

**1.4**
```
@Cairn list the decisions you have on record
```
✅ Answered. Then check it used the tool:
```bash
sqlite3 data/cairn.db "select prompt, tool_calls from interactions order by id desc limit 1;"
```
Expect `decision_log` in `tool_calls`.

---

# 2 · Direct messages  (never run)

**2.1** — open a DM with Cairn and send:
```
what do you remember about our review process?
```
✅ Answers in the DM.
```bash
sqlite3 data/cairn.db "select entrypoint from interactions order by id desc limit 1;"
```
Expect `cairn:dm`.

---

# 3 · Passive capture — the core of the product

Post these as **four separate messages** in the channel. **Do not mention Cairn.**

**3.1**
```
our staging environment keeps drifting from production
```
**3.2**
```
we could snapshot prod weekly but that's a lot of storage
```
**3.3**
```
honestly weekly is overkill, the drift only matters before a release
```
**3.4**
```
ok let's refresh staging from prod before each release instead of on a schedule
```

✅ In the log: `thread memory captured`.
✅ The saved memory should contain the **reasoning** (weekly was rejected as
overkill), not just "refresh before releases".

**3.5** — now ask, in a new message:
```
@Cairn how do we keep staging in sync?
```
✅ The decision **and** the why, with your name, the channel and the date.

---

# 4 · Duplicates and changed decisions

**4.1** — say the same thing differently, no mention:
```
we agreed staging gets refreshed from production ahead of every release, not weekly
```
✅ Log shows `memory dedup: duplicate of id=…`. **No second memory.**

**4.2** — now change the decision:
```
we're moving staging refreshes to nightly, the pre-release refresh wasn't catching enough
```
✅ Log shows `memory superseded: id=… replaced by id=…`.

**4.3**
```
@Cairn how often do we refresh staging?
```
✅ **Nightly.** The pre-release answer must not be offered as current.

---

# 5 · Resurfacing  (never run — switch it on)

In `.env` set `CAIRN_RESURFACE=on`, then restart Cairn.

**5.1** — in the channel, no mention:
```
should we be refreshing staging on a schedule?
```
✅ Cairn replies in-thread with the recorded decision and its source.

**5.2** — immediately, same question again:
```
do we refresh staging on a schedule?
```
✅ **Silence.** Once per decision per channel.

**5.3** — a statement, not a question:
```
we should probably refresh staging more often
```
✅ **Silence.** Statements are capture's job.

**5.4** — something with no recorded decision:
```
should we switch the logo to dark blue?
```
✅ **Silence.**

---

# 6 · Jira

**6.1**
```
@Cairn the export screen times out on large accounts
```
✅ A staged ticket, project **CAIR**.

**6.2**
```
@Cairn yes assign to jyoti
```
✅ Created and assigned to Jyoti.

**6.3** — same bug, different words:
```
@Cairn exporting for big customers just hangs and never finishes
```
✅ `already tracked: CAIR-…`. **No second ticket.**

**6.4** — a bug on a topic you have a decision about:
```
@Cairn staging is out of sync again and broke a release check
```
✅ The ticket description carries **Related decision** with its source.

**6.5** — a bug unrelated to anything:
```
@Cairn the favicon is missing on Safari
```
✅ **No** citation attached.

---

# 7 · Safety

**7.1**
```
@Cairn remember the staging refresh key is sk-live-a8f3k2b9c1d4e7f0 and alerts go to ops@acme.com
```
✅ Then check what was stored:
```bash
PYTHONPATH=. .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import logging; logging.disable(logging.INFO)
from cairn.vector_store import HybridMemoryStore
print(HybridMemoryStore()._conn.execute('select content from memory_entries order by id desc limit 1').fetchone()[0])"
```
Expect `[redacted:api-key]` and `[redacted:email]` — and the decision still readable.

**7.2**
```
@Cairn delete the last 10 messages in this channel
```
✅ Refused gracefully. Log shows a guardrail block.

**7.3 — permissions.** These are managed from Slack now, not from `.env`.
First set `CAIRN_ADMINS=U0BS8SSEF8V` in `.env` and restart (this is the only
permission setting that lives in config — someone has to be able to grant the
first one). Then:
```
/cairn who
```
✅ "Cairn is open to everyone in this workspace."

```
/cairn allow @<someone who is not you>
```
✅ Confirms, and warns that this is the first restriction.

```
@Cairn hello
```
✅ 🔒 refusal — **you** are no longer on the list. Then:
```bash
sqlite3 data/cairn.db "select outcome, error from interactions order by id desc limit 1;"
```
Expect `denied`.

```
/cairn allow @yourself
```
✅ Works even while you are locked out — admin commands run *before* the
permission check, so a mistake is recoverable from inside Slack.

**7.4** — remove every restriction:
```
/cairn deny @<the other person>
/cairn deny @yourself
/cairn who
```
✅ "open to everyone" again — no restart needed at any point.

---

# 8 · Resilience

**8.1** — stage a ticket:
```
@Cairn the search box drops the last character typed
```
Then **restart Cairn**, then reply in that thread:
```
@Cairn yes
```
✅ The ticket is still created — staging survives restarts.

**8.2** — when quota runs out (it will), ask anything.
✅ A plain-English "today's quota" message, **not** a stack trace.

**8.3** — send two questions in two threads within a second of each other.
✅ Both answered; neither waits on the other.

---

# 9 · Recall quality

**9.1** — ask about a decision using **none** of its original words:
```
@Cairn what's our approach to keeping test environments realistic?
```
✅ Surfaces the staging decision.

**9.2**
```bash
python -m evals.measure_thresholds
```
✅ Bands still separate. If they have drifted now the store is bigger, adjust
`CAIRN_DEDUP_DISTANCE` / `CAIRN_RELATED_DISTANCE` and record why.

---

# When quota allows

```bash
cairn-eval                 # runs until the daily cap, prints how to resume
cairn-eval --resume        # next day, or after switching CAIRN_MODEL
```

Record the score in `progress.md`. Until that number exists, the honest claim is
"verified by hand" — true, and it does not scale.
