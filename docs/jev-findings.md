# Jev in Cairn — field notes

_Running log of what we actually observed testing TypeSafe AI's Jev (System One
model) inside Cairn, a Slack bot that remembers a team's decisions. Facts only;
every number here was measured. Added to after each test. Started 2026-09-27._

---

## What Cairn is, in one paragraph

Cairn reads Slack channels, notices when a team makes a decision, saves it with
its reasoning and its source (who, where, when), and answers "why did we do X?"
later. The hard part is that decisions change: "2-week sprints" becomes "3-week
sprints". If the old one keeps being served as current, the bot is confidently
wrong — with a real citation, which makes it look checked.

## The job we gave Jev

One job: when a new decision is saved and an older one looks similar, decide
how they relate.

- **duplicate** — same decision, different words → keep one
- **replaces** — same question, the answer changed → retire the old one
- **independent** — both can be true → keep both

The costly mistake is retiring a decision that was still valid: it silently
stops being answerable, and nobody knows to look for it.

## Where Jev sits

```
Slack message ─► regex: looks like a decision? ─► Gemini writes a 2-4 sentence summary
   ─► remove secrets ─► local embedding (bge-small, 384 numbers)
   ─► 4 closest live memories by distance (computed in the database, not by Jev)
        distance < 0.08       → duplicate, Jev not asked
        distance 0.08 – 0.45  → ask Jev
        distance > 0.45       → unrelated, Jev not asked
   ─► our rules on Jev's answer ─► save / merge / retire
```

Jev is never involved in reading Slack, writing summaries or answering
questions. Gemini writes; Jev judges.

## Why we tried Jev

