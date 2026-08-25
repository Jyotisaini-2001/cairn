"""The cheap prefilter that decides whether a conversation is worth distilling.

Being too narrow is the expensive failure: a missed decision is lost silently
and forever, while a false positive costs one model call that the extraction
step then rejects. The first cut matched four verbs after "let's" and missed
"ok let's merge them into one and drop the per-service split" — a live test,
where four decision messages produced zero captures.
"""
from __future__ import annotations

import pytest

from loop.memory_capture import looks_memorable


@pytest.mark.parametrize("text", [
    # the live miss that prompted the rewrite
    "ok let's merge them into one and drop the per-service split",
    # stated decisions
    "we decided to use Postgres",
    "we're going with the managed service",
    "we are standardizing on Postgres",
    "we'll cap the review turnaround at 4 hours",
    "moving forward with the merge",
    "from now on releases ship on Thursdays",
    "the plan is to migrate in Q3",
    # imperative the room agrees to
    "let's go with Postgres",
    "let's freeze the schema until launch",
    # decision verbs on a thing
    "revert it to the old layout",
    "migrate them onto the new cluster",
    # short assent — weak alone, but capture reads the whole conversation, so
    # this is exactly the moment a proposal becomes a decision
    "agreed",
    "+1",
    "sgtm",
    "lgtm",
    "ship it",
    "sounds good",
])
def test_decisions_pass_the_gate(text):
    assert looks_memorable(text), text


@pytest.mark.parametrize("text", [
    # observations and opinions — real inputs from the same live test that
    # correctly produced nothing
    "our error rate dashboard is basically unreadable now",
    "fewer dashboards matter more than perfect granularity",
    "we could split it per-service but that's three more to maintain",
    # chit-chat: bare assent is deliberately excluded, it is too common
    "ok",
    "yes",
    "yeah",
    "thanks!",
    "good morning everyone",
    "what do you think?",
    "",
    "   ",
])
def test_noise_is_filtered(text):
    assert not looks_memorable(text), text


def test_case_and_punctuation_do_not_matter():
    assert looks_memorable("OK, LET'S GO WITH POSTGRES.")
    assert looks_memorable("Agreed!")


@pytest.mark.parametrize("text", [
    "we could split it per-service but that's three more to maintain",
    "maybe we should merge them into one",
    "what if we drop the per-service split?",
    "should we migrate it to the new cluster?",
    "thinking about switching to Postgres",
])
def test_hedged_proposals_are_not_decisions(text):
    """Slack is full of "we could X" — it trips the decision verbs while the room
    has settled nothing."""
    assert not looks_memorable(text), text


@pytest.mark.parametrize("text", [
    "we could have kept Mongo but we decided to go with Postgres",
    "we considered per-service dashboards; final call is one merged view",
])
def test_an_explicit_decision_beats_a_hedge_in_the_same_line(text):
    assert looks_memorable(text), text
