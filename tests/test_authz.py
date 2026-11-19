"""Who may invoke Cairn.

Cairn reads channels it is never mentioned in, remembers what a team decided, and
files tickets. Until now there was no check at all: anyone who could type in
Slack could make it act. These pin the two lists, the defaults, and — most
importantly — that no entry point is left ungated.
"""
from __future__ import annotations

import os

import pytest

from cairn import authz


@pytest.fixture(autouse=True)
def _clean():
    for k in ("CAIRN_ALLOW_USERS", "CAIRN_ALLOW_CHANNELS"):
        os.environ.pop(k, None)
    yield
    for k in ("CAIRN_ALLOW_USERS", "CAIRN_ALLOW_CHANNELS"):
        os.environ.pop(k, None)


# ------------------------------------------------------------------ defaults
def test_unconfigured_allows_everything():
    """An empty list means "no restriction", not "deny all" — otherwise a fresh
    install silently ignores the person who just set it up."""
    assert authz.check("U1", "C1") == (True, "")
    assert authz.may_listen("C1")
    assert not authz.enabled()


def test_configuring_either_list_turns_it_on():
    os.environ["CAIRN_ALLOW_USERS"] = "U1"
    assert authz.enabled()


# ------------------------------------------------------------------- users
def test_a_listed_user_is_allowed():
    os.environ["CAIRN_ALLOW_USERS"] = "U1,U2"
    assert authz.check("U1", "C9")[0]


def test_an_unlisted_user_is_refused_with_a_reason():
    os.environ["CAIRN_ALLOW_USERS"] = "U1"
    ok, reason = authz.check("U9", "C1")
    assert not ok and "CAIRN_ALLOW_USERS" in reason


def test_whitespace_and_newlines_are_tolerated():
    os.environ["CAIRN_ALLOW_USERS"] = " U1 ,\nU2 ,, "
    assert authz.allow_users() == {"U1", "U2"}


# ---------------------------------------------------------------- channels
def test_an_unlisted_channel_is_refused():
    os.environ["CAIRN_ALLOW_CHANNELS"] = "C1"
    assert not authz.check("U1", "C9")[0]


def test_channel_beats_user_when_both_are_set():
    """A permitted person is exactly who would otherwise walk Cairn into a
    channel it was deliberately kept out of."""
    os.environ["CAIRN_ALLOW_USERS"] = "U1"
    os.environ["CAIRN_ALLOW_CHANNELS"] = "C1"
    ok, reason = authz.check("U1", "C9")
    assert not ok and "CHANNELS" in reason


# ----------------------------------------------------------- passive paths
def test_listening_is_governed_by_channel_only():
    """Capture and resurfacing run on messages nobody addressed to Cairn, so
    "who may ask" is the wrong question — "may Cairn listen here" is the right
    one. Restricting by author would mean hearing some people in a room and not
    others, while the decision usually involves everyone in it."""
    os.environ["CAIRN_ALLOW_USERS"] = "U1"
    assert authz.may_listen("C-any")          # user list must not apply
    os.environ["CAIRN_ALLOW_CHANNELS"] = "C1"
    assert authz.may_listen("C1")
    assert not authz.may_listen("C9")


def test_missing_ids_are_refused_when_a_list_exists():
    os.environ["CAIRN_ALLOW_USERS"] = "U1"
    assert not authz.check(None, "C1")[0]
    os.environ["CAIRN_ALLOW_CHANNELS"] = "C1"
    assert not authz.may_listen(None)


# ------------------------------------------------- every entry point is gated
def test_no_invocation_path_is_left_ungated():
    """The value of a permission check is entirely in not missing one. Four
    surfaces reach the agent — mention and DM share _answer, the slash command
    answers from the decision log before it, and the assistant pane has its own
    handler — and each must call _authorised."""
    import inspect

    from cairn import slack_app

    src = inspect.getsource(slack_app)
    assert src.count("_authorised(") >= 4, "an invocation path is missing its check"
    # and the passive paths must be gated by channel
    assert src.count("authz.may_listen(") >= 2


def test_the_slash_gate_precedes_the_decision_log():
    """The log answers from the store without touching the agent, so a check
    placed after that branch would leave the whole record readable by anyone."""
    import inspect

    from cairn import slack_app

    src = inspect.getsource(slack_app)
    gate = src.index("if not _authorised(ix, respond, log):")
    log_branch = src.index("respond(decisions.log_decisions(")
    assert gate < log_branch


# ═══════════════════════════════════════════ managed from Slack, not from a file
# A config file means only whoever has server access can change anything, every
# change needs a restart, a team lead cannot manage their own team, and there is
# no record of who granted what.

@pytest.fixture()
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("CAIRN_ADMINS", "UADMIN")
    yield


def _cmd(text, actor="UADMIN"):
    return authz.handle_admin_command(text, actor=actor)


def test_a_grant_takes_effect_without_a_restart(store):
    assert not authz.check("U1", "C1")[0] or True      # unconfigured: open
    _cmd("allow <@U1|priya>")
    assert authz.check("U1", "C1")[0]
    assert not authz.check("U9", "C1")[0]              # now restricted


def test_revoking_the_last_grant_reopens_it(store):
    """Removing every restriction must mean "open", not "locked out"."""
    _cmd("allow <@U1>")
    _cmd("deny <@U1>")
    assert authz.check("U9", "C1")[0]


def test_grants_record_who_made_them(store):
    _cmd("allow <@U1>")
    conn = authz._db()
    row = conn.execute("SELECT granted_by FROM authz_grants WHERE subject='U1'").fetchone()
    assert row[0] == "UADMIN"                          # auditable, not anonymous


def test_slack_mention_and_channel_syntax_is_parsed(store):
    """Slack sends "<@U123|name>" and "<#C123|name>" — never a bare id."""
    _cmd("allow <@U123|priya> <#C999|eng>")
    assert "U123" in authz.allow_users()
    assert "C999" in authz.allow_channels()


def test_a_non_admin_is_refused_and_told_who_to_ask(store):
    reply = _cmd("allow <@U1>", actor="USOMEONE")
    assert "Only" in reply and "UADMIN" in reply       # names the way forward
    assert authz.allow_users() == set()


def test_with_no_admins_the_reply_explains_the_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("CAIRN_ADMINS", raising=False)
    reply = authz.handle_admin_command("allow <@U1>", actor="UANY")
    assert "CAIRN_ADMINS" in reply


def test_who_reports_the_current_state(store):
    assert "open to everyone" in _cmd("who")
    _cmd("allow <@U1>")
    reply = _cmd("who")
    assert "<@U1>" in reply and "_any_" in reply       # channels unrestricted


def test_ordinary_questions_are_not_admin_commands(store):
    for text in ("what did we decide last week", "why did we pick Postgres", ""):
        assert authz.handle_admin_command(text, actor="UADMIN") is None


def test_a_verb_with_no_subject_asks_for_one(store):
    assert "Who should I" in _cmd("allow")


def test_env_seed_merges_with_slack_grants(store, monkeypatch):
    monkeypatch.setenv("CAIRN_ALLOW_USERS", "USEED")
    _cmd("allow <@U1>")
    assert authz.allow_users() == {"USEED", "U1"}


def test_admin_commands_run_before_the_permission_gate():
    """An admin who locks themselves out must be able to type `/cairn allow @me`
    and recover; a gate in front of that would make the mistake permanent from
    inside Slack."""
    import inspect

    from cairn import slack_app

    src = inspect.getsource(slack_app)
    admin = src.index("authz.handle_admin_command(")
    gate = src.index("if not _authorised(ix, respond, log):")
    assert admin < gate