The previous judge was a Gemini call returning JSON. On the same pair of
decisions it gave **opposite verdicts on two different runs**, even at
temperature 0. We had built workarounds (caching verdicts, defaulting to "keep
both"). A judgement that changes when you ask twice is not a judgement.

---

## What we sent Jev

**Version 1** — one question:

```
state:     { old_decision, new_decision }
question:  choice — "How does the NEW decision relate to the OLD one?"
           options: duplicate / replaces / independent (each with a one-line description)
rule:      a duplicate/replaces answer below confidence 0.7 → keep both
```

**Version 2** — same call, two extra yes/no questions (Jev answers several
questions in one request, same price, same speed):

```
Q1 choice — How does NEW relate to OLD?                       (as before)
Q2 yes/no — NEW applies only for a limited time or one event (this week,
            one launch, one campaign, one date)?
Q3 yes/no — NEW is about the same specific case as OLD (same team, system,
            situation, time period, aspect), or explicitly overrides it
            ("all", "every", "no exceptions", or by naming it)?
rule:      retire only if Q1 = replaces with confidence ≥ 0.7,
           AND Q2 < 0.5, AND Q3 ≥ 0.5. Otherwise keep both.
```

---

## Results

### Live in Slack (2026-09-27)

| Test | Jev said | Confidence | Latency | Correct |
|---|---|---|---|---|
| "2-week sprints" → "switched to 3-week sprints" | replaces | 1.00 | 482 ms | ✅ old retired |
| same save, also compared with "billing runs on PostgreSQL" (distance 0.435) | independent | 1.00 | 884 ms | ✅ left alone |
| "standup 9:30 → 10:30" vs two nearby memories | independent, independent | 1.00, 1.00 | 464, 421 ms | ✅ |
| "standup 10:30" → "standup 10:00 instead" | replaces | 1.00 | 389 ms | ✅ old retired |
| same save, second nearby memory | independent | 1.00 | 426 ms | ✅ |

- **7 of 7 live judgements correct**, all at confidence 1.00.
- **389 – 884 ms** per call, about **450 – 520 input tokens** each.
- **Zero Gemini calls** for these judgements (verified in the logs: the only
  Gemini call per save was the summary).
- After the test, `@Cairn how long are our sprints?` answered "3 weeks" with
  the reason and the source; the retired 2-week decision was not used.

### Offline test sets

Three sets of labelled pairs. Every Jev answer below comes from the real API.

| Set | Pairs | Who wrote it | Built to be |
|---|---|---|---|
| Easy | 14 | Claude, Aug | realistic, but with giveaway words ("cut from 24 to 4 hours", "again", "after all") |
| Hard | 16 | Claude | no giveaway words; traps (same wording/different subject, exceptions, one-word reversals) |
| Blind #1 | 20 (17 clear + 3 ambiguous) | Jyoti, via ChatGPT, labels hidden from Claude until after the run | exceptions, one-time changes, partial changes, paraphrases |

Score = what Cairn **actually does** after our rules, not Jev's raw answer.

| Set | Version 1 | Version 2 |
|---|---|---|
| Easy | 14/14 | 14/14 |
| Hard | 14/16 | 14/16 |
| Blind #1 (clear pairs, after relabelling — see below) | 12/16 | 13/16 |
| **Harmful mistakes** (valid rule deleted), hard + blind #1 | **3** | **1** |
| Blind #1 ambiguous pairs where both were kept | 0 of 4 | 1 of 4 |

**Consistency:** verdicts did not change across repeated runs on 49 of 50
pairs. The one that flipped (hard set, "code freeze" vs "content freeze") sat at
confidence 0.26 – 0.37, and both answers led to the same outcome (keep both).
Confidence values themselves move slightly between runs (e.g. 0.99 / 1.00,
0.47 – 0.53).

---

### Version 3 — TypeSafe's numbers, and yes/no descriptions

Built from TypeSafe's documentation, not fitted to our test sets:

| Setting | V2 | V3 | Source |
|---|---|---|---|
| act on duplicate/replaces only above | 0.7 (our guess) | **0.85** | their high-stakes routing example |
| retire only if "temporary?" below | 0.5 | **0.2** (clearly no) | their yes/no bands: yes > 0.8, no < 0.2 |
| retire only if "same case?" above | 0.5 | **0.8** (clearly yes) | same |
| yes/no questions | statement only | **+ "true means… / false means…"**, with general examples taken from no test set | their advice for subtle boundaries |
| unsure answers | — | keep both; later ask a person in Slack | their "route to a human" |

| Across all three sets | V1 | V2 | V3 |
|---|---|---|---|
| **Harmful: valid rule deleted** | 3 | 1 | **0** |
| ambiguous pairs kept safely | 0 / 4 | 1 / 4 | **4 / 4** |
| real replacements missed (both kept) | 0 | 0 | 6 |
| duplicates missed (extra row) | 4 | 4 | 5 |

**Adding true/false descriptions removed the overlap.** "Same case?" scores:

| | V2 | V3 |
|---|---|---|
| pairs that must not retire | up to 0.63 | 0.17 – 0.45 |
| real replacements | from 0.64 | 0.55 – 0.96 |

The async-Friday exception went from 0.59 (deleted) to 0.38 (kept).

**Cost:** six real replacements scored 0.55 – 0.76 on "same case?", inside
TypeSafe's unsure band, so both decisions stay live. This is the band their
docs route to a human; it is where Cairn's "does this replace…?" question to
the team will go. The 0.45 – 0.55 gap was seen on these sets and is not used
to move any line.

### Blind set #2 — the honest score (version 3, unseen pairs)

20 pairs written by Jyoti via ChatGPT after version 3 was final; never seen by
Claude, nothing tuned on them. 16 clear, 4 marked ambiguous.

| | Result |
|---|---|
| **valid rule deleted** | **0** |
| ambiguous pairs kept safely | 4 / 4 |
| real replacements retired correctly | 4 (retry count, payload limit, storage technology, indexing schedule — same case 0.88 – 0.93) |
| rescued by our rules (raw answer wrong, outcome right) | 3 |
| real replacements kept as both (unsure band) | 2 (refund threshold: same case 0.74 – 0.76; social-login provider: 0.62 – 0.63) |
| duplicates kept as both | 4 |
| verdict changed between runs | 0 / 20 |

Clear pairs: 10 / 16 final outcome right; all six misses keep both, nothing lost.

**TypeSafe's line did the work on unseen data.** Same tooling, different
teams: Jev said replaces at confidence 0.96, same case 0.65 – 0.67. Version 2's
0.5 line would have deleted it; TypeSafe's 0.8 "clearly yes" line kept it.

**A consistent Jev pattern:** the same decision with an owner or responsibility
added is read as a different decision (independent 0.60 – 0.96, same case
0.08 – 0.16). Seen 6 times across all sets. Harmless — an extra row.

### Version 3 across everything

| Set | Written by | Seen before tuning? | Final right | Valid rule deleted |
|---|---|---|---|---|
| Easy (14) | Claude | yes | 12 / 14 | 0 |
| Hard (16) | Claude | yes | 14 / 16 | 0 |
| Blind #1 (16 + 4 ambiguous) | Jyoti | yes | 10 / 16 | 0 |
| **Blind #2 (16 + 4 ambiguous)** | **Jyoti** | **no** | **10 / 16** | **0** |

**70 pairs, zero valid decisions deleted.** Every miss is the safe kind: an
extra row, or a replacement left for a person to confirm.

### Set 3 — 53 pairs, version 3 unchanged

Written by Jyoti via ChatGPT; 49 clear (23 independent, 14 replaces, 12
duplicate) + 4 ambiguous. Run after version 3 was final; nothing tuned on it.

| | Count |
|---|---|
| retirements made | **13, all correct** |
| wrong deletions prevented (raw Jev said replaces; both should stay) | **13** |
| — of those, Jev ≥ 0.85 confident (only the checks could stop it) | 7 |
| — "exception first, then general rule" at raw confidence 0.99 – 1.00 | 3, all stopped by "same case?" (0.29 – 0.37) |
| ambiguous pairs raw Jev would have acted on | 3 of 4, all stopped |
| real replacements held back | 1 (refund approval threshold, same case 0.75 — set 2's refund pair scored 0.74 – 0.76) |
| one-off / temporary pairs | temporary 0.81 – 0.97, permanent rules kept |
| duplicates merged | **0 of 12** |
| verdict changed between runs | 2 of 53 (both at 0.27 – 0.38; same outcome) |
| would ask a person | 12 of 53 (23%) |

Every one of the 13 prevented deletions involved the "same case?" check.

**Duplicates are now the main gap.** On 12 expected duplicates Jev answered
duplicate once (0.31), independent 7 times (mostly an owner or administrator
added, 0.91 – 0.98) and replaces 4 times. Across all sets, "same decision plus
added detail" has been read as a different decision 14+ times. Harmless — both
kept — but memory accumulates near-copies. Some of these labels are debatable
("adds a narrow exception" arguably is a change).

**Borderline stability:** one real replacement scored same case 0.81 on one
run and 0.77 on the other — retired on one day, held back on another. Safe in
both directions, but not fixed.

### All sets, version 3

| Set | Pairs | Retired (correct) | Wrong deletions prevented | Wrong deletions made |
|---|---|---|---|---|
| Easy | 14 | — | 0 | 0 |
| Hard | 16 | — | 2 | 0 |
| Blind #1 | 20 | — | 2 (+4 ambiguous) | 0 |
| Blind #2 | 20 | 4 (4) | 3 (+4 ambiguous) | 0 |
| Set 3 | 53 | 13 (13) | 13 (+3 ambiguous) | 0 |
| **Total** | **123** | | **20 (+11 ambiguous)** | **0** |

### What the rules are worth, and what they leave for a person

Acting on Jev's raw answer with no rules, across all 70 pairs: **7 valid
decisions deleted**, and all **8 ambiguous pairs acted on**. With the three
rules: 0 and 0. (Blind set #2 alone: 3 deleted and 4 acted, without rules.)

Cases in TypeSafe's unsure band — Jev leans "replaces" but no signal clearly
settles it, so their docs route it to a human:

| Set | Pairs | Would ask a person | real change | ambiguous | should stay separate |
|---|---|---|---|---|---|
| Easy | 14 | 2 | 2 | 0 | 0 |
| Hard | 16 | 2 | 1 | 0 | 1 |
| Blind #1 | 20 | 6 | 3 | 3 | 0 |
| Blind #2 | 20 | 9 | 3 | 4 | 2 |
| **Total** | **70** | **19 (27%)** | **9** | **7** | **3** |

These sets were built to be tricky; in live Slack, 0 of 7 real judgements fell
in the band. The real rate is unknown and should be measured before a Slack
confirmation step is built.

## Where Jev is strong

- **Changed answers, with no hint words.** Time moved (Thursday → Monday), tool
  swapped (Redis → Memcached, Sentry → Datadog), threshold changed (one approval
  → two), a single "not" (will / will not support IE11): all caught at 1.00.
- **Partial changes.** A 3-item checklist where one item changed: 3 of 3
  correct at 0.99 – 1.00.
- **Heavy paraphrase.** "Application logs are retained for 30 days" = "We keep
  app logs for a month": duplicate, 0.99.
- **Same words, different team or thing.** Customer-success vs engineering
  planning at the same time; production vs staging secrets; code freeze vs
  content freeze (low confidence, so kept both).
- **Stable.** The instability that made us switch did not appear.
- **Fast and cheap** relative to the Gemini summary call on the same save.

## Where Jev struggles

### 1. One-time changes read as permanent — FIXED in version 2

```
OLD: Invoices are generated on the first business day of each month.
NEW: For the September launch campaign, invoices will be generated on September 18.
v1:  replaces (0.71 – 0.74) → the permanent rule would be deleted
v2:  replaces, but temporary = 0.96 → keep both ✅
```

Same for "for this week's launch, deploy on Saturday" (temporary 0.95). Asking
the time question directly worked every time we tried it. It also flagged
"keep the flag on **through Friday**" as temporary (0.90), which is correct.

### 2. An exception deleted by a later general rule — fixed in version 3

```
OLD: Standups are asynchronous on Fridays.
NEW: Daily standup is at 10:00.
v1:  replaces 0.96
v2:  replaces 0.96, same-case = 0.59 – 0.63 → still deleted ❌
```

A person reads "daily standup" as the meeting's name; Jev reads "daily" as
every day, Friday included. The same-case question helped where the difference
is a different *thing* (code vs content freeze: 0.24; enterprise customers vs
everyone: 0.43 → kept both), but not where it is an *exception to a rule*
(0.59 – 0.67). Version 3 added true/false descriptions to the question and
the async-Friday score fell to 0.38 — kept. No harmful mistakes remain on the
three sets we have.

### 3. Same decision plus an owner or reason — called "independent"

"We will use PostgreSQL for the order service" vs the same sentence plus "owned
by the platform team" → independent (0.78). Harmless (an extra row, nothing
lost), but consistent across three different pairs.

### 4. Confidence is Jev's certainty, not human disagreement

On pairs where reasonable people disagree, Jev was still confident (0.74 –
1.00). Confidence tells you when *Jev* is unsure; it does not detect that a
question is genuinely open.

---

## TypeSafe's own documentation matches what we found

TypeSafe publishes the known failure modes of Jev 1.13
(docs.typesafe.ai/model-jaggedness/jev-1.13). Mapped to Cairn:

| # | Their failure mode | Their advice | In Cairn |
|---|---|---|---|
| 1 | Literal reading — "scoping words, negations, and implied conditions at face value" | write the exact condition; criteria for each option | **Hit it** ("daily" standup). Version 2 asks the conditions directly |
| 2 | Math and numbers | keep arithmetic in code | Avoided: similarity is computed in code, not by Jev |
| 3 | Date and time comparison | extract components; compare in code | **Hit it** ("this week", "September campaign"). Version 2 asks "is it temporary?" |
| 4 | Indirection | reduce hops; point to the relevant state | Version 1 asked one broad question; splitting it helped |
| 5 | Large irrelevant state | send only what the question needs | Avoided: only the two decisions are sent |
| 8 | Structural invariants | ask each decision one way; don't reuse thresholds across question types | Separate bars: 0.7 for the choice, 0.5 for yes/no |
| 9 | Generation | use a generative model | Gemini writes summaries; Jev only judges |

Their documentation predicted our two real failure types, and their advice is
what fixed one of them.

---

## Things we learned about testing this

- **Our first test set was too easy.** 14/14, but the sentences contained
  giveaway words. A classifier can pass by spotting words.
- **Labels are opinions.** Reviewing the blind set, 4 of 20 labels were
  debatable and were changed after discussion, each with a written reason:
  "all invoices… consistent across customer segments" really does cancel the
  nonprofit exception (→ replaces); a general deploy rule that never mentions
  a launch exception is genuinely unclear (→ ambiguous); two "duplicate" labels
  also allowed a second harmless answer.
- **Score the outcome, not the raw answer.** A wrong raw answer at low
  confidence is harmless if the safety rule catches it. We report both.
- **A blind set is the only honest score.** Version 2 was designed after seeing
  blind set #1, so its 13/16 is not independent. Blind set #2 (run on version 3, unseen) is.

## Related finding (not Jev): the Gemini summarizer

While testing live we found that the Gemini step deciding "is this message a
decision at all?" was unreliable:

- Given the last 20 channel messages, it rejected a real decision posted after
  two unrelated questions. Fixed by telling it which message to judge.
- On the **same input**, it answered "not a decision" live and "a decision"
  minutes later.
- One summary call took **47 seconds** (normally ~2 s), as the free-tier quota
  ran low.

This is the next candidate for Jev: a yes/no "does this message make a
decision?" with a confidence, with Gemini only writing the summary.

## Why the rules are shaped this way

The two possible mistakes are not equal:

| Mistake | Example | Noticeable? |
|---|---|---|
| Retire a decision that was still valid | "daily standup at 10:00" retires "async Fridays" | **No** — answers stay confident and cited, the rule is just gone |
| Keep a decision that was really replaced | "2-week" and "3-week sprints" both live | **Yes** — both appear, with dates |

So every rule only ever turns "retire" into "keep both", never the reverse.

| Rule | Guards against | Number | Basis |
|---|---|---|---|
| distance < 0.08 → duplicate, no Jev call | paying to compare near-identical text | 0.08 | measured (real duplicates start at 0.176) |
| distance > 0.45 → no Jev call | comparing unrelated decisions | 0.45 | measured (all real relations ≤ 0.332; unrelated ≥ 0.460) |
| Jev confidence < 0.7 → keep both | acting on Jev's guess | 0.7 | TypeSafe guidance (act ≳0.9, not <0.5); chosen, not measured |
| temporary ≥ 0.5 → keep both | a one-off deleting a permanent rule | 0.5 | midpoint; not measured |
| same case < 0.5 → keep both | a rule about X deleting a rule about Y | 0.5 | midpoint; not measured |

Observed: Jev's wrong answers were either very sure (0.96 – 1.00) or very
unsure (0.26 – 0.41). The 0.7 bar catches the second kind; no bar can catch
the first, which is why the direct questions were added.

## How Jev's confidence works (checked against real answers)

Jev returns a probability for every option. Confidence measures how
concentrated they are; for three options TypeSafe gives `(3 × top − 1) / 2`,
and it matched the API on every pair we checked:

| Pair | Probabilities | Confidence |
|---|---|---|
| Code freeze vs content freeze | replaces 0.53 · independent 0.45 · duplicate 0.02 | 0.29 |
| Monthly invoices vs "for the September campaign, Sept 18" | replaces 0.83 · independent 0.17 | 0.74 |
| Async Fridays vs "daily standup is at 10:00" | replaces 0.98 · independent 0.02 | 0.96 |

"replaces, confidence 0.29" is not a contradiction: it is a 53/45 lean.
Yes/no questions carry no confidence; the single number is P(yes).

## What TypeSafe says about thresholds — and how ours compare

- Confidence page: *"If confidence < 0.5 … Don't guess."*; *"> 0.9: high
  stakes, high confidence."*; *"The correct threshold values depend on your
  domain … Start with conservative thresholds, test with your own data."*
- Confidence-routing pattern: below 0.6 route to a human; high-stakes actions
  only above 0.85, confirm between 0.6 and 0.85.
- Yes/no (Noul): *"Use 0.5 when yes and no are equally easy to act on. Raise it
  when acting on a false yes is expensive … Lower it when missing a true yes is
  expensive."* Example: yes above 0.8, no below 0.2, human review between.

Retiring a decision is high-stakes, so by their example our 0.7 relation bar is
less careful than they would suggest (0.85). Our two yes/no checks both used
0.5, but neither is symmetric:

| Check | Expensive mistake | TypeSafe says | Our data |
|---|---|---|---|
| "NEW is temporary?" | missing a real one-off | lower the bar | permanent replacements 0.04 – 0.21; one-offs 0.90 – 0.96 — separates cleanly; ~0.3 is safe |
| "NEW is about the same case?" | a false "same case" deletes an exception | raise the bar | real replacements 0.64 – 0.94; async-Friday exception 0.59 – 0.63 — **overlaps**; no bar separates them |

The same-case scores overlap the way embedding distances overlap for "same
decision reworded" vs "decision changed": when two groups overlap, no threshold
fixes it. TypeSafe's suggestion for subtle boundaries — explicit true/false
criteria on the yes/no question — is untested here yet.

## Open items

- Gemini vs Jev on the same sets (Gemini needs a fresh daily quota).
- Temporary decisions have no expiry in Cairn today; every memory is permanent.
- The 0.5 bar on the yes/no checks is a starting value, not yet measured.
- Done in version 3: TypeSafe's bars (0.85, 0.2 / 0.8) and true/false criteria.
- Product question: route unclear cases to a person in Slack ("does this
  replace the earlier decision?") — TypeSafe's routing pattern sends
  low-confidence answers to a human.
