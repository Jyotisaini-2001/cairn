"""Proactivity, earned.

The direction deferred this deliberately: a bot that interrupts you wrongly gets
muted, and there is no recovery. So the bar is high — a question, a strong match
against a live decision, once per decision per channel, and off by default.
These pin each of those, because every one of them is what keeps the feature
from becoming the reason someone mutes Cairn.
"""
from __future__ import annotations

import pytest

from cairn import resurface


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CAIRN_RESURFACE", "on")
    resurface._seen.clear()
    yield
    resurface._seen.clear()


class FakeStore:
    """Hybrid-store stand-in returning one neighbour at a chosen distance."""

    def __init__(self, distance=0.1, row=True):
        self.distance, self.row, self.params, self.sql = distance, row, None, ""

        class Embedder:
            def embed_one(_s, _t): return [0.1, 0.2, 0.3]
        self.embedder = Embedder()

        class Conn:
            def execute(_s, sql, params):
                self.sql, self.params = sql, params

                class R:
                    def fetchone(_r):
                        if not self.row:
                            return None
                        return (7, "we chose Postgres for billing", "U1", "C1",
                                1787600000, self.distance)
                return R()
        self._conn = Conn()


def _check(monkeypatch, store, text="should we use Postgres or Mongo?", **kw):
    monkeypatch.setattr("cairn.agent._memory_store", lambda: store)
    return resurface.check(text, channel="C1", **kw)


# ------------------------------------------------------------------- the gate
@pytest.mark.parametrize("text", [
    "should we use Postgres or Mongo?",
    "what is our review turnaround?",
    "remind me what we decided about the database",
    "maybe we should merge the dashboards",
])
def test_reopened_questions_qualify(text):
    assert resurface.looks_like_a_question(text)


@pytest.mark.parametrize("text", [
    "the build is broken",
    "shipping the fix now",
    "good morning",
    "",
])
def test_statements_do_not(text):
    """A decision being *made* is capture's job. Answering it with "you already
    decided this" would be both wrong and irritating."""
    assert not resurface.looks_like_a_question(text)


# ------------------------------------------------------------------ matching
def test_a_close_match_is_surfaced(monkeypatch):
    note = _check(monkeypatch, FakeStore(distance=0.1))
    assert note and "Postgres" in note


def test_the_note_carries_a_source(monkeypatch):
    note = _check(monkeypatch, FakeStore(distance=0.1))
    assert "<@U1>" in note and "<#C1>" in note


def test_the_wording_offers_rather_than_polices(monkeypatch):
    """The team may be revisiting a decision on purpose."""
    note = _check(monkeypatch, FakeStore(distance=0.1))
    assert "came up before" in note.lower()
    assert "already decided" not in note.lower()


def test_a_loose_match_stays_quiet(monkeypatch):
    """No model judges this — the message itself is the output — so the bar is
    tighter than recall's."""
    assert _check(monkeypatch, FakeStore(distance=0.9)) is None


def test_nothing_in_the_store_stays_quiet(monkeypatch):
    assert _check(monkeypatch, FakeStore(row=False)) is None


def test_a_statement_never_triggers_a_lookup(monkeypatch):
    store = FakeStore(distance=0.0)
    assert _check(monkeypatch, store, text="the build is broken") is None
    assert store.params is None          # not even a query


# ------------------------------------------------------------------ restraint
def test_the_same_decision_is_raised_only_once(monkeypatch):
    store = FakeStore(distance=0.1)
    assert _check(monkeypatch, store) is not None
    assert _check(monkeypatch, store) is None


def test_a_different_channel_may_still_hear_it(monkeypatch):
    store = FakeStore(distance=0.1)
    monkeypatch.setattr("cairn.agent._memory_store", lambda: store)
    assert resurface.check("should we use Postgres?", channel="C1") is not None
    assert resurface.check("should we use Postgres?", channel="C2") is not None


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("CAIRN_RESURFACE", raising=False)
    assert _check(monkeypatch, FakeStore(distance=0.0)) is None


def test_superseded_decisions_are_never_resurfaced(monkeypatch):
    store = FakeStore(distance=0.1)
    _check(monkeypatch, store)
    assert "superseded_by IS NULL" in store.sql


def test_scope_is_respected(monkeypatch):
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE", "channel")
    store = FakeStore(distance=0.1)
    _check(monkeypatch, store)
    assert "channel = ?" in store.sql


def test_a_failure_is_swallowed(monkeypatch):
    class Broken:
        pass
    monkeypatch.setattr("cairn.agent._memory_store", lambda: Broken())
    assert resurface.check("should we use Postgres?", channel="C1") is None
