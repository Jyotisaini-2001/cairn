"""Jira, reframed to serve the direction.

Two changes. First, dedup by *meaning*: three people report one bug in three
wordings ("checkout throwing 500", "500 error on the checkout page", "payment
step fails with a server error") and Jira's `~` operator matches none of them to
each other — gap A from gaps.txt, and the same problem the memory store already
solved. Second, a ticket now carries the decision it relates to, with its source,
so whoever picks it up need not re-derive context settled in Slack weeks ago.
"""
from __future__ import annotations

import pytest

from cairn import jira_tool as jt


class StubEmbedder:
    """Deterministic vectors keyed by a word in the text."""

    _V = {
        "checkout": [1.0, 0.0, 0.0],
        "payment": [0.97, 0.24, 0.0],     # ~0.03 away — the same bug, reworded
        "dashboard": [0.0, 1.0, 0.0],     # unrelated
    }

    def embed(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text):
        for k, v in self._V.items():
            if k in text.lower():
                return v
        return [0.0, 0.0, 1.0]


def _issues(*pairs):
    return [{"key": k, "fields": {"summary": s}} for k, s in pairs]


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr("cairn.embeddings.get_embedder", lambda: StubEmbedder())
    monkeypatch.delenv("CAIRN_JIRA_DEDUP", raising=False)
    monkeypatch.delenv("CAIRN_JIRA_DEDUP_DISTANCE", raising=False)


# ------------------------------------------------------------------- dedup
def test_the_same_bug_in_other_words_is_caught(monkeypatch):
    monkeypatch.setattr(jt, "_open_issues",
                        lambda limit=50: _issues(("KAN-9", "checkout page returns 500")))
    hit = jt._similar_issue("payment step fails with a server error")
    assert hit and hit[0] == "KAN-9"


def test_an_unrelated_bug_files_normally(monkeypatch):
    monkeypatch.setattr(jt, "_open_issues",
                        lambda limit=50: _issues(("KAN-9", "checkout page returns 500")))
    assert jt._similar_issue("dashboard graphs render blank") is None


def test_the_closest_of_several_wins(monkeypatch):
    monkeypatch.setattr(jt, "_open_issues", lambda limit=50: _issues(
        ("KAN-1", "dashboard graphs render blank"),
        ("KAN-9", "checkout page returns 500")))
    assert jt._similar_issue("payment step fails with a server error")[0] == "KAN-9"


def test_no_open_issues_means_no_duplicate(monkeypatch):
    monkeypatch.setattr(jt, "_open_issues", lambda limit=50: [])
    assert jt._similar_issue("anything at all") is None


def test_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CAIRN_JIRA_DEDUP", "off")
    called = []
    monkeypatch.setattr(jt, "_open_issues", lambda limit=50: called.append(1) or [])
    assert jt._similar_issue("checkout page returns 500") is None
    assert called == []              # not even a Jira search


def test_a_dedup_failure_never_blocks_filing(monkeypatch):
    """Filing a duplicate is annoying; failing to file the bug is worse."""
    def boom(limit=50):
        raise RuntimeError("jira down")
    monkeypatch.setattr(jt, "_open_issues", boom)
    with pytest.raises(RuntimeError):
        boom()                        # the stub really does raise
    monkeypatch.setattr(jt, "_open_issues", lambda limit=50: _issues(("K-1", "x")))
    monkeypatch.setattr("cairn.embeddings.get_embedder",
                        lambda: (_ for _ in ()).throw(RuntimeError("no embedder")))
    assert jt._similar_issue("checkout page returns 500") is None


def test_threshold_is_tunable(monkeypatch):
    monkeypatch.setenv("CAIRN_JIRA_DEDUP_DISTANCE", "0.001")
    monkeypatch.setattr(jt, "_open_issues",
                        lambda limit=50: _issues(("KAN-9", "checkout page returns 500")))
    assert jt._similar_issue("payment step fails with a server error") is None


# ---------------------------------------------------------------- citation
class _Entry:
    def __init__(self, content, metadata):
        self.content, self.metadata = content, metadata


