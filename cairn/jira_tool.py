"""Native Jira tool for Cairn — creates and searches issues via the Jira REST API.

Loaded as a first-class Strands tool so MiniMax M3 reliably calls it (MCP tools
are available but M3 doesn't consistently invoke them from persona instructions).
Credentials are read from JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN env vars.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from typing import Any

import requests
from strands import tool

from cairn import context as reqctx

log = logging.getLogger("cairn.jira")

# Pending tickets keyed by thread_ts. Populated by jira_stage_ticket, consumed by
# slack_app when the user confirms with "yes".
#
# Backed by SQLite rather than a process dict: a staged ticket waits on a human
# typing "yes", which can be minutes or hours, and a restart in between used to
# silently drop it (the user's "yes" then did nothing). Same mapping interface,
# so both call sites are unchanged.
_PENDING_TTL_S = 7 * 24 * 3600      # a "yes" this old is not a confirmation


class _PendingStore:
    """Thread-keyed staged tickets that survive a restart.

    Deliberately tiny and forgiving: any storage failure degrades to in-memory
    behaviour rather than breaking ticket staging.
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            path = os.environ.get("DATABASE_PATH", "./data/cairn.db")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("""CREATE TABLE IF NOT EXISTS pending_tickets (
                thread_ts TEXT PRIMARY KEY,
                payload   TEXT NOT NULL,
                created_at REAL NOT NULL)""")
            conn.commit()
            self._conn = conn
            return conn
        except Exception as err:  # noqa: BLE001
            log.warning("cairn.jira: pending store unavailable (%s); staying in-memory", err)
            return None

    def _expire(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM pending_tickets WHERE created_at < ?",
                     (time.time() - _PENDING_TTL_S,))

    def __setitem__(self, thread_ts: str, ticket: dict) -> None:
        conn = self._db()
        if conn is None:
            return
        try:
            self._expire(conn)
            conn.execute(
                "INSERT OR REPLACE INTO pending_tickets VALUES (?,?,?)",
                (thread_ts, json.dumps(ticket), time.time()))
            conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("cairn.jira: failed to persist staged ticket")

    def __contains__(self, thread_ts: object) -> bool:
        return self.get(str(thread_ts)) is not None

    def get(self, thread_ts: str) -> dict | None:
        conn = self._db()
        if conn is None:
            return None
        try:
            self._expire(conn)
            row = conn.execute(
                "SELECT payload FROM pending_tickets WHERE thread_ts=?",
                (thread_ts,)).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:  # noqa: BLE001
            log.exception("cairn.jira: failed to read staged ticket")
            return None

    def pop(self, thread_ts: str, default: Any = None) -> Any:
        ticket = self.get(thread_ts)
        if ticket is None:
            return default
        conn = self._db()
        if conn is not None:
            try:
                conn.execute("DELETE FROM pending_tickets WHERE thread_ts=?", (thread_ts,))
                conn.commit()
            except Exception:  # noqa: BLE001
                log.exception("cairn.jira: failed to clear staged ticket")
        return ticket


_pending = _PendingStore()


def default_project_key() -> str:
    """Read JIRA_PROJECT_KEY from env, fallback to KAN."""
    return os.environ.get("JIRA_PROJECT_KEY", "KAN")


