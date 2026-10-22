"""
Stage timers: where the time of a scan or an anonymization goes (OCR, fonts, painting, the output check...).

    with timing.collecting() as t:          # one collector per file (or per scan of a folder)
        with timing.stage("ocr"):
            ...
    t.report(total)                          # {'ocr': 3.1, 'fonts': 12.4, ..., 'other': 0.8} seconds

The time of a stage is EXCLUSIVE: while a stage runs inside another one (the fonts measured while the matches are
planned), the outer stage is paused, so the stages add up to the wall time of the collector. A stage outside any
collector costs nothing. Nothing here changes a result; it only reads the clock.

Work done in a worker process (the pages in parallel) is measured there and added with `add_workers()`: it is kept
apart (`worker_report()`), because it adds up to more than the wall time; the main process shows the time it waited for
the workers as the stage 'wait'.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from functools import wraps

_local = threading.local()


class Timers:
    def __init__(self):
        self.seconds: dict[str, float] = {}
        self.cpu: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        self.workers: dict[str, float] = {}         # seconds by stage done in worker processes (all of them added)
        self.stack: list[list] = []                 # [name, wall start of the open slice, cpu start]
        self.t0 = time.perf_counter()
        self.c0 = time.process_time()

    def enter(self, name: str):
        now, cpu = time.perf_counter(), time.process_time()
        self._close_slice(now, cpu)
        self.stack.append([name, now, cpu])
        self.calls[name] = self.calls.get(name, 0) + 1

    def leave(self):
        now, cpu = time.perf_counter(), time.process_time()
        self._close_slice(now, cpu)
        self.stack.pop()
        if self.stack:
            self.stack[-1][1], self.stack[-1][2] = now, cpu

    def _close_slice(self, now: float, cpu: float):
        if self.stack:
            name, t, c = self.stack[-1]
            self.seconds[name] = self.seconds.get(name, 0.0) + (now - t)
            self.cpu[name] = self.cpu.get(name, 0.0) + (cpu - c)

    def pause(self):
        self._close_slice(time.perf_counter(), time.process_time())

    def resume(self):
        if self.stack:
            self.stack[-1][1], self.stack[-1][2] = time.perf_counter(), time.process_time()

    def add_workers(self, stages: dict):
        """Add the stages {stage: seconds} measured by a worker process."""
        for k, v in stages.items():
            self.workers[k] = self.workers.get(k, 0.0) + v

    def worker_report(self) -> dict[str, float]:
        return dict(sorted(((k, round(v, 2)) for k, v in self.workers.items() if v >= 0.005), key=lambda kv: -kv[1]))

    def merge(self, other: "Timers"):
        """Add the stages of another collector."""
        self.add_workers(other.workers)
        for k, v in other.seconds.items():
            self.seconds[k] = self.seconds.get(k, 0.0) + v
        for k, v in other.cpu.items():
            self.cpu[k] = self.cpu.get(k, 0.0) + v
        for k, v in other.calls.items():
            self.calls[k] = self.calls.get(k, 0) + v

    def wall(self) -> float:
        return time.perf_counter() - self.t0

    def report(self, total: float | None = None) -> dict[str, float]:
        """{stage: seconds} rounded to 0.01 s, the stages with no time left out; 'other' = what no stage covers (only
        when the collector ran in one process: the stages of workers add up to more than the wall time)."""
        out = {k: round(v, 2) for k, v in self.seconds.items() if v >= 0.005}
        total = self.wall() if total is None else total
        other = total - sum(self.seconds.values())
        if other >= 0.005:
            out["other"] = round(other, 2)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def current() -> Timers | None:
    return getattr(_local, "current", None)


@contextmanager
def collecting():
    """A new collector for the code inside. When one is already active (a benchmark around a scan), the new one is
    added to it at the end and the outer stage is paused meanwhile."""
    outer = current()
    t = Timers()
    if outer is not None:
        outer.pause()
    _local.current = t
    try:
        yield t
    finally:
        _local.current = outer
        if outer is not None:
            outer.merge(t)
            outer.resume()


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False

    done = __exit__


_NULL = _Null()


class _Stage:
    __slots__ = ("t",)

    def __init__(self, t: Timers, name: str):
        self.t = t
        t.enter(name)

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        self.t.leave()
        return False

    done = __exit__                                  # `tok = stage('paint')` ... `tok.done()`: no indentation needed


def stage(name: str, keep: bool = False):
    """`with stage('ocr'):` the time of the block goes to that stage. keep=True: not when a stage of the same family
    ('ocr...') is already open (the OCR of the output pages is 'ocr_check', not 'ocr')."""
    t = current()
    if t is None:
        return _NULL
    if keep and t.stack and t.stack[-1][0].startswith(name):
        return _NULL
    return _Stage(t, name)


def timed(name: str):
    """Decorator: the time of the whole function goes to the stage `name`."""
    def deco(fn):
        @wraps(fn)
        def run(*args, **kw):
            with stage(name):
                return fn(*args, **kw)
        return run
    return deco


def format_stages(stages: dict[str, float], limit: int = 7) -> str:
    """'fonts 12.4s, ocr 3.1s, ...' for the job log."""
    return ", ".join(f"{k} {v:.1f}s" for k, v in list(stages.items())[:limit])