def _store_returning(content, monkeypatch):
    class Store:
        async def search(self, _q, options=None):
            return [_Entry(content, {"author": "U1", "channel": "C1", "date": "2026-03-12"})]
    monkeypatch.setattr("cairn.agent._memory_store", lambda: Store())


def test_a_ticket_carries_a_relevant_decision(monkeypatch):
    _store_returning("billing runs on Postgres; Mongo was dropped", monkeypatch)
    monkeypatch.setattr("cairn.embeddings.get_embedder",
                        lambda: type("E", (), {"embed": lambda _s, t: [[1.0, 0.0], [1.0, 0.0]]})())
    out = jt._related_decision("checkout billing error")
    assert "billing runs on Postgres" in out
    assert "<@U1>" in out and "<#C1>" in out


def test_an_unrelated_decision_is_not_attached(monkeypatch):
    """Regression: a live run put a dashboards decision on a CSV-export bug.
    With a small store *something* always comes back from a search, and an
    unrelated decision reads as context while being none — worse than nothing."""
    _store_returning("we merged the error-rate dashboards", monkeypatch)
    monkeypatch.setattr("cairn.embeddings.get_embedder",
                        lambda: type("E", (), {"embed": lambda _s, t: [[1.0, 0.0], [0.0, 1.0]]})())
    assert jt._related_decision("CSV export downloads an empty file") == ""


def test_the_relevance_bar_is_tunable(monkeypatch):
    _store_returning("something loosely related", monkeypatch)
    monkeypatch.setattr("cairn.embeddings.get_embedder",
                        lambda: type("E", (), {"embed": lambda _s, t: [[1.0, 0.0], [0.9, 0.44]]})())
    monkeypatch.setenv("CAIRN_JIRA_CITE_DISTANCE", "0.5")
    assert jt._related_decision("a ticket") != ""
    monkeypatch.setenv("CAIRN_JIRA_CITE_DISTANCE", "0.01")
    assert jt._related_decision("a ticket") == ""


def test_no_relevant_decision_adds_nothing(monkeypatch):
    class Store:
        async def search(self, _q, options=None):
            return []
    monkeypatch.setattr("cairn.agent._memory_store", lambda: Store())
    assert jt._related_decision("anything") == ""


def test_citation_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CAIRN_JIRA_CITE", "off")
    assert jt._related_decision("anything") == ""


def test_a_broken_store_never_blocks_filing(monkeypatch):
    monkeypatch.setattr("cairn.agent._memory_store",
                        lambda: (_ for _ in ()).throw(RuntimeError("no store")))
    assert jt._related_decision("anything") == ""


# ------------------------------------------------ one entry point, two callers
def test_both_creation_paths_use_the_same_dedup(monkeypatch):
    """Regression: semantic dedup was wired into the agent's jira_stage_ticket
    while slack_app's deterministic bug shortcut still called the keyword check,
    so a re-worded duplicate was staged anyway in the path people actually hit.
    """
    import inspect

    from cairn import slack_app

    src = inspect.getsource(slack_app)
    assert "find_duplicate(ticket[" in src, "the Slack bug shortcut must use find_duplicate"
    assert "_dedup_check(" not in src, "slack_app must not carry its own dedup check"


def test_find_duplicate_prefers_meaning_over_keywords(monkeypatch):
    monkeypatch.setattr(jt, "_similar_issue", lambda s: ("KAN-9", "checkout 500"))
    monkeypatch.setattr(jt, "_dedup_check", lambda s: "KAN-1")
    assert jt.find_duplicate("payment step fails") == "KAN-9"


def test_find_duplicate_falls_back_to_keywords(monkeypatch):
    """The fallback still earns its place when embeddings are unavailable."""
    monkeypatch.setattr(jt, "_similar_issue", lambda s: None)
    monkeypatch.setattr(jt, "_dedup_check", lambda s: "KAN-1")
    assert jt.find_duplicate("payment step fails") == "KAN-1"


def test_find_duplicate_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(jt, "_similar_issue", lambda s: None)
    monkeypatch.setattr(jt, "_dedup_check", lambda s: None)
    assert jt.find_duplicate("something entirely new") is None
