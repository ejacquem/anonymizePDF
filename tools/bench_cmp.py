"""Compact before / after of two `tools/challenge_bench.py run` files, with the acceptance rule's verdicts.

Per group, the measures that moved, as before -> after with a sign (+ better, - worse; found higher is better, the
others closer to their ideal: bg_excess, bg95, bg_worst, ink_rel, dark, dy, dx, coll toward 0, mass and height toward 1).

Usage: python tools/bench_cmp.py before.json after.json [--groups PREFIX ...] [--min 0.01] [--measures a,b,...]
       (default groups: ALL, family:*, split:*; any group whose name starts with a PREFIX is added)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MEASURES = ("found", "bg_excess", "bg95", "bg_worst", "ink_rel", "mass", "dark", "height", "edge", "edge_rel", "dy", "dx", "coll")
ONE = ("mass", "height", "edge", "edge_rel")


def badness(k: str, v):
    """A number that is lower when the measure is better."""
    if v is None:
        return None
    if k == "found":
        return -v
    if k in ONE:
        return abs(v - 1.0)
    return abs(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--groups", nargs="*", default=[])
    ap.add_argument("--min", type=float, default=0.01, help="smallest change shown (in the measure's own unit)")
    ap.add_argument("--measures", default=",".join(MEASURES))
    a = ap.parse_args()
    x = json.loads(Path(a.before).read_text(encoding="utf-8"))["summary"]
    y = json.loads(Path(a.after).read_text(encoding="utf-8"))["summary"]
    measures = a.measures.split(",")
    names = [g for g in sorted(set(x) & set(y)) if g == "ALL" or g.split(":")[0] in ("family", "split")
             or any(g.startswith(p) for p in a.groups)]
    names.sort(key=lambda g: (g != "ALL", g))
    worse_any = []
    for g in names:
        cells = []
        for k in measures:
            u, v = x[g].get(k), y[g].get(k)
            bu, bv = badness(k, u), badness(k, v)
            if bu is None or bv is None:
                if (u is None) != (v is None):
                    cells.append(f"{k} {u}->{v}")
                continue
            if abs(bv - bu) < a.min:
                continue
            sign = "+" if bv < bu else "-"
            if sign == "-":
                worse_any.append((g, k))
            fmt = "{:.0%}" if k == "found" else "{:.2f}"
            cells.append(f"{sign}{k} {fmt.format(u)}->{fmt.format(v)}")
        print(f"{g:<28}n={y[g]['n']:<4}" + ("  ".join(cells) if cells else "(same)"))
    print()
    print("WORSE:", ", ".join(f"{g}/{k}" for g, k in worse_any) if worse_any else "nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
