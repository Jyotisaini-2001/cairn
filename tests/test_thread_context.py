"""Thread context (CAIRN_THREAD_CONTEXT).

Every turn used to be answered blind — long-term memory was the only state, so a
follow-up like "what about the second one?" had no referent. These cover the
fetch + render plumbing; whether the model *uses* it well is the golden set's job.
"""
from __future__ import annotations

import os

from cairn.agent import _format_thread, _with_context
from cairn.slack_app import _thread_history, _thread_limit


class FakeClient:
    """Minimal stand-in for the Slack WebClient."""

    def __init__(self, messages=None, raises=False):
        self._messages = messages or []
        self._raises = raises
        self.calls = []

    def conversations_replies(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise RuntimeError("missing_scope")
        return {"messages": self._messages}


def _msg(ts, user, text, bot=False):
    m = {"ts": ts, "user": user, "text": text}
    if bot:
        m["bot_id"] = "B1"
    return m


def setup_function(_f):
    os.environ.pop("CAIRN_THREAD_CONTEXT", None)
    os.environ.pop("CAIRN_THREAD_CONTEXT_LIMIT", None)


# ---------------------------------------------------------------- fetching
def test_excludes_the_message_being_answered():
    c = FakeClient([_msg("1", "U1", "first"), _msg("2", "U1", "current")])
    turns = _thread_history(c, "C1", "1", current_ts="2", bot_user_id="B")
    assert [t["text"] for t in turns] == ["first"]


def test_labels_the_bot_as_you():
    c = FakeClient([_msg("1", "U1", "q"), _msg("2", "B", "a", bot=True)])
    turns = _thread_history(c, "C1", "1", current_ts="9", bot_user_id="B")
    assert [t["speaker"] for t in turns] == ["<@U1>", "you"]


def test_keeps_only_the_most_recent_turns():
    os.environ["CAIRN_THREAD_CONTEXT_LIMIT"] = "2"
    msgs = [_msg(str(i), "U1", f"m{i}") for i in range(6)]
    turns = _thread_history(FakeClient(msgs), "C1", "0", current_ts="9", bot_user_id="B")
    assert [t["text"] for t in turns] == ["m4", "m5"]


def test_char_cap_drops_oldest_turns():
    msgs = [_msg(str(i), "U1", "x" * 3000) for i in range(4)]
    turns = _thread_history(FakeClient(msgs), "C1", "0", current_ts="9", bot_user_id="B")
    assert sum(len(t["text"]) for t in turns) <= 4000


def test_api_failure_degrades_to_no_context():
    turns = _thread_history(FakeClient(raises=True), "C1", "1", "9", "B")
    assert turns == []          # a missing scope must never break the reply


def test_switch_off_skips_the_api_call_entirely():
    os.environ["CAIRN_THREAD_CONTEXT"] = "off"
    c = FakeClient([_msg("1", "U1", "hi")])
    assert _thread_history(c, "C1", "1", "9", "B") == []
    assert c.calls == []        # tokens *and* an API round-trip saved


def test_blank_messages_are_dropped():
    c = FakeClient([_msg("1", "U1", "   "), _msg("2", "U1", "real")])
    assert [t["text"] for t in _thread_history(c, "C1", "1", "9", "B")] == ["real"]


def test_limit_env_falls_back_on_garbage():
    os.environ["CAIRN_THREAD_CONTEXT_LIMIT"] = "not-a-number"
    assert _thread_limit() == 10


# ---------------------------------------------------------------- rendering
def test_render_is_empty_without_turns():
    assert _format_thread(None) == ""
    assert _format_thread([]) == ""


def test_render_orders_and_labels():
    out = _format_thread([{"speaker": "<@U1>", "text": "q"},
                          {"speaker": "you", "text": "a"}])
    assert out.index("<@U1>: q") < out.index("you: a")
    assert "thread so far" in out


def test_prompt_carries_thread_and_question():
    out = _with_context("and the second?", {
        "user": "U1", "channel": "C1",
        "thread": [{"speaker": "<@U1>", "text": "Postgres or Mongo?"}]})
    assert "Postgres or Mongo?" in out
    assert out.rstrip().endswith("and the second?")


def test_prompt_unchanged_when_no_thread():
    out = _with_context("hi", {"user": "U1"})
    assert "thread so far" not in out


def teardown_module(_m):
    setup_function(None)


# ------------------------------------------------- regression: parent vs own ts
def test_context_envelope_carries_the_threads_parent_not_the_reply():
    """A threaded reply must fetch history from the thread's PARENT.

    Regression: the fetch used the Interaction's thread_ts, which is the
    message's *own* ts (correct for replying — it roots Cairn's answer under the
    mention — but wrong for reading). conversations_replies on a reply's ts
    returns just that one message, so context silently came back empty and the
    model burned ~25k extra tokens calling the Slack API to compensate.
    """
    from cairn.slack_app import _ctx

    reply = {"user": "U1", "channel": "C1", "ts": "222.2", "thread_ts": "111.1"}
    ctx = _ctx(reply)
    assert ctx["thread_ts"] == "111.1"     # the parent — what we fetch with
    assert ctx["ts"] == "222.2"            # this message — what we exclude
    assert ctx["in_thread"] is True

    top_level = {"user": "U1", "channel": "C1", "ts": "333.3"}
    ctx = _ctx(top_level)
    assert ctx["thread_ts"] == ctx["ts"] == "333.3"
    assert ctx["in_thread"] is False
