"""Hybrid (lexical + semantic) MemoryStore over libSQL / Turso — Agentic RAG.

Recall fuses two retrievers over one libSQL/Turso database:

  • **Lexical** — FTS5 `MATCH` ranked by BM25 (exact words, names, ids, numbers).
  • **Semantic** — native vector search (`F32_BLOB` + `libsql_vector_idx` +
    `vector_top_k`) over text embeddings (meaning, paraphrase, synonyms).

…then merges them with **Reciprocal Rank Fusion (RRF)** so a memory that ranks
well on *either* signal surfaces, and one that ranks well on *both* wins. This is
the standard hybrid-RAG retrieval pattern and beats either signal alone.

Why libSQL/Turso: it *is* SQLite, so the content, provenance columns, FTS5 index,
and vectors all live in one local file (no server), queried transactionally, and
can later sync to Turso cloud. It stores/searches vectors but does **not** create
them — embeddings come from `cairn.embeddings` (default `fastembed`, local, no API
key; `minimax`/`openai` optional).

Same Strands `MemoryStore` contract, same provenance stamping, and same
`MemoryEntry` shape as the FTS5-only store on `main`, so the agent, injection
format, and tools are unchanged — only recall quality differs.

Enable:
    pip install "cairn[vector]"
    CAIRN_MEMORY_BACKEND=hybrid          # (libsql/turso/vector are aliases)
    CAIRN_EMBEDDINGS=fastembed           # local, no key. See cairn/embeddings.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strands.memory.types import MemoryEntry, SearchOptions

from cairn import context as reqctx
from cairn.redact import redact
from cairn.embeddings import Embedder, get_embedder
from cairn.storage import _fts_match  # reuse the injection-safe query sanitizer

log = logging.getLogger("cairn.vector")

DEFAULT_VECTOR_DB_PATH = "./data/cairn_vec.db"
_PROVENANCE_COLS = ("kind", "author", "channel", "team", "source", "thread_ts")
_RRF_K = 60  # Reciprocal Rank Fusion damping constant (standard default)


def _vector_literal(vec: list[float]) -> str:
    """Render an embedding as the text form vector32() accepts: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# Position of each scopeable column inside the row tuple `_fetch` returns
# (content, metadata, kind, author, channel, team, source, thread_ts, created_at).
_SCOPE_IDX = {"channel": 4, "team": 5}


def _row_in_scope(row: tuple, scope: tuple[str, str]) -> bool:
    """True if a fetched row satisfies the active (column, value) scope. Used to
    post-filter semantic hits, which `vector_top_k` can't scope in SQL."""
    col, val = scope
    return row[_SCOPE_IDX[col]] == val


# How close is "the same thing"? Measured twice — the second time properly.
#
# First pass, against a store holding three memories:
#     same fact re-phrased 0.12-0.15, answer changed 0.179, unrelated 0.49-0.60.
# That looked like a 0.054-wide gap between the first two. It was an artefact of
# the sample.
#
# Re-measured against a 44-decision corpus with real topic overlap:
#     same decision, reworded ....... 0.176 - 0.266
#     replaced (answer changed) ..... 0.236 - 0.332   <- OVERLAPS the above
#     independent, same topic ....... 0.460 - 0.569
#     unrelated ..................... median 0.531
#
# The first two bands do not merely sit close, they overlap: distance cannot
# distinguish "the same decision said differently" from "the decision changed",
# and no threshold will. What separates cleanly is *related vs not* — a 0.128
# gap between 0.332 and 0.460 — which is exactly what distance is used for here.
# It decides how much to ask; the model decides what the relation is.
#
# On that corpus the bands below sent 6/6 genuine relations to the classifier and
# 0/8 independent same-topic pairs, at a cost of ~6% of unrelated pairs entering
# the band and coming back "independent".
#
# _DEDUP_HARD stays deliberately low. It is the "collapse without asking" band,
# and since duplicates start at 0.176 while replacements start at 0.236, raising
# it far enough to be useful would put it inside the overlap — where a free
# collapse would silently merge a decision with the one that replaced it.
_DEDUP_HARD = 0.08
_RELATED_MAX = 0.45
# How many live neighbours a new memory is compared against (decisions chain).
_NEIGHBOURS = 4


