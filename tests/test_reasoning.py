"""Tests for stripping the model's <think> deliberation out of the reply.

Runnable two ways:
    python -m tests.test_reasoning
    pytest tests/test_reasoning.py

No network, no model calls — exercises the pure text splitter.
"""
from __future__ import annotations


def test_strips_paired_think_block() -> None:
    from loop.agent import _split_reasoning

    raw = "<think>Let me check the tools. Yes, both exist.</think>Filed KAN-8. Done."
    clean, thinking = _split_reasoning(raw)
    assert clean == "Filed KAN-8. Done.", clean
    assert "check the tools" in thinking, thinking
    assert "<think>" not in clean and "</think>" not in clean


def test_strips_multiple_and_thinking_alias() -> None:
    from loop.agent import _split_reasoning

    raw = "<thinking>a</thinking>Hello <think>b</think>world"
    clean, thinking = _split_reasoning(raw)
    assert clean == "Hello world", repr(clean)
    assert "a" in thinking and "b" in thinking


def test_unclosed_open_tag_is_all_reasoning() -> None:
    from loop.agent import _split_reasoning

    # max_tokens cut off the close tag: everything after <think> is reasoning.
    raw = "Here's the plan.\n<think>now I'm deliberating and it got cut off"
    clean, thinking = _split_reasoning(raw)
    assert clean == "Here's the plan.", repr(clean)
    assert "deliberating" in thinking


def test_stray_close_tag() -> None:
    from loop.agent import _split_reasoning

    raw = "reasoning without an open</think>Actual answer."
    clean, thinking = _split_reasoning(raw)
    assert clean == "Actual answer.", repr(clean)
    assert "reasoning without an open" in thinking


def test_plain_text_untouched() -> None:
    from loop.agent import _split_reasoning

    clean, thinking = _split_reasoning("Just a normal reply.")
    assert clean == "Just a normal reply."
    assert thinking == ""


def test_empty() -> None:
    from loop.agent import _split_reasoning

    assert _split_reasoning("") == ("", "")


def test_thinking_params_default_on() -> None:
    import os
    from loop.agent import _thinking_params

    os.environ.pop("LOOP_THINKING_BUDGET", None)
    got = _thinking_params()
    assert got == {"thinking": {"type": "enabled", "budget_tokens": 1024}}, got


def test_thinking_params_off_and_clamped() -> None:
    import os
    from loop.agent import _thinking_params

    os.environ["LOOP_THINKING_BUDGET"] = "off"
    assert _thinking_params() == {}
    # budget is clamped below max_tokens so the request stays valid
    os.environ["LOOP_THINKING_BUDGET"] = "999999"
    os.environ["LOOP_MAX_TOKENS"] = "2048"
    got = _thinking_params()
    assert got["thinking"]["budget_tokens"] == 2048 - 512, got
    os.environ.pop("LOOP_THINKING_BUDGET", None)
    os.environ.pop("LOOP_MAX_TOKENS", None)


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
