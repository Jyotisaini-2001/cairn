"""Score the duplicate / replaces / independent classifier on labelled pairs.

    python -m evals.measure_classifier                 # easy set, CAIRN_CLASSIFIER backend
    python -m evals.measure_classifier --set hard      # no giveaway words, traps
    python -m evals.measure_classifier --set mine      # evals/my_pairs.json, written blind
    python -m evals.measure_classifier --set mine --file evals/my_pairs_2.json
    python -m evals.measure_classifier --runs 3        # more runs = better consistency read
    python -m evals.measure_classifier --judge llm     # the LLM judge instead of Jev (uses model quota)
    python -m evals.measure_classifier --judge llm --llm-model gemini-3.8-flash

Sets:
  easy  threshold_corpus.py — written with cue words ("cut from 24 to 4 hours")
  hard  hard_pairs.py — no cue words; same-wording/different-subject traps,
        exceptions that must not retire the rule, one-word reversals
  mine  evals/my_pairs.json — pairs a person wrote without showing the labels
        to whoever tuned the classifier. The only set nobody could fit to.

Reports accuracy, how often the same pair got a different verdict across runs
(the failure that started this), and the two errors that matter unequally:

  wrong retirements — a pair that must stay independent called duplicate or
                      replaces. Invisible and unrecoverable: a live decision
                      stops being answerable.
  missed relations  — a real duplicate/replacement called independent. One
                      redundant row; noisy, but nothing is lost.

`dist` is the pair's embedding distance: above CAIRN_RELATED_DISTANCE (0.45)
Cairn never asks the classifier at all, so a miss there cannot happen live.
With Jev it also sweeps CAIRN_JEV_MIN_CONFIDENCE. One model call per pair per run.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env", override=True)
import logging; logging.disable(logging.INFO)  # noqa: E402,E702
sys.path.insert(0, str(HERE))

from cairn import jev  # noqa: E402
from cairn import vector_store as vs  # noqa: E402
from cairn.embeddings import get_embedder  # noqa: E402

MINE = HERE / "my_pairs.json"


def load(which: str, mine: Path = MINE) -> list[tuple[str, str, set, str, bool]]:
    """(old_text, new_text, acceptable verdicts, label, ambiguous)."""
    if which == "easy":
        from threshold_corpus import DECISIONS, DUPLICATES, INDEPENDENT, SUPERSESSIONS
        t = dict(DECISIONS)
        return ([(t[a], t[b], {"duplicate"}, f"{a}->{b}", False) for a, b in DUPLICATES]
                + [(t[a], t[b], {"replaces"}, f"{a}->{b}", False) for a, b in SUPERSESSIONS]
                + [(t[a], t[b], {"independent"}, f"{a}->{b}", False) for a, b in INDEPENDENT])
    if which == "hard":
        from hard_pairs import HARD_PAIRS
        return [(o, n, set(e), note, False) for o, n, e, note in HARD_PAIRS]
    if not mine.exists():
        sys.exit(f"no {mine} yet — copy my_pairs.example.json to my_pairs.json and add pairs")
    rows = json.loads(mine.read_text())
    return [(r["old"], r["new"], {r["expected"]} if isinstance(r["expected"], str)
             else set(r["expected"]), r.get("note", f"pair {i + 1}"),
             bool(r.get("ambiguous")))
            for i, r in enumerate(rows)]


def judge(old: str, new: str) -> jev.Choice | None:
    """Jev's raw answer — the same call Cairn makes (CAIRN_JEV_CHECKS decides
    whether the yes/no checks ride along). The LLM path has no confidence.

    Production falls back to the LLM when Jev is unreachable; a measurement must
    not, or one network blip silently mixes two judges into one score. Retry,
    then report the pair as an error (None) instead of switching."""
    if jev.enabled():
        for attempt in range(4):
            v = vs._ask_jev(new, old)
            if v is not None:
                return v
            time.sleep(2 * (attempt + 1))
        return None
    # LLM path: a failed call must not be scored as its "independent" fallback.
    # Free tiers limit calls per minute (Gemini: 15/min/model), so pace them,
    # and after a failure wait out the minute once before calling it an error.
    for attempt in range(2):
        _pace()
        relation = vs._llm_relation(new, old)
        if relation is not None:
            return jev.Choice(relation, None, {})
        if attempt == 0:
            time.sleep(60)
    return None


_last_llm_call = 0.0


def _pace() -> None:
    """Keep LLM calls under CAIRN_EVAL_LLM_PER_MIN (default 12) per minute."""
    global _last_llm_call
    gap = 60.0 / max(1.0, float(os.environ.get("CAIRN_EVAL_LLM_PER_MIN", "12")))
    wait = _last_llm_call + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_llm_call = time.monotonic()


def final(v: jev.Choice, bar: float | None) -> str:
    """What Cairn actually does with that answer — the production rule."""
    return v.choice if v.confidence is None else vs._jev_decide(v, bar)


def show(v: jev.Choice) -> str:
    out = v.choice + ("" if v.confidence is None else f" {v.confidence:.2f}")
    if v.checks:
        out += " [" + " ".join(f"{k[0]}{p:.2f}" for k, p in v.checks.items()) + "]"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["easy", "hard", "mine"], default="easy")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--file", type=Path, default=MINE, help="pairs file for --set mine")
    ap.add_argument("--judge", choices=["jev", "llm"],
                    help="override CAIRN_CLASSIFIER from .env (which is loaded with override)")
    ap.add_argument("--start", type=int, default=1, help="first pair to run (1-based)")
    ap.add_argument("--limit", type=int, help="run at most this many pairs (daily quotas)")
    ap.add_argument("--llm-model", help="model for --judge llm, e.g. gemini-3.8-flash "
                                        "(sets CAIRN_EXTRACT_MODEL for this run only)")
    args = ap.parse_args()
    if args.judge:
        os.environ["CAIRN_CLASSIFIER"] = args.judge
    if args.llm_model:
        os.environ["CAIRN_EXTRACT_MODEL"] = args.llm_model
    sys.stdout.reconfigure(line_buffering=True)   # show lines as they come, even through tee
    pairs = load(args.set, args.file)
    first = max(1, args.start)
    pairs = pairs[first - 1:first - 1 + args.limit if args.limit else None]
    if first > 1 or args.limit:
        print(f"  slice: pairs {first} – {first + len(pairs) - 1}")

    emb = get_embedder()
    band = vs._related_distance()

    def dist(a: str, b: str) -> float:
        va, vb = emb.embed([a, b])
        dot = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** .5
        nb = sum(y * y for y in vb) ** .5
        return 1 - dot / (na * nb) if na and nb else 1.0

    llm_model = (os.environ.get("CAIRN_EXTRACT_MODEL") or os.environ.get("CAIRN_MODEL")
                 or os.environ.get("ANTHROPIC_MODEL") or "default")
    backend = (f"jev ({jev._model()})" if jev.enabled() else
               f"llm ({os.environ.get('CAIRN_MODEL_PROVIDER', 'anthropic')} / {llm_model})")
    bar = vs._jev_min_confidence() if jev.enabled() else None
    print(f"\n  classifier: {backend}   set: {args.set}   pairs: {len(pairs)}   runs: {args.runs}"
          + (f"   safety bar: {bar}" if bar is not None else "") + "\n")

    checks_on = jev.enabled() and vs._jev_checks_enabled()
    if checks_on:
        print("  checks: [t = NEW is temporary, s = NEW is about the same case]; "
              f"replaces only retires when confidence >= {bar}, t < {vs._CLEAR_NO} "
              f"and s >= {vs._CLEAR_YES}\n")

    clear, unclear = [], []   # (acceptable, [Choice per run], distance, label)
    errors = streak = 0
    for old, new, ok, label, amb in pairs:
        runs = [judge(old, new) for _ in range(args.runs)]
        if any(r is None for r in runs):
            errors += 1
            streak += 1
            print(f"  ERR {'Jev unreachable' if jev.enabled() else 'LLM call failed'} — "
                  f"left out of the score: {label}")
            if streak >= 3 and not jev.enabled():
                print("\n  !! 3 LLM failures in a row, even after waiting — a quota (per minute "
                      "or per day) is exhausted. Stopping; the lines above are real answers.")
                break
            continue
        streak = 0
        d = dist(old, new)
        (unclear if amb else clear).append((ok, runs, d, label))
        shown = ", ".join(show(v) for v in runs)
        out = final(runs[0], bar)
        if amb:
            mark = "?? "
        else:
            mark = "ok " if out in ok else "BAD"
            if out in ok and runs[0].choice not in ok:
                mark = "ok*"               # wrong raw answer, rescued by our rules
        asked = "" if d <= band else "  (never asked live: beyond band)"
        print(f"  {mark} dist {d:.3f}  expect {'/'.join(sorted(ok)):22} got {shown:44} "
              f"final {out:11} {label}{asked}")

    if errors:
        print(f"\n  !! {errors} pair(s) could not be judged; re-run to include them")
    if clear:
        first = [(ok, runs[0]) for ok, runs, _, _ in clear]
        raw_ok = sum(v.choice in ok for ok, v in first)
        final_ok = sum(final(v, bar) in ok for ok, v in first)
        flips = sum(len({v.choice for v in runs}) > 1 for _, runs, _, _ in clear + unclear)
        print(f"\n  clear pairs: raw answer right {raw_ok}/{len(first)}   "
              f"final outcome right {final_ok}/{len(first)}   (ok* = rescued by our rules)")
        print(f"  inconsistent across runs  {flips}/{len(clear) + len(unclear)}")
        print(f"  confusion (expected -> final): "
              f"{dict(Counter(f'{sorted(ok)[0]}->{final(v, bar)}' for ok, v in first if final(v, bar) not in ok)) or 'none'}")

    if unclear:
        print(f"\n  ambiguous pairs ({len(unclear)}): no right answer — the safe outcome is")
        print("  keeping both, so a debatable decision is never silently retired.")
        for ok, runs, _, label in unclear:
            kept = final(runs[0], bar) == "independent"
            print(f"    {'kept both' if kept else 'ACTED    '}  {show(runs[0])}  {label}")

    if bar is not None and clear:
        print("\n  CAIRN_JEV_MIN_CONFIDENCE sweep (clear pairs, run 1):")
        print("    bar    wrong retirements   missed relations")
        for b in (0.5, 0.6, 0.7, 0.8, 0.9):
            wrong = missed = 0
            for ok, v in first:
                g = final(v, b)
                wrong += ok == {"independent"} and g != "independent"
                missed += "independent" not in ok and g == "independent"
            print(f"    {b:.1f}    {wrong:>8}            {missed:>8}")


if __name__ == "__main__":
    main()