def _resolve_slack_ids(text: str, bot_token: str) -> str:
    """Replace <@UXXXXX> and <#CXXXXX|name> in text with readable names."""
    if not bot_token:
        return text

    def replace_user(m: re.Match) -> str:
        uid = m.group(1)
        try:
            resp = requests.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": uid},
                timeout=5,
            )
            name = resp.json().get("user", {}).get("profile", {}).get("display_name") or \
                   resp.json().get("user", {}).get("real_name", uid)
            return f"@{name}"
        except Exception:
            return m.group(0)

    def replace_bare_channel(m: re.Match) -> str:
        # <#CXXXXX> with no name — look the channel name up via the API.
        cid = m.group(1)
        try:
            resp = requests.get(
                "https://slack.com/api/conversations.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"channel": cid},
                timeout=5,
            )
            name = resp.json().get("channel", {}).get("name")
            return f"#{name}" if name else "the channel"
        except Exception:
            return "the channel"

    text = re.sub(r"<@([A-Z0-9]+)>", replace_user, text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", lambda m: f"#{m.group(1)}", text)
    text = re.sub(r"<#([A-Z0-9]+)>", replace_bare_channel, text)  # bare, no name
    return text


def check_auth() -> tuple[bool, str]:
    """``(ok, message)`` for the configured Jira credentials.

    Worth a dedicated check because Jira fails *quietly* when a token is dead:
    `/project/search` and `/search/jql` answer **200 with empty results** to an
    unauthenticated caller, so Cairn reports "no duplicates" and "no projects" —
    answers that look normal. Only `/myself` distinguishes "nothing there" from
    "not logged in".
    """
    creds = _client()
    if creds is None:
        return False, "Jira not configured (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN)."
    base_url, auth = creds
    try:
        resp = requests.get(f"{base_url}/rest/api/3/myself", auth=auth, timeout=10)
    except Exception as err:  # noqa: BLE001
        return False, f"Jira unreachable: {err}"
    if resp.status_code == 401:
        return False, ("Jira credentials rejected (401). The token is expired or "
                       "revoked — note that searches still return 200 with empty "
                       "results, so this looks like 'no data' rather than an error. "
                       "New token: id.atlassian.com/manage-profile/security/api-tokens")
    if not resp.ok:
        return False, f"Jira auth check failed: HTTP {resp.status_code}"
    who = resp.json().get("emailAddress") or resp.json().get("displayName") or "?"
    return True, f"Jira authenticated as {who}"


def _dedup_check(summary: str) -> str | None:
    """Search Jira for an existing open issue with a similar summary.
    Returns the issue key if a duplicate is found, else None."""
    creds = _client()
    if creds is None:
        return None
    base_url, auth = creds
    project_key = default_project_key()
    # Use first 6 words of summary as search terms to avoid false negatives
    words = " ".join(summary.split()[:6])
    jql = f'project = {project_key} AND summary ~ "{words}" AND statusCategory != Done ORDER BY created DESC'
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/search/jql",
            auth=auth,
            params={"jql": jql, "maxResults": 1, "fields": "summary,status"},
            timeout=10,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if issues:
            return issues[0]["key"]
    except Exception as e:
        log.warning("cairn.jira: dedup check failed: %s", e)
    return None


# ── Semantic dedup ───────────────────────────────────────────────────────────
# The keyword check below misses the common case: three people report one bug in
# three wordings ("checkout throwing 500", "500 error on the checkout page",
# "payment step fails with a server error") and Jira's `~` operator matches none
# of them to each other. That is gap A from gaps.txt — "no de-duplication" — and
# it is the same problem the memory store already solved, so it uses the same
# tool: compare meaning, not words.
#
# Embeddings are local (fastembed, no API key, no quota), so this costs a Jira
# search and some CPU — no model call.
_JIRA_DEDUP_DISTANCE = 0.25
_JIRA_DEDUP_POOL = 50


def _dedup_enabled() -> bool:
    return os.environ.get("CAIRN_JIRA_DEDUP", "on").strip().lower() not in {
        "off", "0", "false", "no"}


def _dedup_distance() -> float:
    try:
        return max(0.0, float(os.environ.get("CAIRN_JIRA_DEDUP_DISTANCE",
                                             _JIRA_DEDUP_DISTANCE)))
    except ValueError:
        return _JIRA_DEDUP_DISTANCE


def _open_issues(limit: int = _JIRA_DEDUP_POOL) -> list[dict]:
    """Recent open issues in the default project — the pool to compare against."""
    creds = _client()
    if creds is None:
        return []
    base_url, auth = creds
    jql = (f"project = {default_project_key()} AND statusCategory != Done "
           f"ORDER BY created DESC")
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/search/jql", auth=auth,
            params={"jql": jql, "maxResults": limit, "fields": "summary"},
            timeout=10)
        resp.raise_for_status()
        return resp.json().get("issues", []) or []
    except Exception as err:  # noqa: BLE001
        log.warning("cairn.jira: could not list open issues: %s", err)
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 1.0 if not (na and nb) else dot / (na * nb)


