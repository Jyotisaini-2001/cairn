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


# ------------------------------------------------------------ yes/no checks
# Jev reads literally: asked only "how does NEW relate to OLD", it retired a
# permanent rule on reading a one-off, and an exception on reading a restated
# general rule, at up to 1.00 confidence. Two direct checks ride in the same call.
def _with_checks(relation, confidence=0.95, **checks):
    body = _answer(relation, confidence)
    for name, p in checks.items():
        body["answers"][name] = {"type": "noul", "noul": p}
    return body


def test_checks_are_asked_in_the_same_call(jev_on, monkeypatch):
    sent = []
    monkeypatch.setattr(jev, "_post", lambda p: sent.append(p) or _answer("independent", 0.9))
    vs._classify("new", "old")
    qs = sent[0]["questions"]
    assert len(sent) == 1
    assert qs["temporary"]["type"] == "noul" and qs["same_case"]["type"] == "noul"


def test_checks_can_be_switched_off(jev_on, monkeypatch):
    monkeypatch.setenv("CAIRN_JEV_CHECKS", "off")
    sent = []
    monkeypatch.setattr(jev, "_post", lambda p: sent.append(p) or _answer("independent", 0.9))
    vs._classify("new", "old")
    assert set(sent[0]["questions"]) == {"q"}


def test_check_answers_are_parsed(jev_on, monkeypatch):
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", temporary=0.2, same_case=0.9))
    got = jev.choice("s", "i", {"replaces": "", "independent": ""},
                     checks={"temporary": "t", "same_case": "c"})
    assert got.checks == {"temporary": 0.2, "same_case": 0.9}


def test_a_one_off_never_retires_the_permanent_rule(jev_on, monkeypatch):
    """'For this week's launch, deploy on Saturday' vs 'releases on weekdays'."""
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 0.99, temporary=0.9, same_case=0.9))
    assert vs._classify("new", "old") == "independent"


def test_a_rule_about_a_different_case_does_not_retire_the_exception(jev_on, monkeypatch):
    """'Daily standup is at 10:00' vs 'standups are async on Fridays'."""
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 1.0, temporary=0.1, same_case=0.2))
    assert vs._classify("new", "old") == "independent"


def test_a_real_replacement_still_retires(jev_on, monkeypatch):
    """'Review is Thursday 16:00' -> 'review is Monday 16:00' must still retire."""
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 1.0, temporary=0.05, same_case=0.95))
    assert vs._classify("new", "old") == "replaces"


def test_checks_do_not_touch_duplicates(jev_on, monkeypatch):
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("duplicate", 0.99, temporary=0.9, same_case=0.1))
    assert vs._classify("new", "old") == "duplicate"


# ------------------------------------------- TypeSafe's yes/no bands (0.2 / 0.8)
# Both checks guard a retirement, so only a clear answer lets one through; the
# unsure band between keeps both (and is where a question to a person goes later).
def test_checks_carry_true_and_false_criteria(jev_on, monkeypatch):
    sent = []
    monkeypatch.setattr(jev, "_post", lambda p: sent.append(p) or _answer("independent", 0.9))
    vs._classify("new", "old")
    for name in ("temporary", "same_case"):
        assert set(sent[0]["questions"][name]["criteria"]) == {"true", "false"}


def test_maybe_temporary_keeps_both(jev_on, monkeypatch):
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 1.0, temporary=0.3, same_case=0.95))
    assert vs._classify("new", "old") == "independent"


def test_maybe_same_case_keeps_both(jev_on, monkeypatch):
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 1.0, temporary=0.05, same_case=0.7))
    assert vs._classify("new", "old") == "independent"


def test_high_stakes_bar_is_typesafes_085(jev_on, monkeypatch):
    monkeypatch.delenv("CAIRN_JEV_MIN_CONFIDENCE", raising=False)
    _llm_must_not_run(monkeypatch)
    monkeypatch.setattr(jev, "_post", lambda p: _with_checks("replaces", 0.8, temporary=0.05, same_case=0.95))
    assert vs._classify("new", "old") == "independent"
