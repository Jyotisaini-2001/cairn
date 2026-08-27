"""Who is allowed to invoke Cairn.

Cairn reads channels it is never mentioned in, remembers what a team decided, and
files tickets. All of that is fine when the workspace is one person; none of it
is fine by default when it is forty. Until recently there was no check at all.

**Permissions live in the database and are managed from Slack**, not in a config
file. A config file means only whoever has server access can change anything,
every change needs a restart, a team lead cannot manage their own team, and there
is no record of who granted what. `.env` keeps exactly one thing — the bootstrap
admin list — because somebody has to be able to grant the *first* permission and
they cannot do it through a system that is already locked:

    CAIRN_ADMINS=U012,U345          who may run /cairn allow|deny
    CAIRN_ALLOW_USERS / _CHANNELS   optional seed, merged with the DB

Both lists empty (no admins, no grants) means **no restriction** — a fresh
install must not silently ignore the person who just set it up.

Two dimensions, both checked for an invocation: a permitted user in a forbidden
channel is refused, because the channel rule usually exists to keep Cairn out of
somewhere sensitive and a permitted person is exactly who would otherwise walk it
in.

**Passive paths are governed by the channel list only.** Capture and resurfacing
read messages nobody addressed to Cairn, so "who is allowed to ask" is the wrong
question for them — "may Cairn listen here" is the right one.

Grants are recorded with who made them and when, because a permission system
nobody can audit is a permission system nobody trusts.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

log = logging.getLogger("cairn.authz")

_KIND_USER, _KIND_CHANNEL = "user", "channel"


# ── storage ──────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection | None:
    """The grants table, or None if it cannot be opened.

    A storage failure must never lock everyone out — it degrades to whatever the
    env lists say, which for an unconfigured install is "allow".
    """
    try:
        path = os.environ.get("DATABASE_PATH", "./data/cairn.db")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS authz_grants (
            kind       TEXT NOT NULL,          -- 'user' | 'channel'
            subject    TEXT NOT NULL,          -- Slack id
            granted_by TEXT,
            granted_at REAL NOT NULL,
            PRIMARY KEY (kind, subject))""")
        conn.commit()
        return conn
    except Exception as err:  # noqa: BLE001
        log.warning("authz store unavailable (%s); falling back to env lists", err)
        return None


def _stored(kind: str) -> set[str]:
    conn = _db()
    if conn is None:
        return set()
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT subject FROM authz_grants WHERE kind=?", (kind,)).fetchall()}
    except Exception:  # noqa: BLE001
        return set()


def grant(kind: str, subject: str, by: str | None = None) -> bool:
    conn = _db()
    if conn is None or kind not in (_KIND_USER, _KIND_CHANNEL) or not subject:
        return False
    try:
        conn.execute("INSERT OR REPLACE INTO authz_grants VALUES (?,?,?,?)",
                     (kind, subject, by, time.time()))
        conn.commit()
        log.info("authz: %s %s granted by %s", kind, subject, by)
        return True
    except Exception:  # noqa: BLE001
        log.exception("authz: grant failed")
        return False


def revoke(kind: str, subject: str) -> bool:
    conn = _db()
    if conn is None:
        return False
    try:
        cur = conn.execute("DELETE FROM authz_grants WHERE kind=? AND subject=?",
                           (kind, subject))
        conn.commit()
        log.info("authz: %s %s revoked", kind, subject)
        return bool(getattr(cur, "rowcount", 0))
    except Exception:  # noqa: BLE001
        log.exception("authz: revoke failed")
        return False


def _ids(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()}


def admins() -> set[str]:
    """Who may change permissions. Env-only and set once at install: this is the
    bootstrap, and it cannot itself be granted through the system it unlocks."""
    return _ids("CAIRN_ADMINS")


def allow_users() -> set[str]:
    """Granted in Slack, plus any seeded in env."""
    return _stored(_KIND_USER) | _ids("CAIRN_ALLOW_USERS")


def allow_channels() -> set[str]:
    return _stored(_KIND_CHANNEL) | _ids("CAIRN_ALLOW_CHANNELS")


def is_admin(user: str | None) -> bool:
    """With no admins configured, nobody can manage permissions — which is
    correct: an unconfigured install has no restrictions to manage."""
    return bool(user) and user in admins()


def enabled() -> bool:
    """Is any restriction configured at all?"""
    return bool(allow_users() or allow_channels())


def check(user: str | None, channel: str | None) -> tuple[bool, str]:
    """``(allowed, reason)`` for an *invocation* — a mention, DM, or command.

    `reason` is empty when allowed, and otherwise names which list refused, so a
    denial in the logs can be acted on without guessing.
    """
    users, channels = allow_users(), allow_channels()
    if channels and (channel or "") not in channels:
        return False, f"channel {channel} is not in CAIRN_ALLOW_CHANNELS"
    if users and (user or "") not in users:
        return False, f"user {user} is not in CAIRN_ALLOW_USERS"
    return True, ""