def _similar_issue(summary: str) -> tuple[str, str] | None:
    """``(key, existing_summary)`` of an open issue that means the same thing.

    Never raises: a dedup failure must not stop a ticket being filed. Filing a
    duplicate is annoying; failing to file the bug at all is worse.
    """
    if not (_dedup_enabled() and summary.strip()):
        return None
    issues = _open_issues()
    if not issues:
        return None
    try:
        from cairn.embeddings import get_embedder  # noqa: PLC0415

        embedder = get_embedder()
        summaries = [i.get("fields", {}).get("summary", "") for i in issues]
        vectors = embedder.embed([summary, *summaries])
        target, rest = vectors[0], vectors[1:]
        best, best_distance = None, 1.0
        for issue, vec in zip(issues, rest):
            distance = 1.0 - _cosine(target, vec)
            if distance < best_distance:
                best, best_distance = issue, distance
        if best is not None and best_distance <= _dedup_distance():
            log.info("cairn.jira: %s looks like a duplicate (distance=%.3f)",
                     best.get("key"), best_distance)
            return best.get("key", ""), best.get("fields", {}).get("summary", "")
    except Exception as err:  # noqa: BLE001
        log.warning("cairn.jira: semantic dedup unavailable (%s); "
                    "falling back to keyword match", err)
    return None


# ── Citing the decision a ticket came from ───────────────────────────────────
# Jira is supporting cast now: a ticket should carry the decision it relates to,
# so whoever picks it up does not have to re-derive context that was settled in
# Slack weeks ago.
# How close a decision must be to a ticket before it is worth attaching. With a
# small store *something* always comes back from a search, and an unrelated
# decision on a ticket is worse than none: it reads as context and is not. A live
# run attached a dashboards decision to a CSV-export bug.
#
# Measured against the real store rather than guessed:
#     CSV-export bug   -> Postgres decision    0.472   (unrelated)
#     dashboards bug   -> dashboards decision  0.418   (relevant)
#     billing-db bug   -> Postgres decision    0.233   (relevant)
#
# The usable gap is 0.054 wide, so this will occasionally get it wrong. It sits
# just under the unrelated case rather than just over the relevant one, because
# the two failures are not equal: a missing citation is a smaller loss than a
# confidently wrong one on a ticket someone will act on.
_CITE_DISTANCE = 0.42


def _cite_distance() -> float:
    try:
        return max(0.0, float(os.environ.get("CAIRN_JIRA_CITE_DISTANCE", _CITE_DISTANCE)))
    except ValueError:
        return _CITE_DISTANCE


def _related_decision(text: str) -> str:
    """A one-line, sourced decision genuinely relevant to this ticket, or "".

    Reads the memory store directly — no model call, so filing a ticket never
    depends on quota — and checks relevance itself, because `search` ranks but
    does not threshold.
    """
    if os.environ.get("CAIRN_JIRA_CITE", "on").strip().lower() in {"off", "0", "false", "no"}:
        return ""
    try:
        import asyncio  # noqa: PLC0415

        from cairn.agent import _memory_store  # noqa: PLC0415
        from cairn.decisions import _cite  # noqa: PLC0415
        from cairn.embeddings import get_embedder  # noqa: PLC0415

        entries = asyncio.run(_memory_store().search(text))
        if not entries:
            return ""
        top = entries[0]
        content = (getattr(top, "content", "") or "").strip()
        if not content:
            return ""

        # Rank is not relevance. Measure it.
        embedder = get_embedder()
        a, b = embedder.embed([text, content])
        if 1.0 - _cosine(a, b) > _cite_distance():
            log.info("cairn.jira: no decision close enough to cite")
            return ""

        meta = getattr(top, "metadata", None) or {}
        cite = _cite(meta.get("author"), meta.get("channel"),
                     meta.get("date") or meta.get("created_at"))
        return f"\n\nRelated decision: {content}" + (f"\n— {cite}" if cite else "")
    except Exception:  # noqa: BLE001
        log.info("cairn.jira: no related decision available", exc_info=False)
        return ""


