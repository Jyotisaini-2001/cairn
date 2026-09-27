"""Score the duplicate / replaces / independent classifier on labelled pairs.

    python -m evals.measure_classifier            # whatever CAIRN_CLASSIFIER says
    python -m evals.measure_classifier --runs 3   # more runs = a better consistency read

Uses the labelled pairs in threshold_corpus.py. Reports accuracy, how often the
same pair got a different verdict across runs (the failure that started this),
and the two errors that matter unequally:

  wrong retirements — an independent pair called duplicate/replaces. Invisible
                      and unrecoverable: a live decision stops being answerable.
  missed relations  — a real duplicate/replacement called independent. One
                      redundant row; noisy, but nothing is lost.

With Jev it also sweeps CAIRN_JEV_MIN_CONFIDENCE, since that is the knob that
trades one error for the other. Costs one model call per pair per run.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
import logging; logging.disable(logging.INFO)  # noqa: E402,E702
sys.path.insert(0, str(Path(__file__).resolve().parent))
from threshold_corpus import DECISIONS, DUPLICATES, INDEPENDENT, SUPERSESSIONS  # noqa: E402

from cairn import jev  # noqa: E402
from cairn import vector_store as vs  # noqa: E402

text = dict(DECISIONS)
# (old, new, expected) — in SUPERSESSIONS the second of each pair is the newer one.
PAIRS = ([(a, b, "duplicate") for a, b in DUPLICATES]
         + [(a, b, "replaces") for a, b in SUPERSESSIONS]
         + [(a, b, "independent") for a, b in INDEPENDENT])


def judge(old: str, new: str) -> tuple[str, float | None]:
    """Raw verdict (before any confidence gate) and Jev's confidence, if Jev."""
    if jev.enabled():
        v = jev.choice({"old_decision": text[old], "new_decision": text[new]},
                       "How does the NEW decision relate to the OLD one?", vs._RELATIONS)
        if v is not None:
            return v.choice, v.confidence
    return vs._classify_llm(text[new], text[old]), None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    runs = ap.parse_args().runs

    backend = f"jev ({jev._model()})" if jev.enabled() else "llm (CAIRN_MODEL_PROVIDER)"
    print(f"\n  classifier: {backend}   pairs: {len(PAIRS)}   runs: {runs}\n")

    results = []  # (expected, [(verdict, confidence) per run])
    for old, new, expected in PAIRS:
        verdicts = [judge(old, new) for _ in range(runs)]
        results.append((expected, verdicts))
        shown = ", ".join(f"{v}" + (f" {c:.2f}" if c is not None else "") for v, c in verdicts)
        mark = "ok " if all(v == expected for v, _ in verdicts) else "BAD"
        print(f"  {mark} {old}->{new:5} expected {expected:11}  got {shown}")

    first = [(exp, vs_[0]) for exp, vs_ in results]
    correct = sum(v == exp for exp, (v, _) in first)
    flips = sum(len({v for v, _ in vs_}) > 1 for _, vs_ in results)
    print(f"\n  accuracy (run 1) ........ {correct}/{len(first)}")
    print(f"  inconsistent across runs  {flips}/{len(results)}")
    print(f"  confusion (expected -> got): "
          f"{dict(Counter(f'{e}->{v}' for e, (v, _) in first if e != v)) or 'none'}")

    if any(c is not None for _, (_, c) in first):
        print("\n  CAIRN_JEV_MIN_CONFIDENCE sweep (run 1):")
        print("    bar    wrong retirements   missed relations")
        for bar in (0.5, 0.6, 0.7, 0.8, 0.9):
            wrong = missed = 0
            for exp, (v, c) in first:
                gated = "independent" if v != "independent" and (c or 0) < bar else v
                wrong += exp == "independent" and gated != "independent"
                missed += exp != "independent" and gated == "independent"
            print(f"    {bar:.1f}    {wrong:>8}            {missed:>8}")


if __name__ == "__main__":
    main()