def may_listen(channel: str | None) -> bool:
    """May Cairn passively read (capture / resurface) in this channel?

    Only the channel list applies: these paths run on messages nobody addressed
    to Cairn, so restricting them by author would mean listening to some people
    in a room and not others — which is both odd and useless, since the decision
    being captured usually involves everyone in it.
    """
    channels = allow_channels()
    return not channels or (channel or "") in channels


DENIAL_MESSAGE = (
    ":lock: I'm not enabled for you here. Ask whoever set Cairn up to add you "
    "to `CAIRN_ALLOW_USERS`, or this channel to `CAIRN_ALLOW_CHANNELS`."
)


# ── the /cairn allow|deny|who subcommands ────────────────────────────────────
# Managed from Slack so a team lead can run their own team without server access
# and without a restart. Slack sends mentions as "<@U123|name>" and channels as
# "<#C123|name>", so the id is parsed out rather than asked for.
import re  # noqa: E402

_SUBJECT_RE = re.compile(r"<([@#])([A-Z0-9]+)(?:\|[^>]*)?>|(\b[UWC][A-Z0-9]{6,}\b)")
_ADMIN_CMD_RE = re.compile(r"^\s*(allow|deny|revoke|who|permissions)\b\s*(.*)$",
                           re.IGNORECASE | re.DOTALL)


def parse_admin_command(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """``(verb, [(kind, id), …])`` for an admin subcommand, else None."""
    m = _ADMIN_CMD_RE.match(text or "")
    if not m:
        return None
    verb = m.group(1).lower()
    verb = "deny" if verb == "revoke" else ("who" if verb == "permissions" else verb)
    subjects: list[tuple[str, str]] = []
    for sigil, sid, bare in _SUBJECT_RE.findall(m.group(2) or ""):
        ident = sid or bare
        kind = _KIND_CHANNEL if (sigil == "#" or ident.startswith("C")) else _KIND_USER
        subjects.append((kind, ident))
    return verb, subjects


def _describe(kind: str, ident: str) -> str:
    return f"<#{ident}>" if kind == _KIND_CHANNEL else f"<@{ident}>"


def handle_admin_command(text: str, *, actor: str | None) -> str | None:
    """Run an admin subcommand, returning the reply — or None if not one.

    Refuses when the caller is not an admin, and says who to ask rather than
    just "no": a permission error that does not name the way forward is a dead
    end for the person who hit it.
    """
    parsed = parse_admin_command(text)
    if parsed is None:
        return None
    verb, subjects = parsed

    if not is_admin(actor):
        if not admins():
            return (":lock: No admins are configured, so permissions cannot be "
                    "managed from Slack yet. Set `CAIRN_ADMINS` to your Slack "
                    "user id and restart — that is the only thing that lives in "
                    "config; everything else is managed here.")
        listed = ", ".join(f"<@{a}>" for a in sorted(admins()))
        return f":lock: Only {listed} can change permissions."

    if verb == "who":
        users, channels = sorted(allow_users()), sorted(allow_channels())
        if not users and not channels:
            return ("Cairn is open to everyone in this workspace — no "
                    "restrictions are set. `allow @someone` starts restricting.")
        lines = ["*Cairn is restricted to:*"]
        if users:
            lines.append("• people: " + ", ".join(f"<@{u}>" for u in users))
        if channels:
            lines.append("• channels: " + ", ".join(f"<#{c}>" for c in channels))
        else:
            lines.append("• channels: _any_")
        if not users:
            lines.append("• people: _anyone_ (in the channels above)")
        return "\n".join(lines)

    if not subjects:
        return (f"Who should I {verb}? Try `{verb} @someone` or `{verb} #channel`.")

    done, failed = [], []
    for kind, ident in subjects:
        ok = grant(kind, ident, by=actor) if verb == "allow" else revoke(kind, ident)
        (done if ok else failed).append(_describe(kind, ident))

    if verb == "allow":
        msg = f":white_check_mark: Allowed {', '.join(done)}." if done else ""
        first = ("\n_This is the first restriction — from now on only the people "
                 "and channels listed can use me._"
                 if done and len(allow_users() | allow_channels()) == len(done) else "")
        extra = f" Already absent: {', '.join(failed)}." if failed else ""
        return (msg + extra + first) or "Nothing changed."
    msg = f":white_check_mark: Removed {', '.join(done)}." if done else ""
    extra = f" Not on the list: {', '.join(failed)}." if failed else ""
    tail = ("\n_No restrictions remain — Cairn is open to everyone again._"
            if not (allow_users() | allow_channels()) else "")
    return (msg + extra + tail) or "Nothing changed."
