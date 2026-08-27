"""Re-measure Cairn's distance thresholds against a realistic corpus."""
"""Re-measure the dedup/supersession thresholds against a realistic corpus.

Run it after any embedder change, or when the real store grows enough to matter:

    python -m evals.measure_thresholds

Uses local embeddings only — no model calls, no quota. The thresholds in
cairn/vector_store.py were set from this; the header comment there records the
numbers and why they are shaped the way they are.
"""
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
import logging; logging.disable(logging.INFO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from threshold_corpus import DECISIONS, DUPLICATES, SUPERSESSIONS, INDEPENDENT
from cairn.embeddings import get_embedder

text = dict(DECISIONS)
emb = get_embedder()
vecs = dict(zip(text.keys(), emb.embed(list(text.values()))))

def dist(a, b):
    va, vb = vecs[a], vecs[b]
    dot = sum(x*y for x, y in zip(va, vb))
    na = sum(x*x for x in va) ** .5; nb = sum(y*y for y in vb) ** .5
    return 1 - dot/(na*nb) if na and nb else 1.0

def report(label, pairs):
    ds = sorted(dist(a, b) for a, b in pairs)
    print(f"\n  {label}  (n={len(ds)})")
    print(f"    min {ds[0]:.3f}   median {statistics.median(ds):.3f}   max {ds[-1]:.3f}")
    return ds

dup = report("SAME decision, reworded", DUPLICATES)
sup = report("REPLACED (answer changed)", SUPERSESSIONS)
ind = report("INDEPENDENT (same topic area)", INDEPENDENT)

# unrelated: every cross-topic pair
keys = list(text)
unrel = [(a, b) for i, a in enumerate(keys) for b in keys[i+1:]
         if a[:3] != b[:3]]
ds = sorted(dist(a, b) for a, b in unrel)
print(f"\n  UNRELATED (different topics)  (n={len(ds)})")
print(f"    min {ds[0]:.3f}   median {statistics.median(ds):.3f}   p5 {ds[int(len(ds)*.05)]:.3f}")

print("\n  ── where the current thresholds sit ──")
print(f"    CAIRN_DEDUP_DISTANCE  = 0.08   catches {sum(1 for d in dup if d <= 0.08)}/{len(dup)} true duplicates for free")
print(f"    CAIRN_RELATED_DISTANCE= 0.45   sends {sum(1 for d in dup+sup if d <= 0.45)}/{len(dup+sup)} real relations to the model")
print(f"                                   and {sum(1 for d in ind if d <= 0.45)}/{len(ind)} independent pairs too (cost, not error)")
print(f"    unrelated pairs entering the model band: {sum(1 for d in ds if d <= 0.45)}/{len(ds)}")
