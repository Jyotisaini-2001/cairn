"""Backfill: learning from history that predates Cairn.

Passive capture only sees what happens after it is switched on, so a fresh
install knows nothing. These cover grouping history into conversations, the free
gate that decides what is worth paying a model call for, and the state that makes
a second run cheap instead of a repeat bill.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from cairn import backfill


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("DATABASE_PATH", os.path.join(d, "t.db"))
        yield


class FakeClient:
    def __init__(self, history, replies=None):
        self.history, self.replies = history, replies or {}
        self.reply_calls = 0

    def conversations_history(self, **kw):
        return {"messages": list(reversed(self.history))}   # Slack: newest first

    def conversations_replies(self, channel, ts, **kw):
        self.reply_calls += 1
        return {"messages": self.replies.get(ts, [])}


def _m(ts, text, user="U1", **extra):
    return {"ts": str(ts), "user": user, "text": text, **extra}


# ------------------------------------------------------------------- grouping
def test_a_gap_of_silence_splits_conversations():
    """Slack has no "discussion ended" marker; time is the available proxy."""
    msgs = [_m(100, "morning chat"), _m(200, "still chatting"),
            _m(100_000, "totally different topic")]
    convs = list(backfill.iter_conversations(FakeClient(msgs), "C1"))
    assert len(convs) == 2


def test_a_thread_is_one_conversation_keyed_by_its_parent():
    parent = _m(500, "should we merge the dashboards?", reply_count=2)
    replies = {"500": [parent, _m(501, "fewer is better"), _m(502, "let's merge them")]}
    convs = list(backfill.iter_conversations(FakeClient([parent], replies), "C1"))
    assert len(convs) == 1
    key, turns = convs[0]
    assert key == "500" and len(turns) == 3


def test_thread_replies_are_not_also_treated_as_top_level():
    parent = _m(500, "parent", reply_count=1)
    reply = _m(501, "reply", thread_ts="500")
    replies = {"500": [parent, reply]}
    convs = list(backfill.iter_conversations(FakeClient([parent, reply], replies), "C1"))
    assert len(convs) == 1          # not two


# ----------------------------------------------------------------- the gate
def test_only_conversations_containing_a_decision_are_candidates():
    assert backfill.looks_worth_distilling([{"text": "let's merge them"}])
    assert not backfill.looks_worth_distilling(
        [{"text": "the dashboard is unreadable"}, {"text": "what do you think?"}])


def test_dry_run_costs_nothing(monkeypatch):
    """The scan is free; distillation is not. --dry-run must not call the model."""
    called = []
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda *a, **k: called.append(1))
    msgs = [_m(100, "let's merge the dashboards")]
    stats = backfill.backfill_channel(FakeClient(msgs), "C1", dry_run=True)
    assert stats["candidates"] == 1 and stats["captured"] == 0
    assert called == []


# ------------------------------------------------------------------- running
def test_captures_a_decision(monkeypatch):
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda *a, **k: "a distilled memory")
    msgs = [_m(100, "dashboards are unreadable"), _m(150, "let's merge them")]
    stats = backfill.backfill_channel(FakeClient(msgs), "C1")
    assert stats["captured"] == 1


def test_second_run_skips_what_the_first_did(monkeypatch):
    """Re-running must continue, not re-distil and re-pay for the same threads."""
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda *a, **k: "m")
    msgs = [_m(100, "let's merge them")]
    c = FakeClient(msgs)
    assert backfill.backfill_channel(c, "C1")["captured"] == 1
    second = backfill.backfill_channel(c, "C1")
    assert second["captured"] == 0 and second["skipped_seen"] == 1


def test_limit_caps_model_calls(monkeypatch):
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda *a, **k: "m")
    msgs = [_m(i * 100_000, "let's merge them") for i in range(1, 6)]
    stats = backfill.backfill_channel(FakeClient(msgs), "C1", limit=2)
    assert stats["captured"] == 2       # free tiers are tens of calls per day


def test_a_rejected_conversation_is_still_marked(monkeypatch):
    """A "not memorable" verdict cost a model call too — do not pay twice."""
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda *a, **k: None)
    msgs = [_m(100, "let's merge them")]
    c = FakeClient(msgs)
    backfill.backfill_channel(c, "C1")
    assert backfill.backfill_channel(c, "C1")["skipped_seen"] == 1


def test_api_failure_yields_nothing(monkeypatch):
    class Broken:
        def conversations_history(self, **kw):
            raise RuntimeError("not_in_channel")
    stats = backfill.backfill_channel(Broken(), "C1")
    assert stats == {"scanned": 0, "candidates": 0, "captured": 0, "skipped_seen": 0}


# ------------------------------------------------- not learning from ourselves
def test_cairns_own_messages_never_count_as_decisions():
    """The one failure a provenance product cannot have.

    Cairn's replies are full of decision language — "We decided to use Postgres
    because…", "Saved to memory: …", "Per @alice, the team agreed to…" — because
    it reports decisions back. Distilling those feeds its own output into its
    memory, to be cited later as evidence: a citation whose source is the
    citation. A live dry-run showed 14 candidates, 7 of which were Cairn's own
    essays.
    """
    only_cairn = [
        {"speaker": "you", "text": "We decided to use Postgres for billing because…"},
        {"speaker": "you", "text": ":brain: Saved to memory: we decided to use Postgres"},
        {"speaker": "<@U1>", "text": "thanks!"},
    ]
    assert not backfill.looks_worth_distilling(only_cairn)


def test_a_human_decision_still_counts_when_cairn_replied():
    turns = [{"speaker": "<@U1>", "text": "let's merge the dashboards"},
             {"speaker": "you", "text": "Saved to memory: the team decided to merge…"}]
    assert backfill.looks_worth_distilling(turns)


def test_attribution_goes_to_the_human_not_the_bot(monkeypatch):
    """A live run stamped author=<Cairn's own user id> because it took the last
    turn, which is Cairn's reply."""
    seen = {}
    monkeypatch.setattr(backfill.memory_capture, "capture_conversation",
                        lambda turns, **kw: seen.update(kw) or "m")
    parent = _m(500, "let's merge the dashboards", user="UHUMAN", reply_count=1)
    replies = {"500": [parent,
                       {"ts": "501", "user": "UBOT", "text": "Saved to memory: …",
                        "bot_id": "B1"}]}
    backfill.backfill_channel(FakeClient([parent], replies), "C1", bot_user_id="UBOT")
    assert seen["author"] == "UHUMAN"
