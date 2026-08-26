"""Golden-set evaluation runner for Cairn (Pillar 01 — evaluation).

Runs the golden dataset (`evals/golden.json`) through the *real* agent
(`agent.run`, i.e. real MiniMax/Anthropic calls) and scores each case against
its expectations: which tools were used, what the reply must / must not contain,
and that no guardrail crash occurred. Prints a scorecard (overall + per-category
pass rate, tool-selection accuracy) and exits non-zero on any failure so it can
gate CI. Results are also persisted to the `eval_results` table for trend tracking.

Memory is isolated to a throwaway DB by default so add/recall cases get a clean,
deterministic store and never pollute production memory (eval_results still
persist to the real DATABASE_PATH). Memory-dependent cases (mem-add-* before
mem-recall-*) rely on file order within a single run. Pass --use-real-memory to
run against the live store instead.

Usage:
    cairn-eval                 # run the whole golden set
    cairn-eval --limit 5       # first 5 cases (smoke test)
    cairn-eval --case calc-multiply mem-recall-standup
    cairn-eval --json          # machine-readable summary to stdout
    cairn-eval --resume        # continue the last run, skipping its passes
    cairn-eval --resume <id> --model gemini-3.7-flash   # continue on another model
    cairn-eval --use-real-memory   # don't isolate memory (default: throwaway DB)

Each case in golden.json:
    { "id", "category", "prompt", "context?": {user, channel, channel_type},
      "expect": { "must_use_tools"[], "must_not_use_tools"[],
                  "must_contain"[], "must_not_contain"[],
                  "min_guardrail_hits"?, "max_latency_ms"? } }
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("cairn.eval")

DATASET = Path(__file__).resolve().parent.parent / "evals" / "golden.json"


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _score(run, expect: dict) -> list[str]:
    """Return a list of failure reasons; empty list == pass."""
    reasons: list[str] = []
    text = (run.text or "").lower()
    used = list(run.tool_calls or [])

    for tool in expect.get("must_use_tools", []):
        if tool not in used:
            reasons.append(f"missing tool '{tool}' (used: {used or 'none'})")
    for tool in expect.get("must_not_use_tools", []):
        if tool in used:
            reasons.append(f"forbidden tool '{tool}' was used")
    for sub in expect.get("must_contain", []):
        if sub.lower() not in text:
            reasons.append(f"reply missing '{sub}'")
    for sub in expect.get("must_not_contain", []):
        if sub.lower() in text:
            reasons.append(f"reply contains forbidden '{sub}'")
    if "min_guardrail_hits" in expect:
        if run.guardrail_hits < expect["min_guardrail_hits"]:
            reasons.append(f"guardrail_hits {run.guardrail_hits} < {expect['min_guardrail_hits']}")
    return reasons


def _resume_target(which: str) -> tuple[str, set[str]]:
    """``(run_id, ids already passed)`` for a run to continue.

    ``which`` is a run id, or "last" for the most recent. A case that *failed* is
    deliberately retried — a failure is the thing you are trying to fix, and the
    non-determinism the classifier already showed us means one failure is not a
    verdict. Only passes are skipped.
    """
    import sqlite3  # noqa: PLC0415

    path = os.environ.get("DATABASE_PATH", "./data/cairn.db")
    try:
        conn = sqlite3.connect(path)
        if which == "last":
            row = conn.execute(
                "SELECT run_id FROM eval_results ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return uuid.uuid4().hex[:12], set()
            which = str(row[0])
        rows = conn.execute(
            "SELECT case_id FROM eval_results WHERE run_id=? AND passed=1", (which,)
        ).fetchall()
        return which, {str(r[0]) for r in rows}
    except Exception as err:  # noqa: BLE001
        log.warning("could not read previous run (%s); starting fresh", err)
        return uuid.uuid4().hex[:12], set()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cairn-eval", description="Run Cairn's golden eval set.")
    parser.add_argument("--dataset", default=os.environ.get("CAIRN_EVAL_DATASET", str(DATASET)))
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--case", nargs="+", default=None, help="run only these case ids")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    parser.add_argument("--model", help="override CAIRN_MODEL for this run — "
                        "free-tier quotas are per model, so rotating models is "
                        "how a full run finishes in one day")
    parser.add_argument("--resume", metavar="RUN_ID", nargs="?", const="last",
                        help="continue a run, skipping cases it already passed; "
                             "omit the id to continue the most recent run")
    parser.add_argument(
        "--use-real-memory", action="store_true",
        help="run against the real memory DB instead of a throwaway one (default: isolate)",
    )
    parser.add_argument(
        "--use-real-jira", action="store_true",
        help="let eval turns hit the real Jira (default: strip creds so jira_* tools are inert)",
    )
    args = parser.parse_args(argv)

    # Evals must be side-effect-free by default: an agent turn that escalates to
    # jira_create_issue would file a REAL ticket (it happened: KAN-23). Tool
    # SELECTION is what's scored, and the jira tools degrade cleanly to "Jira is
    # not configured" without creds — so strip them for the run.
    if not args.use_real_jira:
        os.environ.pop("JIRA_API_TOKEN", None)

    # Isolate memory by default so eval add/recall cases get a clean, deterministic
    # store and never pollute production memory. eval_results still go to the real
    # DATABASE_PATH (telemetry is decoupled from memory). Memory-dependent cases
    # (mem-add-* before mem-recall-*) rely on file order within one run.
    if args.model:
        os.environ["CAIRN_MODEL"] = args.model

    # Resolve the run id first: an isolated memory DB has to be *stable per run*
    # or resuming breaks the dataset. The mem-add cases seed memories that the
    # mem-recall cases read; with a fresh throwaway DB on resume, every recall
    # would fail for want of a memory that a skipped case had already written —
    # false failures that look exactly like a regression.
    resume_id, already_passed = _resume_target(args.resume) if args.resume else (None, set())
    run_id = resume_id or uuid.uuid4().hex[:12]

    if not args.use_real_memory:
        # Under data/, not the system temp dir: a resumed run may be *days* later
        # (a free-tier quota is per day), and macOS prunes temp. If the memory DB
        # vanished between sittings, every mem-recall case would fail for want of
        # a memory its mem-add case had already written — a false regression.
        mem_dir = os.path.join(os.path.dirname(
            os.environ.get("DATABASE_PATH", "./data/cairn.db")) or ".",
            "eval-runs", run_id)
        os.makedirs(mem_dir, exist_ok=True)
        os.environ["CAIRN_MEMORY_DB_PATH"] = os.path.join(mem_dir, "memory.db")
        os.environ["CAIRN_VECTOR_DB_PATH"] = os.path.join(mem_dir, "memory_vec.db")

    # Import after load_dotenv (+ memory env) so the model/store env is in place.
    from cairn import observability as obs
    from cairn.agent import QuotaExhaustedError
    from cairn.agent import run as run_agent

    cases = _load(Path(args.dataset))
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    # Resuming matters because a free-tier quota is measured in tens of requests
    # per day: a 35-case set cannot finish in one sitting, and re-running from
    # the top would spend the next day's allowance re-proving yesterday's passes.
    # Results append to the SAME run_id so the scorecard is one comparable run.
    if already_passed:
        skipped = [c["id"] for c in cases if c["id"] in already_passed]
        cases = [c for c in cases if c["id"] not in already_passed]
        if not args.json:
            print(f"\nResuming run {run_id}: {len(skipped)} case(s) already passed, "
                  f"{len(cases)} to go.")
    # Ask the agent, not the env: the provider factory resolves CAIRN_MODEL /
    # ANTHROPIC_MODEL / the built-in default, and reading one env var recorded
    # "?" for every run the moment the project moved off Anthropic — which
    # defeats the point, since comparing models is what this dataset is for.
    from cairn.agent import _model_id as _resolve_model  # noqa: PLC0415
    model_id = _resolve_model()
    results: list[dict] = []

    if not args.json:
        print(f"\nCairn eval · run {run_id} · model {model_id} · {len(cases)} cases\n" + "─" * 64)

    from cairn import agent as agent_mod

    for c in cases:
        expect = c.get("expect", {})
        # Fresh agent per case: a cached Strands agent carries CONVERSATION
        # HISTORY across invocations, so case N would see case N-1's turns
        # (e.g. a channel-A codename leaking into a channel-B question). Memory
        # still persists across cases via the shared store — that's what the
        # mem-add → mem-recall pairs exercise.
        agent_mod._agents.clear()
        t0 = time.perf_counter()
        try:
            run = run_agent(c["prompt"], context=c.get("context"))
            err = None
        except QuotaExhaustedError as exc:
            # Every remaining case would fail the same way, recording false
            # failures that look like regressions. Stop, and say how to continue.
            if not args.json:
                print(f"\n⛔ Quota exhausted after {len(results)} case(s).")
                print(f"   Resume tomorrow:  cairn-eval --resume {run_id}")
                print(f"   Or switch model:  cairn-eval --resume {run_id} --model <other-model>")
            log.warning("eval halted on exhausted quota: %s", str(exc)[:160])
            break
        except Exception as exc:  # noqa: BLE001
            run = None
            err = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - t0) * 1000)

        if run is None:
            reasons = [f"run raised: {err}"]
            tool_calls: list[str] = []
        else:
            reasons = _score(run, expect)
            tool_calls = list(run.tool_calls or [])
            max_lat = expect.get("max_latency_ms")
            slow = max_lat is not None and latency_ms > max_lat  # soft: reported, not a failure

        passed = not reasons
        obs.record_eval(run_id, c["id"], c.get("category", "?"), model_id,
                        passed, latency_ms, tool_calls, reasons)
        results.append({
            "id": c["id"], "category": c.get("category", "?"), "passed": passed,
            "latency_ms": latency_ms, "tools": tool_calls, "reasons": reasons,
        })

        if not args.json:
            mark = "✅ PASS" if passed else "❌ FAIL"
            slow_tag = " ⚠️slow" if (run is not None and expect.get("max_latency_ms") and latency_ms > expect["max_latency_ms"]) else ""
            print(f"{mark}  {c['id']:<24} {c.get('category',''):<10} {latency_ms:>6}ms{slow_tag}  tools={tool_calls or '-'}")
            for r in reasons:
                print(f"        ↳ {r}")

    # --- scorecard ---------------------------------------------------------
    total = len(results)
    passed = sum(r["passed"] for r in results)
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    tool_cases = [c for c in cases if c.get("expect", {}).get("must_use_tools") or c.get("expect", {}).get("must_not_use_tools")]
    tool_ids = {c["id"] for c in tool_cases}
    tool_passed = sum(1 for r in results if r["id"] in tool_ids and not [x for x in r["reasons"] if "tool" in x])
    tool_acc = (tool_passed / len(tool_ids) * 100) if tool_ids else 100.0

    summary = {
        "run_id": run_id, "model_id": model_id,
        "total": total, "passed": passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "tool_selection_accuracy": round(tool_acc, 1),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False))
    else:
        print("─" * 64)
        print(f"Overall: {passed}/{total} ({summary['pass_rate']}%)   "
              f"tool-selection: {summary['tool_selection_accuracy']}%")
        print("By category: " + "  ".join(f"{k} {v}" for k, v in summary["by_category"].items()))
        print(f"Stored under run_id={run_id} in eval_results.\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
