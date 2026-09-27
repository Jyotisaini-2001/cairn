"""Jev as the duplicate / replaces / independent classifier.

The HTTP call is stubbed: these pin the contract Cairn relies on — typed answer
in, confidence-gated verdict out, and a fall-through to the LLM whenever Jev is
off, unreachable, or answers something unusable.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from cairn import jev, llm
from cairn import vector_store as vs


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv("CAIRN_CLASSIFIER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev.time, "sleep", lambda s: None)


def _answer(choice, confidence=None, probabilities=None):
    a = {"type": "choice", "choice": choice}
    if confidence is not None:
        a["confidence"] = confidence
    if probabilities is not None:
        a["probabilities"] = probabilities
    return {"model": "jev-1.13.0", "answers": {"q": a},
            "usage": {"input_tokens": 120, "output_tokens": 1}}


def _http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b""))


# ------------------------------------------------------------------ switch
def test_off_by_default(monkeypatch):
    monkeypatch.delenv("CAIRN_CLASSIFIER", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert not jev.enabled()


def test_needs_a_key_as_well_as_the_switch(monkeypatch):
    """CAIRN_CLASSIFIER=jev without a key must not break capture — it stays off."""
    monkeypatch.setenv("CAIRN_CLASSIFIER", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert not jev.enabled()


# ------------------------------------------------------------------ client
def test_sends_one_typed_choice_question(jev_on, monkeypatch):
    sent = []
    monkeypatch.setattr(jev, "_post", lambda p: sent.append(p) or _answer("duplicate", 0.9))
    jev.choice({"old": "a"}, "which?", {"duplicate": "d", "independent": "i"})
    q = sent[0]["questions"]["q"]
    assert sent[0]["model"] == "jev-latest"
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"duplicate", "independent"}


def test_parses_choice_and_confidence(jev_on, monkeypatch):
    monkeypatch.setattr(jev, "_post", lambda p: _answer("replaces", 0.83, {"replaces": 0.9}))
    got = jev.choice("s", "i", {"replaces": "r", "independent": "i"})
    assert got.choice == "replaces" and got.confidence == 0.83


def test_derives_confidence_when_the_api_omits_it(jev_on, monkeypatch):
    """Three options, top probability 0.9 -> (3*0.9 - 1) / 2 = 0.85."""
    monkeypatch.setattr(jev, "_post", lambda p: _answer(
        "duplicate", probabilities={"duplicate": 0.9, "replaces": 0.05, "independent": 0.05}))
    got = jev.choice("s", "i", {"duplicate": "", "replaces": "", "independent": ""})
    assert got.confidence == pytest.approx(0.85)


def test_an_answer_outside_the_options_is_unusable(jev_on, monkeypatch):
    monkeypatch.setattr(jev, "_post", lambda p: _answer("maybe", 0.99))
    assert jev.choice("s", "i", {"duplicate": "", "independent": ""}) is None


def test_bad_key_fails_without_retrying(jev_on, monkeypatch):
    calls = []

    def post(p):
        calls.append(1)
        raise _http_error(401)
    monkeypatch.setattr(jev, "_post", post)
    assert jev.choice("s", "i", {"duplicate": ""}) is None
    assert len(calls) == 1


def test_rate_limit_is_retried_once(jev_on, monkeypatch):
    replies = iter([_http_error(429), _answer("duplicate", 0.95)])

    def post(p):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(jev, "_post", post)
    assert jev.choice("s", "i", {"duplicate": ""}).choice == "duplicate"


# -------------------------------------------------------------- classifier
def _llm_must_not_run(monkeypatch):
    monkeypatch.setattr(llm, "extract_json",
                        lambda *a, **k: pytest.fail("LLM called while Jev answered"))


@pytest.mark.parametrize("relation", ["duplicate", "replaces", "independent"])
def test_a_confident_verdict_is_used_as_is(jev_on, monkeypatch, relation):
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _answer(relation, 0.95))
    assert vs._classify("new", "old") == relation


@pytest.mark.parametrize("relation", ["duplicate", "replaces"])
def test_an_unsure_destructive_verdict_keeps_both(jev_on, monkeypatch, relation):
    """Both verdicts take a row out of recall. Wrongly retiring a live decision
    is the failure nobody notices, so an unsure one keeps both instead."""
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _answer(relation, 0.4))
    assert vs._classify("new", "old") == "independent"


def test_the_confidence_bar_is_configurable(jev_on, monkeypatch):
    monkeypatch.setenv("CAIRN_JEV_MIN_CONFIDENCE", "0.3")
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _answer("replaces", 0.4))
    assert vs._classify("new", "old") == "replaces"


def test_jev_down_falls_back_to_the_llm(jev_on, monkeypatch):
    monkeypatch.setattr(jev, "_post", lambda p: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(llm, "extract_json", lambda *a, **k: {"relation": "replaces"})
    assert vs._classify("new", "old") == "replaces"


def test_jev_off_uses_the_llm(monkeypatch):
    monkeypatch.delenv("CAIRN_CLASSIFIER", raising=False)
    monkeypatch.setattr(jev, "_post", lambda p: pytest.fail("Jev called while off"))
    monkeypatch.setattr(llm, "extract_json", lambda *a, **k: {"relation": "duplicate"})
    assert vs._classify("new", "old") == "duplicate"
