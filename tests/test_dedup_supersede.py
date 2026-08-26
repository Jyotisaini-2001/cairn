"""Near-duplicate collapsing and decision supersession.

Two problems, one mechanism. After a single evening of testing the store held 7
memories carrying 3 facts: the Postgres decision appeared four times in four
wordings (saved live, then re-read by backfill), and "cap review turnaround at 24
hours" sat next to "moved it to 4 hours" as equal, equally-cited truths.

Exact-content dedup catches neither, because the wording differs every time.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

libsql = pytest.importorskip("libsql_experimental")

from cairn import vector_store as vs  # noqa: E402


class StubEmbedder:
    """Deterministic 3-d embeddings so distances are predictable in tests."""

    name = "stub"
    dim = 3
    _VECTORS = {
        # the same fact, three wordings — near-identical vectors
        "postgres_a": [1.0, 0.0, 0.0],
        "postgres_b": [0.99, 0.01, 0.0],
        "postgres_c": [0.995, 0.0, 0.01],
        # same subject, changed answer. Deliberately ~0.2 cosine distance:
        # far enough to clear the dedup band (<=0.12, "same fact re-phrased"),
        # close enough to stay inside the supersession band (<=0.45).
        "review_24h": [0.0, 1.0, 0.0],
        "review_4h": [0.6, 0.8, 0.0],
        # unrelated
        "dashboards": [0.0, 0.0, 1.0],
    }

    def embed_one(self, text: str):
        for key, vec in self._VECTORS.items():
            if text.startswith(key):
                return vec
        return [0.3, 0.3, 0.3]

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]


@pytest.fixture()
def store(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("CAIRN_VECTOR_DB_PATH", os.path.join(d, "v.db"))
        monkeypatch.setattr(vs, "_build_embedder", lambda: StubEmbedder(), raising=False)
        s = vs.HybridMemoryStore(embedder=StubEmbedder())
        yield s


def _add(store, text):
    return asyncio.run(store.add(text))


def _live_ids(store):
    return [r[0] for r in store._conn.execute(
        "SELECT id FROM memory_entries WHERE superseded_by IS NULL").fetchall()]


# ------------------------------------------------------------------ dedup
def test_identical_content_is_not_stored_twice(store):
    a = _add(store, "postgres_a we chose Postgres")
    b = _add(store, "postgres_a we chose Postgres")
    assert a == b


def test_the_same_fact_in_different_words_collapses(store, monkeypatch):
    """The actual failure: four rows, one fact, four phrasings."""
    monkeypatch.setattr(vs, "_classify", lambda new, old: "duplicate")
    a = _add(store, "postgres_a we decided to use Postgres because Mongo was flaky")
    b = _add(store, "postgres_b The team chose PostgreSQL as Mongo's transactions were flaky")
    assert a == b
    assert len(_live_ids(store)) == 1


def test_a_fuller_wording_replaces_a_thinner_one(store, monkeypatch):
    monkeypatch.setattr(vs, "_classify", lambda new, old: "duplicate")
    thin = _add(store, "postgres_a Postgres for billing")
    _add(store, "postgres_b The team decided on Postgres for billing because MongoDB's "
                "transaction support proved unreliable under load")
    row = store._conn.execute("SELECT content FROM memory_entries WHERE id=?", (thin,)).fetchone()
    assert "unreliable under load" in row[0]      # same row, richer content


def test_unrelated_facts_are_both_kept(store, monkeypatch):
    monkeypatch.setattr(vs, "_classify", lambda new, old: "independent")
    _add(store, "postgres_a Postgres for billing")
    _add(store, "dashboards merged the error rate dashboards")
    assert len(_live_ids(store)) == 2


def test_dedup_can_be_switched_off(store, monkeypatch):
    monkeypatch.setenv("CAIRN_DEDUP", "off")
    _add(store, "postgres_a one wording")
    _add(store, "postgres_b another wording")
    assert len(_live_ids(store)) == 2


# ------------------------------------------------------------ supersession
def test_a_changed_decision_retires_the_old_one(store, monkeypatch):
    monkeypatch.setattr(vs, "_classify", lambda new, old: "replaces")
    old = _add(store, "review_24h cap PR review turnaround at 24 hours")
    new = _add(store, "review_4h moved PR review turnaround to 4 hours after the incident")
    assert old != new
    row = store._conn.execute("SELECT superseded_by FROM memory_entries WHERE id=?",
                              (old,)).fetchone()
    assert row[0] == new                      # linked, not deleted — still auditable
    assert _live_ids(store) == [new]


def test_a_superseded_memory_is_never_recalled(store, monkeypatch):
    """The point: a stale answer *with a citation* is worse than no answer."""
    monkeypatch.setattr(vs, "_classify", lambda new, old: "replaces")
    old = _add(store, "review_24h cap PR review turnaround at 24 hours")
    _add(store, "review_4h moved PR review turnaround to 4 hours")
    assert store._fetch([old]) == {}


def test_complementary_facts_are_both_kept(store, monkeypatch):
    monkeypatch.setattr(vs, "_classify", lambda new, old: "independent")
    a = _add(store, "review_24h cap PR review turnaround at 24 hours")
    b = _add(store, "review_4h reviews are assigned round-robin")
    assert sorted(_live_ids(store)) == sorted([a, b])


def test_unclear_judgement_keeps_both(store, monkeypatch):
    """Wrongly retiring a live decision is worse than carrying a stale row, so a
    failed or unparseable classification falls back to "independent"."""
    from cairn import llm
    monkeypatch.setattr(llm, "extract_json", lambda *a, **k: None)   # model down
    _add(store, "review_24h cap review at 24 hours")
    _add(store, "review_4h moved review to 4 hours")
    assert len(_live_ids(store)) == 2


def test_supersession_can_be_switched_off(store, monkeypatch):
    monkeypatch.setenv("CAIRN_SUPERSEDE", "off")
    monkeypatch.setattr(vs, "_classify", lambda new, old: "replaces")
    _add(store, "review_24h cap review at 24 hours")
    _add(store, "review_4h moved review to 4 hours")
    assert len(_live_ids(store)) == 2


def test_a_retired_row_cannot_absorb_its_replacement(store, monkeypatch):
    """_nearest ignores superseded rows, or the old decision would keep winning
    the dedup check and swallow every later revision."""
    monkeypatch.setattr(vs, "_classify", lambda new, old: "replaces")
    _add(store, "review_24h cap review at 24 hours")
    second = _add(store, "review_4h moved review to 4 hours")
    assert _live_ids(store) == [second]


# ------------------------------------------------------------- classification
def test_classifier_falls_back_to_independent(monkeypatch):
    """Distance cannot separate "same fact re-phrased" (0.12–0.15 on real
    vectors) from "the answer changed" (0.179), so a model call arbitrates. Any
    failure must land on the safe side."""
    from cairn import llm

    for reply in (None, {}, {"relation": "nonsense"}, {"other": "shape"}):
        monkeypatch.setattr(llm, "extract_json", lambda *a, _r=reply, **k: _r)
        assert vs._classify("new", "old") == "independent"


@pytest.mark.parametrize("relation", ["duplicate", "replaces", "independent"])
def test_classifier_passes_through_valid_verdicts(monkeypatch, relation):
    from cairn import llm
    monkeypatch.setattr(llm, "extract_json", lambda *a, **k: {"relation": relation})
    assert vs._classify("new", "old") == relation


def test_the_free_band_skips_the_model_call(store, monkeypatch):
    """Below the hard threshold there is nothing to decide — do not pay for it."""
    called = []
    monkeypatch.setattr(vs, "_classify", lambda n, o: called.append(1) or "duplicate")
    _add(store, "postgres_a Postgres for billing")
    _add(store, "postgres_a Postgres for billing")     # identical: caught earlier still
    _add(store, "postgres_c Postgres for billing now")  # distance ~0.005
    assert called == []


# ------------------------------------------------------------------- chains
def test_a_new_decision_retires_every_version_it_replaces(store, monkeypatch):
    """Decisions chain: 24h -> 4h -> 8h. Comparing only against the closest
    memory retires the middle link and leaves the *original* live — still
    well-sourced, still confidently wrong. A live run did exactly that.
    """
    monkeypatch.setattr(vs, "_classify", lambda new, old: "replaces")
    first = _add(store, "review_24h cap PR review turnaround at 24 hours")
    second = _add(store, "review_4h moved PR review turnaround to 4 hours")
    assert _live_ids(store) == [second]

    monkeypatch.setattr(store, "_neighbours", lambda *a, **k: [
        (second, "moved PR review turnaround to 4 hours", 0.2),
        (first, "cap PR review turnaround at 24 hours", 0.25),
    ])
    third = _add(store, "review_4h review turnaround is now 8 hours")
    assert _live_ids(store) == [third]          # both older links retired


def test_the_walk_stops_once_neighbours_are_unrelated(store, monkeypatch):
    """Neighbours come back sorted by distance, so the first one beyond the band
    ends the walk — no model call for things that are obviously unrelated."""
    calls = []
    monkeypatch.setattr(vs, "_classify",
                        lambda n, o: calls.append(o) or "independent")
    monkeypatch.setattr(store, "_neighbours", lambda *a, **k: [
        ("1", "related enough", 0.30),
        ("2", "far away", 0.90),
        ("3", "further still", 0.95),
    ])
    _add(store, "dashboards something new")
    assert calls == ["related enough"]


# ------------------------------------------------------------ verdict caching
def test_the_same_pair_is_only_judged_once(store, monkeypatch):
    """Same texts, same question — re-asking is a re-roll of the dice, not a
    re-examination. The live store gave opposite verdicts on one pair across two
    compaction runs."""
    calls = []
    monkeypatch.setattr(vs, "_classify",
                        lambda n, o: calls.append(1) or "independent")
    assert store._relation("new text", "old text") == "independent"
    assert store._relation("new text", "old text") == "independent"
    assert len(calls) == 1


def test_changing_either_text_asks_again():
    """A different pair is a different question."""
    assert vs._relation_key("a", "b") != vs._relation_key("a", "c")
    assert vs._relation_key("a", "b") != vs._relation_key("x", "b")
    assert vs._relation_key("a", "b") == vs._relation_key("a", "b")


def test_recheck_forces_a_fresh_judgement(store, monkeypatch):
    """The escape hatch. A cached "independent" leaves both rows live, so a
    wrong one would otherwise be locked in forever — and compaction would keep
    reading the cache instead of correcting it."""
    verdicts = iter(["independent", "duplicate"])
    monkeypatch.setattr(vs, "_classify", lambda n, o: next(verdicts))
    assert store._relation("new", "old") == "independent"
    assert store._relation("new", "old") == "independent"          # cached
    assert store._relation("new", "old", recheck=True) == "duplicate"
    assert store._relation("new", "old") == "duplicate"            # re-cached


def test_a_cache_failure_still_returns_a_verdict(store, monkeypatch):
    monkeypatch.setattr(vs, "_cached_relation",
                        lambda c, k: (_ for _ in ()).throw(RuntimeError("no table")))
    monkeypatch.setattr(vs, "_classify", lambda n, o: "duplicate")
    assert store._relation("new", "old") == "duplicate"
