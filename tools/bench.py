#!/usr/bin/env python3
"""
Benchmark of the scan (config step) and the anonymization on a fixed set of generated documents, by stage.

  python tools/bench.py run <out.json> [--repeat 2] [--doc NAME ...] [--code <root>]
  python tools/bench.py compare <before.json> <after.json>

Each document is copied alone into a temporary folder, with a fixed scratch key (never the user's key), then
`engine.scan_folder` and `engine.run_anonymize` run in this one process, as the app does (the caches of the first step
serve the second one). The whole set is run `--repeat` times and the best run of each document is kept. Per document
and per phase: the wall time, the CPU time of the process, and the time by stage (anonymizer/timing.py: ocr, fonts,
paint, ocr_check...). The share of the machine that other programs used during a run is recorded too: a number taken on
a busy machine is only good for proportions. Run it when nothing else heavy is running.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_data"
KEY = Path(tempfile.gettempdir()) / "anonymizePDF_bench_key.txt"       # a fixed scratch key, not the user's
DOCS = [("Dossier Test 17", "contrat_assurance_scan.pdf"), ("Dossier Test 17", "paragraphes_scan.pdf"),
        ("Dossier Test 01", "releve_banque_mosane_scan.pdf"), ("Dossier Test 01", "releve_banque_mosane.pdf"),
        ("Dossier Test 03", "passeport_scan.pdf"),
        ("Dossier Test 05", "releve_scan_cherchable.pdf"), ("Dossier Test 05", "releve_annexe_scannee.pdf"),
        ("Dossier Test 05", "contrat_image_collee.pdf")]


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


def _system_times():
    idle, kern, user = _FT(), _FT(), _FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    v = lambda f: (f.hi << 32) | f.lo                     # noqa: E731
    return v(idle), v(kern) + v(user)                      # kernel time includes the idle time


def _busy_since(before) -> float:
    """Share (0..1) of all the cores that was busy since `before` (_system_times), every program included."""
    (i0, t0), (i1, t1) = before, _system_times()
    return 0.0 if t1 == t0 else 1.0 - (i1 - i0) / (t1 - t0)


class _MemCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_mb() -> int:
    """Peak working set of this process so far (MB)."""
    c = _MemCounters()
    c.cb = ctypes.sizeof(c)
    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MemCounters), ctypes.c_uint32]
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return int(c.PeakWorkingSetSize / 1e6)


def _clear_caches():
    from anonymizer import ocr
    ocr.clear_cache()
    try:
        from anonymizer import memo
        memo.clear_all()
    except ImportError:
        pass


def _git(code: Path) -> dict:
    def run(*a):
        return subprocess.run(["git", *a], cwd=code, capture_output=True, text=True).stdout.strip()
    if not (code / ".git").exists():                       # an exported copy: the label file written by the exporter
        label = code / "COMMIT.txt"
        return {"commit": label.read_text(encoding="utf-8").strip() if label.exists() else "export", "dirty": False}
    return {"commit": run("rev-parse", "--short", "HEAD"), "dirty": bool(run("status", "--porcelain", "--", "anonymizer"))}


def run_one(engine, ner, folder: str, name: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix="bench_"))
    try:
        shutil.copy(SAMPLES / folder / name, work / name)
        out = {}
        busy0, c0, t0 = _system_times(), time.process_time(), time.perf_counter()
        scan = engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)
        out["scan"] = {"wall": round(time.perf_counter() - t0, 2), "cpu": round(time.process_time() - c0, 2),
                       "stages": scan["stats"].get("stages", {}), "busy": round(_busy_since(busy0), 2)}
        busy0, c0, t0 = _system_times(), time.process_time(), time.perf_counter()
        rep = engine.run_anonymize(work, folder, scan["config"], "fra+eng", False, lambda *a: None)
        entry = rep["files"][0]
        if entry.get("error"):
            raise RuntimeError(f"{name}: {entry['error']}")
        out["anon"] = {"wall": round(time.perf_counter() - t0, 2), "cpu": round(time.process_time() - c0, 2),
                       "stages": entry.get("stages", {}), "busy": round(_busy_since(busy0), 2),
                       "pages": entry["pages"], "mode": entry["mode"]}
        out["total"] = round(out["scan"]["wall"] + out["anon"]["wall"], 2)
        out["peak_mb"] = peak_mb()                                  # of the whole process so far
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run(args) -> int:
    KEY.write_text("0123456789abcdef" * 4, encoding="utf-8")
    os.environ["ANON_SECRET_FILE"] = str(KEY)
    code = Path(args.code).resolve() if args.code else ROOT
    sys.path.insert(0, str(code))
    from anonymizer import engine
    from anonymizer.pii import load_ner

    t0 = time.perf_counter()
    ner = load_ner(quiet=True)
    ner_s = round(time.perf_counter() - t0, 1)
    docs = [d for d in DOCS if not args.doc or d[1] in args.doc or any(a in d[1] for a in args.doc)]
    best: dict = {}
    runs: dict = {}
    for r in range(args.repeat):
        for folder, name in docs:
            _clear_caches()                                  # every run starts with empty caches
            got = run_one(engine, ner, folder, name)
            runs.setdefault(name, []).append(got["total"])
            if name not in best or got["total"] < best[name]["total"]:
                best[name] = got
            print(f"[{r + 1}/{args.repeat}] {name}: scan {got['scan']['wall']:.1f}s, anonymize {got['anon']['wall']:.1f}s"
                  f"  (machine busy {max(got['scan']['busy'], got['anon']['busy']) * 100:.0f} %, peak {got['peak_mb']} MB)",
                  flush=True)
    result = {"meta": {"cpu_count": os.cpu_count(), "ner_load_seconds": ner_s, "repeat": args.repeat,
                       "workers_env": os.environ.get("ANON_WORKERS"), **_git(code), "when": time.strftime("%Y-%m-%d %H:%M")},
              "docs": best, "all_runs": runs}
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    show(result)
    return 0


def show(result: dict):
    print(f"\nbest of {result['meta']['repeat']} (names model loaded in {result['meta']['ner_load_seconds']}s)")
    print(f"{'document':36s} {'scan':>7s} {'anon':>7s} {'total':>7s}   stages of the anonymization")
    for name, d in result["docs"].items():
        st = ", ".join(f"{k} {v:.1f}" for k, v in list(d["anon"]["stages"].items())[:6])
        print(f"{name:36s} {d['scan']['wall']:7.1f} {d['anon']['wall']:7.1f} {d['total']:7.1f}   {st}")
    print(f"{'TOTAL':36s} {sum(d['scan']['wall'] for d in result['docs'].values()):7.1f} "
          f"{sum(d['anon']['wall'] for d in result['docs'].values()):7.1f} "
          f"{sum(d['total'] for d in result['docs'].values()):7.1f}")


def compare(args) -> int:
    a = json.loads(Path(args.before).read_text(encoding="utf-8"))
    b = json.loads(Path(args.after).read_text(encoding="utf-8"))
    print(f"before: {a['meta']['commit']}{'+' if a['meta']['dirty'] else ''} {a['meta']['when']}   "
          f"after: {b['meta']['commit']}{'+' if b['meta']['dirty'] else ''} {b['meta']['when']}")
    print(f"{'document':36s} {'scan':>15s} {'anonymize':>15s} {'total':>15s}")
    tot = [0.0] * 6
    for name in a["docs"]:
        if name not in b["docs"]:
            continue
        x, y = a["docs"][name], b["docs"][name]
        row = [x["scan"]["wall"], y["scan"]["wall"], x["anon"]["wall"], y["anon"]["wall"], x["total"], y["total"]]
        tot = [t + v for t, v in zip(tot, row)]
        print(f"{name:36s} {row[0]:6.1f} ->{row[1]:6.1f} {row[2]:6.1f} ->{row[3]:6.1f} {row[4]:6.1f} ->{row[5]:6.1f}")
    print(f"{'TOTAL':36s} {tot[0]:6.1f} ->{tot[1]:6.1f} {tot[2]:6.1f} ->{tot[3]:6.1f} {tot[4]:6.1f} ->{tot[5]:6.1f}"
          f"   ({(1 - tot[5] / tot[4]) * 100:+.0f} %)" if tot[4] else "")
    stages = sorted({s for n in a["docs"] if n in b["docs"] for ph in ("scan", "anon")
                     for s in a["docs"][n][ph]["stages"].keys() | b["docs"][n][ph]["stages"].keys()})
    print("\nstages, summed over the documents (scan + anonymize), seconds")
    for s in stages:
        sa = sum(a["docs"][n][ph]["stages"].get(s, 0) for n in a["docs"] if n in b["docs"] for ph in ("scan", "anon"))
        sb = sum(b["docs"][n][ph]["stages"].get(s, 0) for n in a["docs"] if n in b["docs"] for ph in ("scan", "anon"))
        print(f"  {s:12s} {sa:7.1f} -> {sb:7.1f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--repeat", type=int, default=2)
    r.add_argument("--code", help="root of another copy of the code (e.g. an exported older commit)")
    r.add_argument("--doc", action="append", help="only the documents whose name contains this (repeatable)")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = ap.parse_args()
    return run(args) if args.cmd == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