def find_duplicate(summary: str) -> str | None:
    """The key of an existing open issue this would duplicate, or None.

    **The** entry point for dedup — both callers must use it. There are two paths
    into ticket creation (the agent's `jira_stage_ticket`, and the deterministic
    bug shortcut in `slack_app`, which bypasses the agent because the model does
    not reliably call the tool), and wiring a better check into only one of them
    is exactly the bug this consolidates away: semantic dedup was live in the
    tool while Slack still used the keyword check, so a re-worded duplicate got
    staged anyway.

    Meaning first, keywords as fallback — the fallback still earns its place when
    embeddings are unavailable.
    """
    similar = _similar_issue(summary)
    if similar:
        return similar[0]
    return _dedup_check(summary)


def pop_pending(thread_ts: str) -> dict | None:
    """Return and remove a staged ticket for this thread, or None."""
    return _pending.pop(thread_ts, None)


def has_pending(thread_ts: str) -> bool:
    return thread_ts in _pending


def _create_issue(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Bug",
    priority: str = "Medium",
    assignee_email: str = "",
    slack_mention: str = "",
    assignee_name: str = "",
) -> str:
    """Shared core for filing a Jira issue: resolve the project key + assignee,
    clean Slack ids out of the description, POST it, and format the reply. Both
    `jira_create_issue` (agent tool) and `_create_jira_issue_direct` (staged-ticket
    confirm) delegate here so the creation logic lives in exactly one place."""
    creds = _client()
    if creds is None:
        return "Jira is not configured (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN missing)."
    base_url, auth = creds

    project_key, proj_note = resolve_project_key(project_key)
    requested_assignee = assignee_name or slack_mention or assignee_email
    assignee_email = _resolve_assignee_email(assignee_email, slack_mention, assignee_name)
    # Attach the related decision BEFORE resolving Slack ids, so the citation's
    # <@U…> / <#C…> render as names too — a citation nobody can read is not one.
    description += _related_decision(f"{summary} {description}")
    description = _resolve_slack_ids(description, os.environ.get("SLACK_BOT_TOKEN", ""))

    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary,
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
        },
        "issuetype": {"name": issue_type},
        "priority": {"name": priority},
    }
    if assignee_email:
        account_id = _resolve_assignee(base_url, auth, assignee_email)
        if account_id:
            fields["assignee"] = {"accountId": account_id}
        else:
            log.warning("cairn.jira: assignee %s not found in Jira, leaving unassigned", assignee_email)

    try:
        resp = requests.post(
            f"{base_url}/rest/api/3/issue",
            auth=auth,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"fields": fields}),
            timeout=10,
        )
        resp.raise_for_status()
        key = resp.json()["key"]
        url = f"{base_url}/browse/{key}"
        assignee_note, unassigned_note = _assignee_notes(fields, assignee_email, requested_assignee)
        log.info("cairn.jira: created issue %s%s priority=%s", key, assignee_note, priority)
        return (
            f"Created {issue_type} *{key}* · Priority: *{priority}*{assignee_note}: "
            f"{url}{unassigned_note}{_note_suffix(proj_note)}"
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        log.error("cairn.jira: create failed: %s — %s", e, body)
        return f"Failed to create Jira issue: {e} — {body}"
    except Exception as e:
        log.error("cairn.jira: unexpected error: %s", e)
        return f"Failed to create Jira issue: {e}"


def _create_jira_issue_direct(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Bug",
    priority: str = "Medium",
    assignee_email: str = "",
    slack_mention: str = "",
    assignee_name: str = "",
) -> str:
    """Non-tool entry point called from slack_app.py when the user confirms a
    staged ticket. Thin wrapper over the shared `_create_issue`."""
    return _create_issue(
        project_key, summary, description, issue_type, priority,
        assignee_email, slack_mention, assignee_name,
    )


def _adf(text: str) -> dict:
    """Wrap plain text as an Atlassian Document Format doc (Jira description field)."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _update_issue_direct(
    issue_key: str,
    *,
    priority: str = "",
    summary: str = "",
    description: str = "",
    append_description: str = "",
    assignee_name: str = "",
    slack_mention: str = "",
) -> str:
    """Update any of priority/summary/description/assignee on an existing issue.
    Deterministic path from slack_app.py (M3 won't reliably call an update tool)."""
    creds = _client()
    if creds is None:
        return "Jira is not configured (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN missing)."
    base_url, auth = creds

    fields: dict[str, Any] = {}
    changed: list[str] = []
    if priority:
        fields["priority"] = {"name": priority}; changed.append(f"priority → *{priority}*")
    if summary:
        fields["summary"] = summary; changed.append(f"title → *{summary}*")
    if append_description:
        # Fetch current description text and append.
        try:
            cur = requests.get(f"{base_url}/rest/api/3/issue/{issue_key}?fields=description",
                               auth=auth, timeout=10).json()
            existing = _adf_to_text(cur.get("fields", {}).get("description"))
            description = f"{existing}\n\n{append_description}".strip()
        except Exception:
            description = append_description
    if description:
        fields["description"] = _adf(_resolve_slack_ids(description, os.environ.get("SLACK_BOT_TOKEN", "")))
        changed.append("description")

    assignee_note = ""
    if assignee_name or slack_mention:
        email = _resolve_assignee_email("", slack_mention, assignee_name)
        if email:
            account_id = _resolve_assignee(base_url, auth, email)
            if account_id:
                fields["assignee"] = {"accountId": account_id}
                assignee_note = f", assigned to {email}"
                changed.append(f"assignee → *{email}*")
            else:
                assignee_note = f"\n_Couldn't match assignee to a Jira account._"
        else:
            assignee_note = f"\n_Couldn't resolve '{assignee_name or slack_mention}' to a member._"

    if not fields:
        return f"Nothing to update on {issue_key} — tell me what to change (priority/title/description/assignee)."
    try:
        resp = requests.put(
            f"{base_url}/rest/api/3/issue/{issue_key}",
            auth=auth, headers={"Content-Type": "application/json"},
            data=json.dumps({"fields": fields}), timeout=10,
        )
        resp.raise_for_status()
        log.info("cairn.jira: updated %s (%s)", issue_key, ", ".join(changed))
        return f"Updated *{issue_key}*: {', '.join(changed)}{assignee_note} — {base_url}/browse/{issue_key}"
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        log.error("cairn.jira: update failed: %s — %s", e, body)
        return f"Couldn't update {issue_key}: {e} — {body}"
    except Exception as e:
        log.error("cairn.jira: unexpected error updating issue: %s", e)
        return f"Couldn't update {issue_key}: {e}"


def _adf_to_text(adf: Any) -> str:
    """Flatten an Atlassian Document Format description back to plain text (best effort)."""
    if not isinstance(adf, dict):
        return ""
    out: list[str] = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            for c in node.get("content", []) or []:
                walk(c)
    walk(adf)
    return " ".join(out).strip()


def _assignee_notes(fields: dict, assignee_email: str, requested: str) -> tuple[str, str]:
    """(inline "assigned to …" note, trailing "left unassigned" flag). The flag
    fires only when an assignee was requested but couldn't be resolved/matched."""
    if fields.get("assignee"):
        return f", assigned to {assignee_email}", ""
    if requested:
        return "", f"\n_Couldn't match assignee '{requested}' to a workspace member — left unassigned._"
    return "", ""


def _note_suffix(note: str | None) -> str:
    return f"\n_{note}_" if note else ""


def _client() -> tuple[str, requests.auth.HTTPBasicAuth] | None:
    url = os.environ.get("JIRA_URL", "").rstrip("/")
    user = os.environ.get("JIRA_USERNAME", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not (url and user and token):
        return None
    return url, requests.auth.HTTPBasicAuth(user, token)


def _slack_email_from_user_id(user_id: str) -> str | None:
    """Look up a Slack user's email from their user ID using the bot token."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {token}"},
            params={"user": user_id},
            timeout=10,
        )
        data = resp.json()
        return data.get("user", {}).get("profile", {}).get("email")
    except Exception as e:
        log.warning("cairn.jira: slack users.info failed for %s: %s", user_id, e)
        return None


def _extract_slack_mention(text: str) -> str | None:
    """Extract the first Slack user ID from a message like <@U0123ABCDEF>."""
    m = re.search(r"<@([A-Z0-9]+)>", text)
    return m.group(1) if m else None


def _resolve_assignee(base_url: str, auth: requests.auth.HTTPBasicAuth, email: str) -> str | None:
    """Look up a Jira account ID by email address. Returns None if not found."""
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/user/search",
            auth=auth,
            params={"query": email},
            timeout=10,
        )
        resp.raise_for_status()
        users = resp.json()
        if users:
            log.info("cairn.jira: resolved assignee %s → %s", email, users[0]["accountId"])
            return users[0]["accountId"]
    except Exception as e:
        log.warning("cairn.jira: could not resolve assignee %s: %s", email, e)
    return None


# ── project-key resolution (match a named project to a real Jira key) ─────────
_project_cache: dict[str, Any] = {}  # {"projects": [(key, name)], "ts": epoch}
_PROJECT_TTL = 300  # seconds


def _list_projects() -> list[tuple[str, str]]:
    """(key, name) for every Jira project the token can see, cached ~5 min."""
    creds = _client()
    if creds is None:
        return []
    now = time.time()
    if _project_cache.get("projects") is not None and now - _project_cache.get("ts", 0) < _PROJECT_TTL:
        return _project_cache["projects"]
    base_url, auth = creds
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/project/search",
            auth=auth,
            params={"maxResults": 100},
            timeout=10,
        )
        resp.raise_for_status()
        projects = [(p["key"], p.get("name", "")) for p in resp.json().get("values", [])]
        _project_cache["projects"] = projects
        _project_cache["ts"] = now
        return projects
    except Exception as e:
        log.warning("cairn.jira: project list failed: %s", e)
        return _project_cache.get("projects") or []


def resolve_project_key(requested: str | None) -> tuple[str, str | None]:
    """Map a requested project (key OR name, e.g. "BAN") to a real Jira key.

    Returns ``(key, note)``. ``note`` is a short human string only when we had to
    fall back to the default because the request didn't match a real project — so
    a mistyped/nonexistent project files under the default instead of 400ing. If
    the project list can't be fetched we trust the caller's input rather than block.
    """
    default = default_project_key()
    req = (requested or "").strip()
    if not req:
        return default, None
    projects = _list_projects()
    if not projects:
        return req, None  # can't verify — trust input, don't block the file
    low = req.lower()
    for key, _name in projects:
        if key.lower() == low:
            return key, None
    for key, name in projects:
        if name and (name.lower() == low or low in name.lower() or name.lower() in low):
            return key, None
    keys = ", ".join(k for k, _ in projects)
    return default, f"Project '{req}' not found (available: {keys}); filed under {default}."


# ── assignee-by-name resolution (Slack member list → email) ───────────────────
_users_cache: dict[str, Any] = {}  # {"users": [member dicts], "ts": epoch}
_USERS_TTL = 300


def _slack_users() -> list[dict]:
    """All workspace members (paginated), cached ~5 min. Needs users:read."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return []
    now = time.time()
    if _users_cache.get("users") is not None and now - _users_cache.get("ts", 0) < _USERS_TTL:
        return _users_cache["users"]
    users: list[dict] = []
    cursor: str | None = None
    try:
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                "https://slack.com/api/users.list",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=10,
            )
            data = resp.json()
            users += data.get("members", []) or []
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        _users_cache["users"] = users
        _users_cache["ts"] = now
    except Exception as e:
        log.warning("cairn.jira: users.list failed: %s", e)
        return _users_cache.get("users") or []
    return users


def _slack_email_from_name(name: str) -> str | None:
    """Resolve a bare name (e.g. "Jyoti") to a workspace member's email.

    This is what lets the agent assign "I want Jyoti to fix X" without a follow-up:
    match ``name`` against each member's username / real name / display name. Prefer
    an exact full-name match, then a unique first-name match. Returns None when the
    match is ambiguous or absent (caller then files unassigned) — never guesses."""
    name = (name or "").strip().lstrip("@")
    if not name:
        return None
    low = name.lower()
    exact: list[str] = []
    partial: list[str] = []
    for u in _slack_users():
        if u.get("deleted") or u.get("is_bot"):
            continue
        prof = u.get("profile") or {}
        email = prof.get("email")
        if not email:
            continue
        low_names = [
            n.lower() for n in (u.get("name"), u.get("real_name"),
                                prof.get("real_name"), prof.get("display_name")) if n
        ]
        if low in low_names:
            exact.append(email)
        elif any(low == n.split()[0] or n.startswith(low + " ") for n in low_names if n):
            partial.append(email)
    for bucket in (exact, partial):
        uniq = list(dict.fromkeys(bucket))
        if len(uniq) == 1:
            return uniq[0]
    return None


def _resolve_assignee_email(
    assignee_email: str = "",
    slack_mention: str = "",
    assignee_name: str = "",
) -> str:
    """Best-effort assignee email from the three signals.

    Priority is **name first**: a bare name the user stated ("assign to Jyoti") is
    the most reliable signal of *who should own this*. The model routinely echoes
    the **requester's** ``<@id>`` into ``slack_mention`` (and sometimes their email
    into ``assignee_email``) from the context line — that must NOT override a named
    assignee. So: resolvable ``assignee_name`` → explicit ``assignee_email`` →
    ``slack_mention``. Empty string if none resolve."""
    if assignee_name:
        email = _slack_email_from_name(assignee_name)
        if email:
            log.info("cairn.jira: resolved assignee name %r → %s", assignee_name, email)
            return email
    if assignee_email:
        return assignee_email
    if slack_mention:
        uid = _extract_slack_mention(slack_mention)
        if uid:
            email = _slack_email_from_user_id(uid)
            if email:
                return email
    return ""


@tool
def jira_stage_ticket(
    summary: str,
    description: str,
    issue_type: str = "Bug",
    priority: str = "Medium",
    slack_mention: str = "",
    assignee_name: str = "",
    project: str = "",
) -> str:
    """Stage a Jira ticket for confirmation. Call this when a bug is reported —
    it saves the ticket details and returns a confirmation message for the user.
    The ticket is NOT created yet; it is created when the user replies 'yes'.

    Args:
        summary: Short title for the issue
        description: Full description of the bug or task
        issue_type: Bug or Task (default Bug)
        priority: Highest / High / Medium / Low (default Medium)
        slack_mention: Raw Slack mention like <@U0123ABCDEF> if someone was tagged
        assignee_name: Bare display name of the assignee if only a name was given
            (e.g. "Jyoti") — resolved to an email at create time, no follow-up
        project: Project the user named (key or name); resolved at create time,
            falls back to the default if it doesn't match a real project
    """
    state = reqctx.current()
    thread_ts = state.thread_ts if state else None
    if not thread_ts:
        return "Could not stage ticket: no thread context."

    # Dedup regardless of path (agent tool or deterministic staging both land here-ish):
    # Meaning first, keywords as the fallback: three people reporting one bug in
    # three wordings is the case the keyword search cannot see.
    existing = find_duplicate(summary)
    if existing:
        base_url = _client()[0] if _client() else ""
        link = f": {base_url}/browse/{existing}" if base_url else ""
        return (f":mag: Looks like this may already be tracked: *{existing}*{link} — "
                f"check Jira before filing a new one.")

    _pending[thread_ts] = {
        "summary": summary,
        "description": description,
        "issue_type": issue_type,
        "priority": priority,
        "slack_mention": slack_mention,
        "assignee_name": assignee_name,
        "project": project,
    }
    log.info("cairn.jira: staged ticket for thread %s: %s", thread_ts, summary)

    assignee_display = slack_mention or assignee_name
    assignee_line = f"\n• *Assignee:* {assignee_display}" if assignee_display else ""
    project_key, _ = resolve_project_key(project)
    return (
        f"Here's what I'll file:\n"
        f"• *Title:* {summary}\n"
        f"• *Project:* {project_key} | *Type:* {issue_type} | *Priority:* {priority}{assignee_line}\n\n"
        f"Reply *yes* (mentioning me) in this thread to confirm and I'll create it in Jira."
    )


@tool
def jira_create_issue(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Bug",
    priority: str = "Medium",
    assignee_email: str = "",
    slack_mention: str = "",
    assignee_name: str = "",
) -> str:
    """Create a Jira issue and return the issue key and URL.

    Use this when the user confirms they want a bug or task filed in Jira. You do
    NOT need to ask follow-up questions to fill these in — the tool resolves them:

    - project_key: pass the project the user named (a key like "KAN" or a plain
      name like "BAN"). The tool matches it to a real Jira project and falls back
      to the default if there's no match, so it never fails on an unknown project.
    - Assignee: when a person is NAMED (e.g. "assign to Jyoti"), pass assignee_name
      ONLY — the tool resolves their email from the Slack member list. Pass
      slack_mention ONLY if the assignee themselves was @mentioned; never pass the
      requester's own mention/email here (that would misassign the ticket to them).
      If nothing matches, it's filed unassigned and the reply says so. Never ask
      for an email.

    Args:
        project_key: Jira project key or name (e.g. KAN, or "BAN")
        summary: Short title for the issue
        description: Full description of the bug or task
        issue_type: Bug or Task (default Bug)
        priority: Highest / High / Medium / Low (default Medium)
        assignee_email: Email of the person to assign the ticket to (optional)
        slack_mention: Raw Slack mention string like <@U0123ABCDEF> (optional)
        assignee_name: Bare display name of the assignee, e.g. "Jyoti" (optional)
    """
    return _create_issue(
        project_key, summary, description, issue_type, priority,
        assignee_email, slack_mention, assignee_name,
    )


@tool
def jira_search_issues(project_key: str, query: str) -> str:
    """Search or list Jira issues in a project.

    Args:
        project_key: Jira project key, e.g. KAN
        query: Text to search for, OR a filter keyword:
               "all" / "recent" / "open" → list all open issues ordered by date
               "bugs" / "bug" → list all open Bug issues
               "tasks" → list all open Task issues
               anything else → full-text search across summaries and descriptions
    """
    creds = _client()
    if creds is None:
        return "Jira is not configured."

    base_url, auth = creds
    q = query.strip().lower()

    if q in {"all", "recent", "open", ""}:
        jql = f"project = {project_key} AND statusCategory != Done ORDER BY created DESC"
    elif q in {"bug", "bugs"}:
        jql = f"project = {project_key} AND issuetype = Bug AND statusCategory != Done ORDER BY created DESC"
    elif q in {"task", "tasks"}:
        jql = f"project = {project_key} AND issuetype = Task AND statusCategory != Done ORDER BY created DESC"
    else:
        jql = f'project = {project_key} AND text ~ "{query}" ORDER BY created DESC'

    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/search/jql",
            auth=auth,
            params={"jql": jql, "maxResults": 10, "fields": "summary,status,priority,assignee,issuetype"},
            timeout=10,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if not issues:
            return f"No issues found for '{query}' in {project_key}."
        lines = []
        for i in issues:
            key = i["key"]
            summary = i["fields"]["summary"]
            status = i["fields"]["status"]["name"]
            assignee = (i["fields"].get("assignee") or {}).get("displayName", "unassigned")
            itype = i["fields"].get("issuetype", {}).get("name", "")
            url = f"{base_url}/browse/{key}"
            lines.append(f"• {key} [{itype}/{status}] @{assignee}: {summary} — {url}")
        return "\n".join(lines)
    except Exception as e:
        log.error("cairn.jira: search failed: %s", e)
        return f"Failed to search Jira: {e}"