def _dedup_enabled() -> bool:
    return os.environ.get("CAIRN_DEDUP", "on").strip().lower() not in {"off", "0", "false", "no"}


def _supersede_enabled() -> bool:
    return os.environ.get("CAIRN_SUPERSEDE", "on").strip().lower() not in {"off", "0", "false", "no"}


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _dedup_distance() -> float:
    return _float_env("CAIRN_DEDUP_DISTANCE", _DEDUP_HARD)


def _related_distance() -> float:
    return _float_env("CAIRN_RELATED_DISTANCE", _RELATED_MAX)


def _relation_key(new_content: str, old_content: str) -> str:
    """Stable key for one *pair of texts*. Change either text and it is a
    different question, so a new verdict is fetched."""
    return hashlib.sha256(
        f"{old_content}\x00{new_content}".encode("utf-8")).hexdigest()


def _cached_relation(conn, key: str) -> str | None:
    try:
        row = conn.execute("SELECT relation FROM memory_relations WHERE key=?",
                           (key,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return str(row[0]) if row else None


def _cache_relation(conn, key: str, relation: str) -> None:
    try:
        conn.execute("INSERT OR REPLACE INTO memory_relations VALUES (?,?,unixepoch())",
                     (key, relation))
        conn.commit()
    except Exception:  # noqa: BLE001
        log.debug("could not cache relation verdict")


_RELATIONS = {
    "duplicate": "The same fact in different words; nothing new.",
    "replaces": "Same question, the answer changed (a limit moved, a tool swapped, "
                "a policy reversed). The NEW one is current.",
    "independent": "Different subjects, or complementary facts that can both be "
                   "true at once.",
}


def _jev_min_confidence() -> float:
    """Below this, a Jev "duplicate"/"replaces" is treated as "independent".

    Both of those verdicts remove a row from recall, and wrongly retiring a live
    decision is invisible and unrecoverable — a high-stakes action. TypeSafe's
    confidence-routing pattern acts on high-stakes choices only above 0.85, so
    that is the default. Deliberately not tuned to our eval sets.
    """
    return _float_env("CAIRN_JEV_MIN_CONFIDENCE", 0.85)


_JEV_QUESTION = "How does the NEW decision relate to the OLD one?"

# Jev reads text literally (TypeSafe documents it: "scoping words, negations,
# and implied conditions at face value"). Asked only the question above, it
# retired a rule on reading a one-off ("for this week's launch, deploy on
# Saturday") and an exception on reading a restated general rule ("daily
# standup is at 10:00" vs "standups are async on Fridays"), at up to 1.00
# confidence — so no confidence bar could catch it. These ask about the exact
# condition directly, in the same call, as their guidance suggests.
_JEV_CHECKS = {
    "temporary": {
        "instructions": "The NEW decision applies only for a limited time or a single event.",
        "criteria": {
            "true": "NEW is limited to a period or an occasion, after which the usual "
                    "rule applies again, for example 'during the office move, "
                    "deliveries go to reception' or 'until the audit ends, exports "
                    "need two approvals'.",
            "false": "NEW is meant to hold from now on with no end, even if it names "
                     "a day or a time as part of the rule, for example 'the office "
                     "opens at 9:00' or 'the newsletter goes out every Monday'.",
        },
    },
    "same_case": {
        "instructions": "The NEW decision governs exactly the same thing as the OLD "
                        "decision, so following NEW means OLD can no longer be followed.",
        "criteria": {
            "true": "Same thing, same people, same situation, different answer, or NEW "
                    "explicitly cancels OLD, for example 'the office opens at 9:00' "
                    "then 'the office opens at 8:30'.",
            "false": "OLD covers a narrower situation that NEW does not mention (one "
                     "day, one team, one customer group, one event, one environment), "
                     "or they govern different aspects, so both can be followed "
                     "together, for example 'the sales team works remotely on "
                     "Mondays' then 'the office opens at 9:00'.",
        },
    },
}

# TypeSafe's yes/no guidance: "Raise it when acting on a false yes is expensive…
# Lower it when missing a true yes is expensive", with yes above 0.8, no below
# 0.2 and the band between sent to a human. Both checks guard a retirement, so
# only a clear answer lets it through; the unsure band keeps both for now and
# is where a "does this replace …?" question to a person belongs later.
_CLEAR_YES = 0.8
_CLEAR_NO = 0.2


def _jev_checks_enabled() -> bool:
    return os.environ.get("CAIRN_JEV_CHECKS", "on").strip().lower() not in {"off", "0", "false", "no"}


def _ask_jev(new_content: str, old_content: str):
    from cairn import jev  # noqa: PLC0415
    return jev.choice(
        {"old_decision": old_content, "new_decision": new_content},
        _JEV_QUESTION, _RELATIONS,
        checks=_JEV_CHECKS if _jev_checks_enabled() else None,
    )


def _jev_decide(verdict, bar: float | None = None) -> str:
    """Turn Jev's answers into what Cairn does. Every rule here only ever moves
    toward "independent" (keep both): a wrongly kept row is visible and
    harmless, a wrongly retired decision is neither."""
    bar = _jev_min_confidence() if bar is None else bar
    if verdict.choice != "independent" and verdict.confidence < bar:
        log.info("jev said %s at confidence %.2f; keeping both",
                 verdict.choice, verdict.confidence)
        return "independent"
    if verdict.choice == "replaces":
        # A check Jev did not answer is unknown, not a veto.
        if verdict.checks.get("temporary", 0.0) >= _CLEAR_NO:
            log.info("jev said replaces, but NEW is not clearly permanent "
                     "(temporary=%.2f); keeping both", verdict.checks["temporary"])
            return "independent"
        if verdict.checks.get("same_case", 1.0) < _CLEAR_YES:
            log.info("jev said replaces, but NEW is not clearly the same case "
                     "(same_case=%.2f); keeping both", verdict.checks["same_case"])
            return "independent"
    return verdict.choice


def _classify(new_content: str, old_content: str) -> str:
    """How does the new statement relate to the closest existing one?

    Returns ``"duplicate"`` (same fact, keep one), ``"replaces"`` (same question,
    the team changed its mind) or ``"independent"`` (both can be true).

    Conservative on failure: an unparseable or failed call returns
    ``"independent"``, so the worst case is a slightly noisier store rather than
    a live decision wrongly retired — a retired decision stops being answerable
    at all, which is much harder to notice.

    With ``CAIRN_CLASSIFIER=jev`` this is a typed choice with a confidence, and
    an unsure destructive verdict keeps both; if Jev is unreachable it falls
    through to the LLM, never to a guess.
    """
    from cairn import jev  # noqa: PLC0415

    if jev.enabled():
        verdict = _ask_jev(new_content, old_content)
        if verdict is not None:
            return _jev_decide(verdict)
        log.info("jev unavailable; classifying with the LLM")
    return _classify_llm(new_content, old_content)


def _classify_llm(new_content: str, old_content: str) -> str:
    return _llm_relation(new_content, old_content) or "independent"


def _llm_relation(new_content: str, old_content: str) -> str | None:
    """The LLM's verdict, or None when the call failed or answered nothing
    usable. Kept apart from the "independent" fallback so a measurement can
    tell a failed call from a real answer."""
    from cairn.llm import extract_json  # noqa: PLC0415 (import cycle at module load)

    data = extract_json(
        "Two statements about a team's decisions.\n\n"
        'Return ONLY: {"relation": "duplicate" | "replaces" | "independent"}\n\n'
        "duplicate   → the same fact in different words; nothing new\n"
        "replaces    → same question, the answer changed (a limit moved, a tool "
        "swapped, a policy reversed). The NEW one is current.\n"
        "independent → different subjects, or complementary facts that can both "
        "be true at once\n\n"
        f"OLD: {old_content}\nNEW: {new_content}",
        max_tokens=64,
    )
    relation = (data or {}).get("relation")
    return relation if relation in {"duplicate", "replaces", "independent"} else None


class HybridMemoryStore:
    """Hybrid lexical+semantic long-term memory over libSQL/Turso."""

    name = "loop_memory"
    description = (
        "Cairn's hybrid (full-text + semantic) memory of crucial facts, decisions, "
        "preferences, and episodes — each stamped with who said it, where, and when."
    )
    max_search_results = 5
    writable = True
    extraction = False

    def __init__(self, db_path: str | None = None, embedder: Embedder | None = None) -> None:
        try:
            import libsql_experimental as libsql  # noqa: PLC0415 (optional dep)
        except ImportError as err:  # pragma: no cover
            raise RuntimeError(
                'libSQL not installed. Run: pip install "cairn[vector]" '
                "(or set CAIRN_MEMORY_BACKEND=sqlite for the FTS5-only store)."
            ) from err

        self.db_path = db_path or os.environ.get("CAIRN_VECTOR_DB_PATH", DEFAULT_VECTOR_DB_PATH)
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or get_embedder()
        self.dim = self.embedder.dim
        self._lock = threading.Lock()

        sync_url = os.environ.get("TURSO_DATABASE_URL")
        auth = os.environ.get("TURSO_AUTH_TOKEN")
        if sync_url:  # embedded replica synced to Turso cloud
            self._conn = libsql.connect(self.db_path, sync_url=sync_url, auth_token=auth)
            self._conn.sync()
        else:  # purely local embedded file (default)
            self._conn = libsql.connect(self.db_path)

        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS memory_entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT NOT NULL,
                metadata   TEXT,
                kind TEXT, author TEXT, channel TEXT, team TEXT, source TEXT, thread_ts TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                embedding  F32_BLOB({self.dim})
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS memory_vec_idx "
            "ON memory_entries(libsql_vector_idx(embedding))"
        )
        # Supersession: a decision that replaced this one. Additive + idempotent
        # so existing databases migrate in place.
        # Cached relation verdicts, keyed by the pair of texts. The judgement is
        # a model call and so non-deterministic — compacting the live store twice
        # gave opposite verdicts on the same pair. Caching stops re-rolling the
        # dice on a question whose inputs have not changed. It also *locks in* a
        # verdict, including a wrong one, which is why `recheck=True` exists.
        self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_relations (
            key      TEXT PRIMARY KEY,
            relation TEXT NOT NULL,
            decided_at INTEGER NOT NULL)""")
        for col, decl in (("superseded_by", "INTEGER"), ("superseded_at", "INTEGER")):
            try:
                self._conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {col} {decl}")
            except Exception:  # noqa: BLE001 - already present
                pass
        self._init_fts()
        self._conn.commit()
        log.info("hybrid memory ready (embedder=%s dim=%d db=%s)", self.embedder.name, self.dim, self.db_path)

    def _init_fts(self) -> None:
        existed = bool(
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
            ).fetchone()
        )
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
            "USING fts5(content, content='memory_entries', content_rowid='id')"
        )
        self._conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memory_entries_ai
            AFTER INSERT ON memory_entries BEGIN
                INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
            END
            """
        )
        if not existed:
            self._conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")

    # --- read --------------------------------------------------------------
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = int((options or {}).get("max_search_results") or self.max_search_results)
        pool = max(limit * 4, 20)
        scope = reqctx.memory_scope()  # channel/team isolation, or None for global
        with self._lock:
            lexical = self._lexical_ids(query, pool, scope)  # scoped in SQL via join
            semantic = self._semantic_ids(query, pool)        # over-fetched; scoped below
            fused = _rrf_fuse(lexical, semantic)
            if not fused:
                return []
            rows = self._fetch(fused)
        # Keep RRF order; drop semantic hits out of scope; cap at limit.
        out: list[MemoryEntry] = []
        for i in fused:
            row = rows.get(i)
            if row is None or (scope and not _row_in_scope(row, scope)):
                continue
            out.append(self._row_to_entry(row))
            if len(out) >= limit:
                break
        return out

    def _lexical_ids(self, query: str, pool: int, scope: tuple[str, str] | None = None) -> list[int]:
        match = _fts_match(query)
        if not match:
            return []
        scope_sql, scope_params = ("", ())
        if scope:
            scope_sql = f" AND m.{scope[0]} = ?"
            scope_params = (scope[1],)
        try:
            rows = self._conn.execute(
                "SELECT f.rowid FROM memory_fts f JOIN memory_entries m ON m.id = f.rowid "
                f"WHERE memory_fts MATCH ?{scope_sql} ORDER BY bm25(memory_fts) LIMIT ?",
                (match, *scope_params, pool),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as err:  # noqa: BLE001
            log.warning("lexical search failed: %s", err)
            return []

    def _semantic_ids(self, query: str, pool: int) -> list[int]:
        try:
            qvec = _vector_literal(self.embedder.embed_one(query))
            rows = self._conn.execute(
                "SELECT id FROM vector_top_k('memory_vec_idx', vector32(?), ?)",
                (qvec, pool),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as err:  # noqa: BLE001
            log.warning("semantic search failed: %s", err)
            return []

    def _fetch(self, ids: list[int]) -> dict[int, tuple]:
        """Load rows for recall. Both retrieval legs funnel through here, which is
        why superseded rows are filtered here and nowhere else.

        A replaced decision must never come back as current truth: "cap review
        turnaround at 24 hours" and "moved it to 4 hours" are equally well-sourced
        and equally confident, and serving the stale one *with a citation* is the
        worst failure this product has. The row is kept (auditable, and
        `superseded_by` records what replaced it) — it just stops being an answer.
        """
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"""
            SELECT id, content, metadata, kind, author, channel, team, source, thread_ts, created_at
            FROM memory_entries
            WHERE id IN ({placeholders}) AND superseded_by IS NULL
            """,
            tuple(ids),
        ).fetchall()
        return {int(r[0]): r[1:] for r in rows}

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        content, meta_json, kind, author, channel, team, source, thread_ts, created_at = row
        metadata: dict[str, Any] = {}
        if meta_json:
            try:
                parsed = json.loads(meta_json)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                pass
        for key, val in (
            ("kind", kind), ("author", author), ("channel", channel),
            ("team", team), ("source", source), ("thread_ts", thread_ts),
        ):
            if val and key not in metadata:
                metadata[key] = val
        if created_at and "date" not in metadata:
            metadata["date"] = datetime.fromtimestamp(int(created_at), timezone.utc).strftime("%Y-%m-%d")
        return MemoryEntry(content=content, store_name=self.name, metadata=metadata or None)

    # --- write -------------------------------------------------------------

    def _neighbours(self, vec: str, scope_sql: str = "", scope_params: tuple = (),
                    limit: int = _NEIGHBOURS) -> list[tuple]:
        """Closest live memories to `vec` as ``(id, content, cosine_distance)``.

        Several, not one, because decisions form chains: "cap review at 24h" →
        "moved to 4h" → "now 8h". Comparing only against the closest retires the
        middle link and leaves the *original* live — still well-sourced, still
        confidently wrong. Each new decision is checked against every live
        neighbour inside the ambiguous band.

        Scoped like dedup and recall, so a fact in another channel stays a
        distinct row. Superseded rows are excluded: a retired decision must not
        absorb its own replacement.
        """
        try:
            rows = self._conn.execute(
                f"""
                SELECT id, content, vector_distance_cos(embedding, vector32(?))
                FROM memory_entries
                WHERE superseded_by IS NULL{scope_sql}
                ORDER BY 3 ASC LIMIT ?
                """,
                (vec, *scope_params, limit),
            ).fetchall()
        except Exception as err:  # noqa: BLE001
            log.warning("near-duplicate check failed: %s", err)
            return []
        return [(int(r[0]), str(r[1]), float(r[2])) for r in rows if r[2] is not None]


    def _relation(self, new_content: str, old_content: str, *,
                  recheck: bool = False) -> str:
        """Classify a pair, reusing a previous verdict unless `recheck`.

        Same texts, same question — the only thing that would change the answer
        is the model's own randomness, so re-asking is a re-roll rather than a
        re-examination. `recheck` forces a fresh call for the case this cannot
        fix on its own: a verdict of "independent" leaves both rows live, so a
        wrong one would otherwise be locked in forever (the live store did
        exactly this, calling a duplicate pair independent on one run and
        duplicate on the next).
        """
        key = _relation_key(new_content, old_content)
        # Defence in depth: the cache is an optimisation, never a dependency. A
        # broken or missing table must degrade to "ask the model", not to a
        # failed write.
        if not recheck:
            try:
                cached = _cached_relation(self._conn, key)
            except Exception:  # noqa: BLE001
                cached = None
            if cached:
                log.debug("relation cache hit: %s", cached)
                return cached
        relation = _classify(new_content, old_content)
        try:
            _cache_relation(self._conn, key, relation)
        except Exception:  # noqa: BLE001
            log.debug("could not cache relation verdict")
        return relation

    def compact(self, *, dry_run: bool = False, limit: int = 50,
                recheck: bool = False) -> dict:
        """Apply dedup + supersession to memories already in the store.

        New writes are cleaned as they arrive, but anything written before that
        existed stays as-is — and the backlog is exactly the mess that motivated
        the feature (one evening of testing left four re-phrased copies of a
        single decision). This replays the same classification over existing
        rows, newest first, so the most recent statement of a fact is the one
        that survives.

        Returns counts. `limit` caps model calls, not rows scanned.
        """
        rows = self._conn.execute(
            "SELECT id, content FROM memory_entries WHERE superseded_by IS NULL "
            "ORDER BY id DESC"
        ).fetchall()
        stats = {"scanned": len(rows), "duplicates": 0, "superseded": 0, "calls": 0}
        retired: set[int] = set()

        for new_id, new_content in rows:
            if int(new_id) in retired:
                continue
            for old_id, old_content in rows:
                old_id = int(old_id)
                if old_id >= int(new_id) or old_id in retired:
                    continue          # only compare against strictly older rows
                distance = self._conn.execute(
                    "SELECT vector_distance_cos(a.embedding, b.embedding) "
                    "FROM memory_entries a, memory_entries b WHERE a.id=? AND b.id=?",
                    (new_id, old_id),
                ).fetchone()[0]
                if distance is None or distance > _related_distance():
                    continue
                if distance <= _dedup_distance():
                    relation = "duplicate"
                else:
                    if stats["calls"] >= limit:
                        continue
                    stats["calls"] += 1
                    relation = self._relation(str(new_content), str(old_content),
                                              recheck=recheck)
                if relation == "independent":
                    continue

                log.info("%scompact: id=%s %s id=%s (distance=%.3f)",
                         "[dry-run] " if dry_run else "", old_id,
                         "duplicate of" if relation == "duplicate" else "superseded by",
                         new_id, distance)
                stats["duplicates" if relation == "duplicate" else "superseded"] += 1
                retired.add(old_id)
                if not dry_run:
                    self._conn.execute(
                        "UPDATE memory_entries SET superseded_by=?, superseded_at=unixepoch() "
                        "WHERE id=?", (new_id, old_id))
        if not dry_run:
            self._conn.commit()
        return stats

    async def add(self, content: str, metadata: Any = None) -> int:
        # Before anything else: strip credentials. This must happen ahead of the
        # embedding, or the secret stays encoded in the vector even though the
        # text no longer shows it.
        content, redacted = redact(content)
        merged: dict[str, Any] = {}
        state = reqctx.current()
        if state is not None:
            merged.update(state.provenance())
        if redacted:
            merged["redacted"] = redacted
        if isinstance(metadata, dict):
            merged.update(metadata)
        elif metadata is not None:
            merged["note"] = str(metadata)

        cols = {c: merged.get(c) for c in _PROVENANCE_COLS}
        meta_json = json.dumps(merged, ensure_ascii=False, default=str) if merged else None
        vec = _vector_literal(self.embedder.embed_one(content))

        # Scope-aware dedup: under channel/team isolation the same fact in a
        # different scope is a distinct row (mirrors recall scoping).
        scope = reqctx.memory_scope()
        dup_sql, dup_params = ("", ())
        if scope:
            dup_sql = f" AND {scope[0]} = ?"
            dup_params = (scope[1],)
        with self._lock:
            dup = self._conn.execute(
                f"SELECT id FROM memory_entries WHERE content = ?{dup_sql} LIMIT 1",
                (content, *dup_params),
            ).fetchone()
            if dup:
                log.info("memory dedup: identical content already stored id=%s", dup[0])
                return int(dup[0])

            # Near-duplicate: same fact, different words. Keep the richer wording
            # rather than accumulating paraphrases — a re-phrased copy is not new
            # knowledge, it is noise that crowds the retrieval budget.
            superseded: list[int] = []
            duplicate_of: tuple | None = None
            neighbours = self._neighbours(vec, dup_sql, dup_params) if _dedup_enabled() else []
            for near_id, near_content, distance in neighbours:
                if distance <= _dedup_distance():
                    relation = "duplicate"          # close enough to skip the call
                elif distance <= _related_distance():
                    relation = self._relation(content, near_content)
                else:
                    break                           # sorted by distance; rest are further

                if relation == "duplicate":
                    duplicate_of = (near_id, near_content, distance)
                    break                           # nothing new to store
                if relation == "replaces" and _supersede_enabled():
                    superseded.append(near_id)

            if duplicate_of is not None:
                near_id, near_content, distance = duplicate_of
                # Keep the richer wording: a paraphrase is not new knowledge, but
                # a fuller account of the same decision is worth having.
                if len(content) > len(near_content) * 1.2:
                    self._conn.execute(
                        "UPDATE memory_entries SET content=?, metadata=?, "
                        "embedding=vector32(?) WHERE id=?",
                        (content, meta_json, vec, near_id))
                    self._conn.commit()
                    log.info("memory dedup: id=%s rewritten with a fuller wording "
                             "(distance=%.3f)", near_id, distance)
                else:
                    log.info("memory dedup: duplicate of id=%s (distance=%.3f), kept "
                             "existing", near_id, distance)
                return int(near_id)
            cur = self._conn.execute(
                """
                INSERT INTO memory_entries
                    (content, metadata, kind, author, channel, team, source, thread_ts, embedding)
                VALUES (?,?,?,?,?,?,?,?, vector32(?))
                """,
                (
                    content, meta_json, cols["kind"], cols["author"], cols["channel"],
                    cols["team"], cols["source"], cols["thread_ts"], vec,
                ),
            )
            self._conn.commit()
            rid = int(getattr(cur, "lastrowid", 0) or 0)
            if superseded and rid:
                for old_id in superseded:
                    self._conn.execute(
                        "UPDATE memory_entries SET superseded_by=?, superseded_at=unixepoch() "
                        "WHERE id=?", (rid, old_id))
                self._conn.commit()
                log.info("memory superseded: id(s)=%s replaced by id=%s",
                         ",".join(str(i) for i in superseded), rid)
        log.info("hybrid memory stored id=%s author=%s channel=%s chars=%d",
                 rid, cols["author"], cols["channel"], len(content))
        return rid


def _rrf_fuse(*ranked_lists: list[int]) -> list[int]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank). Higher = better; ties keep
    first-seen order. A doc strong on either retriever surfaces; strong on both wins."""
    scores: dict[int, float] = {}
    order: list[int] = []
    for ids in ranked_lists:
        for rank, doc_id in enumerate(ids):
            if doc_id not in scores:
                scores[doc_id] = 0.0
                order.append(doc_id)
            scores[doc_id] += 1.0 / (_RRF_K + rank)
    return sorted(order, key=lambda d: scores[d], reverse=True)


# Back-compat alias (the backend selector + older imports use this name).
LibsqlVectorMemoryStore = HybridMemoryStore
