"""A realistic decision corpus for re-measuring Cairn's thresholds.

Three memories is not enough to set a threshold on. This is ~50 decisions of the
shape a real team produces: overlapping topics, several genuine near-duplicates
(the same decision captured twice in different words), and supersession chains
(a policy revised, then revised again).
"""

# (id, text) — grouped so the expected relationships are explicit below.
DECISIONS = [
    # --- review turnaround: a supersession chain ---------------------------
    ("rev1", "The team agreed to cap PR review turnaround at 24 hours, decided after the Q3 retro."),
    ("rev2", "PR review turnaround was cut from 24 hours to 4 hours after the incident where a blocking fix waited overnight."),
    ("rev3", "Review turnaround is now 6 hours, revised at the January planning session as a compromise."),
    # --- deploy policy: chain + a near-duplicate ---------------------------
    ("dep1", "We stopped deploying on Fridays after two weekend rollbacks; releases now ship Monday to Thursday."),
    ("dep2", "No Friday deploys — the team moved releases to Monday through Thursday following repeated weekend rollbacks."),
    ("dep3", "Deploys are allowed on Friday mornings again now that the canary rollout catches regressions before full release."),
    # --- trial length ------------------------------------------------------
    ("tri1", "The free trial is 14 days, not 30 — conversion data showed almost everyone who converts does so in the first week."),
    ("tri2", "We kept the trial at 14 days because extending it only delayed the decision without improving conversion."),
    # --- auth / signup -----------------------------------------------------
    ("aut1", "Signup is email-only for now; social login was dropped because SSO support for enterprise customers matters more."),
    ("aut2", "We are adding Google sign-in after all, since self-serve signups stalled at the password step."),
    # --- dashboards --------------------------------------------------------
    ("dsh1", "The team decided to merge the error rate dashboards into one and drop the per-service split, prioritising fewer dashboards over perfect granularity."),
    ("dsh2", "Error dashboards were consolidated into a single view — maintaining one dashboard beat having per-service detail."),
    # --- on-call -----------------------------------------------------------
    ("onc1", "On-call rotation moved from weekly to fortnightly so people get a real break between shifts."),
    ("onc2", "The on-call playbook lives in Notion under SRE, not in the repo, so non-engineers can read it."),
    # --- billing / db ------------------------------------------------------
    ("bil1", "The billing service runs on PostgreSQL; MongoDB was dropped because its transactional support proved unreliable."),
    ("bil2", "Invoices are generated nightly at 02:00 UTC rather than on demand, to keep the payment provider's rate limits comfortable."),
    # --- hiring / process --------------------------------------------------
    ("hir1", "Take-home exercises are capped at three hours and candidates are paid for them."),
    ("hir2", "We dropped the whiteboard round entirely — it filtered for interview practice, not for the job."),
    # --- support -----------------------------------------------------------
    ("sup1", "Support tickets are triaged twice daily at 10:00 and 15:00 rather than continuously, to protect focus time."),
    ("sup2", "Customers on the free plan get community support only; paid plans get a 24-hour first-response target."),
    # --- infra -------------------------------------------------------------
    ("inf1", "We standardised on Terraform for infrastructure; hand-edited console changes are now treated as incidents."),
    ("inf2", "Staging mirrors production data volume but with anonymised records, so load tests mean something."),
    ("inf3", "Log retention is 30 days for application logs and 1 year for audit logs, set by the compliance review."),
    # --- product -----------------------------------------------------------
    ("prd1", "The mobile app is going responsive-web-first; a native iOS build was deferred until retention justifies it."),
    ("prd2", "We removed the usage graph from the free dashboard because it drove support questions without driving upgrades."),
    ("prd3", "Feature flags are cleaned up within two sprints of full rollout; stale flags caused two outages."),
    # --- security ----------------------------------------------------------
    ("sec1", "API keys rotate every 90 days, enforced automatically rather than by reminder."),
    ("sec2", "Production database access requires a second approver; read-only replicas need only the requester."),
    # --- meetings ----------------------------------------------------------
    ("mtg1", "Standup moved to async in Slack; the live call is now Tuesdays only."),
    ("mtg2", "Retros are fortnightly rather than weekly — weekly ran out of material and attendance dropped."),
    # --- docs --------------------------------------------------------------
    ("doc1", "Architecture decisions are recorded as short ADRs in the repo, one file per decision, no template beyond context/decision/consequence."),
    ("doc2", "New-hire onboarding docs live in Notion, and the first task for every new hire is fixing something wrong in them."),
    # --- pricing -----------------------------------------------------------
    ("prc1", "The free tier is capped at three projects; the number was chosen to be useful for a side project and not for a team."),
    ("prc2", "Annual plans do not auto-renew — we email 30 days before expiry instead, after complaints about surprise charges."),
    # --- testing -----------------------------------------------------------
    ("tst1", "End-to-end tests run on merge to main only, not per commit; the per-commit run was too slow to be useful."),
    ("tst2", "Flaky tests are quarantined within 24 hours of being flagged rather than being retried in CI."),
]

# Pairs the system SHOULD collapse (same decision, different wording).
DUPLICATES = [("dep1", "dep2"), ("dsh1", "dsh2")]

# Pairs where the second REPLACES the first.
SUPERSESSIONS = [("rev1", "rev2"), ("rev2", "rev3"), ("dep2", "dep3"), ("aut1", "aut2")]

# Pairs that must stay INDEPENDENT despite sharing a topic area.
INDEPENDENT = [
    ("onc1", "onc2"),   # both on-call, different subjects
    ("bil1", "bil2"),   # both billing
    ("hir1", "hir2"),   # both hiring
    ("sup1", "sup2"),   # both support
    ("tri1", "prc1"),   # both pricing-adjacent
    ("sec1", "sec2"),   # both security
    ("mtg1", "mtg2"),   # both meetings
    ("prd1", "prd2"),   # both product
]
