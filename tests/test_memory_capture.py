"""Tests for passive memory capture (decisions/commitments from channel chatter).

Runnable two ways:
    python -m tests.test_memory_capture
    pytest tests/test_memory_capture.py

No network, no model calls, no store — the extraction call and the store write
are stubbed so we test the gate + provenance wiring only.
"""
from __future__ import annotations

import os


def test_gate_matches_decisions_and_commitments() -> None:
    from loop.memory_capture import looks_memorable

    assert looks_memorable("we decided to go with Postgres")
    assert looks_memorable("let's use Redis for the cache")
    assert looks_memorable("John will fix the pipeline by Friday")  # action-item side
    assert looks_memorable("going forward we deploy on Fridays")


def test_gate_rejects_chitchat() -> None:
    from loop.memory_capture import looks_memorable

    assert not looks_memorable("lol nice one")
    assert not looks_memorable("what time is standup?")
    assert not looks_memorable("thanks everyone 🙏")


def test_capture_disabled_is_noop() -> None:
    import loop.memory_capture as mc

    os.environ["CAIRN_MEMORY_CAPTURE"] = "off"
    calls: list = []
    mc._save = lambda content, metadata: calls.append((content, metadata))  # type: ignore[assignment]
    mc._extract_memory = lambda text: "should not run"  # type: ignore[assignment]
    assert mc.capture("we decided to use Postgres", channel="C1") is None
    assert calls == []


def test_capture_saves_with_provenance() -> None:
    import loop.memory_capture as mc

    os.environ["CAIRN_MEMORY_CAPTURE"] = "on"
    saved: list = []
    mc._save = lambda content, metadata: saved.append((content, metadata))  # type: ignore[assignment]
    mc._extract_memory = lambda text: "Team chose Postgres as the primary DB."  # type: ignore[assignment]

    out = mc.capture(
        "we decided to go with Postgres",
        author="U1", channel="C1", team="T1", source="rasputin",
    )
    assert out == "Team chose Postgres as the primary DB.", out
    assert len(saved) == 1, saved
    content, meta = saved[0]
    assert content == "Team chose Postgres as the primary DB."
    assert meta["kind"] == "decision"
    assert meta["author"] == "U1" and meta["channel"] == "C1"
    assert meta["team"] == "T1" and meta["source"] == "rasputin"


def test_capture_skips_when_gate_fails_no_llm() -> None:
    import loop.memory_capture as mc

    os.environ["CAIRN_MEMORY_CAPTURE"] = "on"
    called: list = []
    mc._extract_memory = lambda text: called.append(text) or "x"  # type: ignore[assignment]
    mc._save = lambda content, metadata: None  # type: ignore[assignment]
    assert mc.capture("just some banter", channel="C1") is None
    assert called == [], "extraction must not run when the regex gate fails"


def test_capture_skips_when_not_memorable() -> None:
    import loop.memory_capture as mc

    os.environ["CAIRN_MEMORY_CAPTURE"] = "on"
    saved: list = []
    mc._save = lambda content, metadata: saved.append(1)  # type: ignore[assignment]
    mc._extract_memory = lambda text: None  # model says not memorable
    assert mc.capture("we decided something vague", channel="C1") is None
    assert saved == []


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())


# ------------------------------------------------- provider-agnostic extraction
def test_extract_json_tolerates_fences_and_prose():
    """Models wrap JSON in different ways; the parser must survive all of them."""
    from loop.llm import _parse_json

    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure! Here you go: {"a": 1} — hope that helps') == {"a": 1}
    assert _parse_json("not json at all") is None
    assert _parse_json('[1, 2, 3]') is None          # must be an object


def test_extraction_uses_the_provider_factory(monkeypatch):
    """Regression: extract_json spoke Anthropic's HTTP wire format directly, so
    every passive capture failed with a 401 the moment the project moved to
    Gemini — while its own docstring claimed it followed the configured provider.
    """
    from loop import llm

    built = {}
    monkeypatch.setattr(llm, "_json_agent", None)

    class FakeAgent:
        messages: list = []

        def __init__(self, **kw):
            built.update(kw)

        def __call__(self, _prompt):
            return '{"ok": true}'

    import strands
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr("loop.agent._build_model", lambda mid=None: f"model:{mid}")

    assert llm.extract_json("anything") == {"ok": True}
    assert built["model"] == "model:None"      # came from the factory, not httpx
    assert built["tools"] == []
