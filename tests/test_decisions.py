"""The decision log — questions ABOUT the record, not answered FROM it.

Recall was a side effect of chatting: a handful of memories injected before a
reply. That is right for "why did we pick Postgres?" and wrong for "what did we
decide last week", which wants a list, ordered, with sources, and longer than a
prompt can afford to carry. This path reads the store directly, so it is
deterministic — it cannot invent an entry, and it works when the model quota is
spent.
"""
from __future__ import annotations

import time

import pytest

from cairn import context as reqctx
from cairn import decisions


# ---------------------------------------------------------------- time windows
@pytest.mark.parametrize("text,days", [
    ("what did we decide last week", 14),
    ("anything from the past 3 days", 3),
    ("decisions this month", 30),
    ("show me the last 2 weeks", 14),
    ("recent decisions lately", 14),
])
def test_periods_are_understood(text, days):
    since = decisions.parse_since(text)
    assert since is not None
    assert round((time.time() - since) / 86400) == days


@pytest.mark.parametrize("text", ["why did we pick Postgres", "", "decisions"])
def test_no_period_means_no_window(text):
    """Relevance has nothing to do with recency — only filter when asked."""
    assert decisions.parse_since(text) is None


# ---------------------------------------------------------------- rendering
class FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

        class Conn:
            def execute(_self, sql, params):
                self.sql, self.params = sql, params

                class R:
                    def fetchall(_r): return self._rows
                return R()
        self._conn = Conn()


def _row(content, author="U1", channel="C1", ts=None):
    return (content, author, channel, int(ts or time.time()), None)


def test_each_entry_carries_its_source(monkeypatch):
    monkeypatch.setattr(decisions, "_store",
                        lambda: FakeStore([_row("chose Postgres for billing")]))
    out = decisions.log_decisions("")
    assert "chose Postgres for billing" in out
    assert "<@U1>" in out and "<#C1>" in out      # the citation is the product


def test_empty_store_says_so(monkeypatch):
    monkeypatch.setattr(decisions, "_store", lambda: FakeStore([]))
    assert "no decisions recorded" in decisions.log_decisions("")


def test_empty_period_mentions_the_period(monkeypatch):
    monkeypatch.setattr(decisions, "_store", lambda: FakeStore([]))
    assert "in that period" in decisions.log_decisions("what did we decide last week")


def test_superseded_entries_are_never_listed(monkeypatch):
    """A retired decision must not reappear in the log with a citation."""
    store = FakeStore([])
    monkeypatch.setattr(decisions, "_store", lambda: store)
    decisions.log_decisions("")
    assert "superseded_by IS NULL" in store.sql


def test_a_period_becomes_a_query_filter(monkeypatch):
    store = FakeStore([])
    monkeypatch.setattr(decisions, "_store", lambda: store)
    decisions.log_decisions("last week")
    assert "created_at >= ?" in store.sql


def test_scope_is_applied(monkeypatch):
    """The log is queried outside an agent turn; if it ignored scope, a slash
    command would be a way around channel isolation."""
    store = FakeStore([])
    monkeypatch.setattr(decisions, "_store", lambda: store)
    decisions.log_decisions("", scope=("channel", "C9"))
    assert "channel = ?" in store.sql and "C9" in store.params


def test_limit_is_bounded(monkeypatch):
    store = FakeStore([])
    monkeypatch.setattr(decisions, "_store", lambda: store)
    decisions.log_decisions("", limit=9999)
    assert store.params[-1] == decisions._MAX_LIMIT
    decisions.log_decisions("", limit=0)
    assert store.params[-1] == 10


def test_a_broken_store_degrades_to_a_message(monkeypatch):
    class Broken:
        class _conn:
            @staticmethod
            def execute(*a): raise RuntimeError("no such table")
    monkeypatch.setattr(decisions, "_store", lambda: Broken())
    assert "no decisions recorded" in decisions.log_decisions("")


# ------------------------------------------------------------ scope policy
def test_scope_policy_resolves_without_a_request(monkeypatch):
    monkeypatch.delenv("CAIRN_MEMORY_SCOPE", raising=False)
    assert reqctx.scope_policy() == "channel"
    assert reqctx.scope_for("C1", "T1") == ("channel", "C1")
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE", "team")
    assert reqctx.scope_for("C1", "T1") == ("team", "T1")
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE", "global")
    assert reqctx.scope_for("C1", "T1") is None


def test_missing_provenance_means_no_filter(monkeypatch):
    """Better to answer broadly than to silently return nothing."""
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE", "channel")
    assert reqctx.scope_for(None, "T1") is None


def test_per_app_override(monkeypatch):
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE", "channel")
    monkeypatch.setenv("CAIRN_MEMORY_SCOPE_CAIRN", "team")
    assert reqctx.scope_policy("cairn") == "team"
