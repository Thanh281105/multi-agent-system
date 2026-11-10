"""Pre-registered analysis of the confirmatory run (see
docs/preregistration-confirmatory-run-2026-11-10.md). Offline; reads
unblinding-key.json and heldout-judge.p8.jsonl next to this file (or in
argv[1]). Missing judge cells are listed; `fill` gives a worst/best-case bound.
"""

import collections
import json
import random
import sys
from pathlib import Path

prep = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
SA = "sa_shared_tools_rag"
key = {
    e["opaque_answer_id"]: e
    for e in json.loads((prep / "unblinding-key.json").read_text(encoding="utf-8"))[
        "entries"
    ]
}
rows = [
    json.loads(line)
    for line in (prep / "heldout-judge.p8.jsonl").open(encoding="utf-8")
]
scores = {
    r["artifact"]["opaque_answer_id"]: r["artifact"]["scores"]
    for r in rows
    if r["record_type"] == "terminal" and r["status"] == "completed"
}
missing = sorted(set(key) - set(scores))


def cat(c):
    return c.split("_", 1)[1].rsplit("_", 1)[0]


def conv_means(metric, fill=None):
    v = collections.defaultdict(list)
    for a, e in key.items():
        s = scores[a].get(metric) if a in scores else fill
        if s is not None:
            v[
                (
                    cat(e["conversation_id"]),
                    e["work_group_id"],
                    e["conversation_id"],
                    e["variant_id"],
                )
            ].append(s)
    return {k: sum(x) / len(x) for k, x in v.items()}


def group_deltas(conv, cats, cand):
    byg = collections.defaultdict(list)
    for (c, g, cv, var), m in conv.items():
        if c in cats and var == SA and (c, g, cv, cand) in conv:
            byg[g].append(conv[(c, g, cv, cand)] - m)
    return {g: sum(x) / len(x) for g, x in byg.items()}


def boot(gm, n=10000):
    rng = random.Random(42)
    ks = sorted(gm)
    out = []
    for _ in range(n):
        s = [gm[rng.choice(ks)] for _ in ks]
        out.append(sum(s) / len(s))
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n)]


def table(metric, fill=None):
    conv = conv_means(metric, fill)
    cats = sorted({k[0] for k in conv})
    res = {}
    for cand in ("ma_fixed_rag", "ma_adaptive_rag", "ma_adaptive_no_rag"):
        for c in cats + ["ALL"]:
            gm = group_deltas(conv, cats if c == "ALL" else {c}, cand)
            d = sum(gm.values()) / len(gm)
            lo, hi = boot(gm)
            w = sum(x > 1e-9 for x in gm.values())
            losses = sum(x < -1e-9 for x in gm.values())
            res[f"{cand}|{c}"] = {
                "groups": len(gm),
                "delta_pp": round(d * 100, 2),
                "ci95_pp": [round(lo * 100, 2), round(hi * 100, 2)],
                "W/T/L": [w, len(gm) - w - losses, losses],
            }
    means = collections.defaultdict(list)
    for (_c, _g, _cv, var), m in conv.items():
        means[var].append(m)
    res["variant_means_pct"] = {
        k: round(100 * sum(x) / len(x), 2) for k, x in sorted(means.items())
    }
    return res


out = {
    "judged": len(scores),
    "expected": len(key),
    "missing": [key[a] for a in missing],
    "bootstrap": {"seed": 42, "resamples": 10000, "unit": "work_group"},
}
for metric in ("task_completion", "claim_support"):
    out[metric] = table(metric)
if missing:
    out["task_completion_missing_as_0"] = table("task_completion", 0.0)
    out["task_completion_missing_as_1"] = table("task_completion", 1.0)
print(json.dumps(out, ensure_ascii=False, indent=1))
