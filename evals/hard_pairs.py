"""Hard labelled pairs for the duplicate / replaces / independent judgement.

threshold_corpus.py was written with giveaway words — "cut from 24 hours to 4
hours", "allowed again now", "after all" — so a classifier could pass it by
spotting cue words. These avoid them. None says "update", "now", "changed",
"again", "instead" or "from X to Y"; the relation has to be read from meaning.

Kinds of trap:
  * same wording, different subject    (must stay independent)
  * an exception to a rule             (must NOT retire the rule)
  * a one-word reversal / value swap   (replaces)
  * a paraphrase with different form   (duplicate)

`expected` is a set: where a second answer is harmless it is allowed. E.g. a
fuller restatement may be judged `duplicate` (the richer wording is kept) or
`replaces` (the old row retires, the new one says everything it said) — either
loses nothing. Where retiring would lose a live decision, only `independent`
is accepted.

(old, new, expected, what it tests) — `new` is always the later message.
"""

HARD_PAIRS = [
    # --- replaces, no cue words ------------------------------------------
    ("Daily standup is at 10:00.",
     "Daily standup is at 11:15.",
     {"replaces"}, "value swap, no time words"),
    ("We will support Internet Explorer 11 in the admin panel.",
     "We will not support Internet Explorer 11 in the admin panel.",
     {"replaces"}, "one-word reversal"),
    ("Error tracking is done in Sentry.",
     "Error tracking is done in Datadog.",
     {"replaces"}, "tool swap"),
    ("Pull requests need one approval before merging.",
     "Pull requests need two approvals before merging.",
     {"replaces"}, "threshold tweak"),
    ("No deploys on Fridays.",
     "Deploys are fine on any weekday, Friday included.",
     {"replaces"}, "rule lifted, reworded"),
    ("Candidates do a paid take-home exercise capped at three hours.",
     "Candidates pair with an engineer for one hour; there is no take-home exercise.",
     {"replaces"}, "process replaced, no cue word"),

    # --- duplicate, heavy paraphrase --------------------------------------
    ("Application logs are retained for 30 days.",
     "We keep app logs for a month.",
     {"duplicate"}, "paraphrase, number as words"),
    ("On-call rotates every fortnight.",
     "Each engineer holds on-call for two weeks before handing over to the next person.",
     {"duplicate"}, "paraphrase, different structure"),
    ("Infrastructure is managed with Terraform.",
     "We manage infrastructure in Terraform because console changes could not be traced.",
     {"duplicate", "replaces"}, "restatement that adds the reason"),

    # --- independent traps ------------------------------------------------
    ("API keys rotate every 90 days.",
     "Database passwords rotate every 90 days.",
     {"independent"}, "same wording, different subject"),
    ("Pull requests need two approvals before merging.",
     "Hotfix pull requests need one approval before merging.",
     {"independent"}, "exception must not retire the rule"),
    ("Code freeze starts two days before each release.",
     "Content freeze starts two days before each marketing launch.",
     {"independent"}, "near-identical sentence, different freeze"),
    ("Retrospectives happen every two weeks.",
     "Sprint planning happens every two weeks.",
     {"independent"}, "same cadence, different meeting"),
    ("The mobile team releases every two weeks.",
     "The web team releases every day.",
     {"independent"}, "same topic, different team"),
    ("Standups are asynchronous on Fridays.",
     "Daily standup is at 10:00.",
     {"independent"}, "complementary facts about one meeting"),
    ("Error tracking is done in Sentry.",
     "Performance monitoring is done in Datadog.",
     {"independent"}, "different job, similar tooling words"),
]
