"""The shared conversation reader (loop/conversation.py).

Context, capture and backfill all read Slack the same way through this module,
so these cover normalisation and the thread-vs-channel routing once, for all
three.
"""
from __future__ import annotations

import os

from loop import conversation as conv


class FakeClient:
    def __init__(self, replies=None, history=None, raises=None):
        self.replies, self.history, self.raises = replies or [], history or [], raises
        self.calls = []

    def conversations_replies(self, **kw):
        self.calls.append(("replies", kw))
        if self.raises == "replies":
            raise RuntimeError("missing_scope")
        return {"messages": self.replies}

    def conversations_history(self, **kw):
        self.calls.append(("history", kw))
        if self.raises == "history":
            raise RuntimeError("not_in_channel")
        return {"messages": self.history}


def _m(ts, user, text, **extra):
    return {"ts": ts, "user": user, "text": text, **extra}


def setup_function(_f):
    for k in ("CAIRN_THREAD_CONTEXT", "CAIRN_CHANNEL_CONTEXT",
              "CAIRN_THREAD_CONTEXT_LIMIT", "CAIRN_CHANNEL_CONTEXT_LIMIT"):
        os.environ.pop(k, None)


# --------------------------------------------------------------- normalising
def test_mentions_are_stripped():
    turns = conv.to_turns([_m("1", "U1", "<@U9> why Postgres?")])
    assert turns[0]["text"] == "why Postgres?"


def test_mention_only_message_is_dropped():
    assert conv.to_turns([_m("1", "U1", "<@U9>")]) == []


def test_join_leave_noise_is_dropped():
    msgs = [_m("1", "U1", "joined", subtype="channel_join"),
            _m("2", "U1", "real message")]
    assert [t["text"] for t in conv.to_turns(msgs)] == ["real message"]


def test_bot_messages_are_labelled_you():
    msgs = [_m("1", "U1", "q"), _m("2", "B1", "a", bot_id="B1")]
    assert [t["speaker"] for t in conv.to_turns(msgs)] == ["<@U1>", "you"]


def test_bot_recognised_by_user_id_too():
    turns = conv.to_turns([_m("1", "UBOT", "a")], bot_user_id="UBOT")
    assert turns[0]["speaker"] == "you"


def test_excluded_ts_is_removed():
    msgs = [_m("1", "U1", "keep"), _m("2", "U1", "drop")]
    assert [t["text"] for t in conv.to_turns(msgs, exclude_ts="2")] == ["keep"]


def test_limit_keeps_the_most_recent():
    msgs = [_m(str(i), "U1", f"m{i}") for i in range(5)]
    assert [t["text"] for t in conv.to_turns(msgs, limit=2)] == ["m3", "m4"]


def test_char_cap_drops_oldest_first():
    msgs = [_m(str(i), "U1", "x" * 400) for i in range(5)]
    turns = conv.to_turns(msgs, limit=10, char_cap=1000)
    assert 0 < len(turns) <= 2      # newest survive, oldest trimmed


# ------------------------------------------------------------------ fetching
def test_read_thread_returns_turns():
    c = FakeClient(replies=[_m("1", "U1", "hello")])
    assert [t["text"] for t in conv.read_thread(c, "C1", "1")] == ["hello"]


def test_read_channel_reverses_to_oldest_first():
    # Slack returns history newest-first; turns must read chronologically.
    c = FakeClient(history=[_m("3", "U1", "third"), _m("2", "U1", "second"),
                            _m("1", "U1", "first")])
    assert [t["text"] for t in conv.read_channel(c, "C1")] == ["first", "second", "third"]


def test_missing_scope_degrades_to_empty():
    assert conv.read_thread(FakeClient(raises="replies"), "C1", "1") == []
    assert conv.read_channel(FakeClient(raises="history"), "C1") == []


def test_no_thread_ts_makes_no_api_call():
    c = FakeClient()
    assert conv.read_thread(c, "C1", "") == []
    assert c.calls == []


# ------------------------------------------------------------------- routing
def test_in_thread_reads_the_thread():
    c = FakeClient(replies=[_m("1", "U1", "from thread")],
                   history=[_m("9", "U1", "from channel")])
    turns = conv.read_context(c, "C1", thread_ts="1", in_thread=True)
    assert [t["text"] for t in turns] == ["from thread"]
    assert c.calls[0][0] == "replies"


def test_top_level_falls_back_to_channel_history():
    """The gap this module was built for: people @-mention in the channel and
    ask their follow-up in the channel too, so thread context never fires."""
    c = FakeClient(replies=[_m("1", "U1", "from thread")],
                   history=[_m("9", "U1", "from channel")])
    turns = conv.read_context(c, "C1", thread_ts="9", in_thread=False)
    assert [t["text"] for t in turns] == ["from channel"]
    assert c.calls[0][0] == "history"


def test_each_source_is_independently_switchable():
    c = FakeClient(replies=[_m("1", "U1", "t")], history=[_m("9", "U1", "c")])
    os.environ["CAIRN_CHANNEL_CONTEXT"] = "off"
    assert conv.read_context(c, "C1", thread_ts="9", in_thread=False) == []
    assert conv.read_context(c, "C1", thread_ts="1", in_thread=True) != []
    os.environ["CAIRN_THREAD_CONTEXT"] = "off"
    assert conv.read_context(c, "C1", thread_ts="1", in_thread=True) == []


def test_switched_off_skips_the_api_entirely():
    os.environ["CAIRN_CHANNEL_CONTEXT"] = "off"
    c = FakeClient(history=[_m("9", "U1", "c")])
    conv.read_context(c, "C1", in_thread=False)
    assert c.calls == []


def teardown_module(_m):
    setup_function(None)
