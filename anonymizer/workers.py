"""
Pages in parallel processes. A scanned page takes 10 to 20 s of numpy and Pillow work that holds the GIL, and PyMuPDF is
not thread-safe: the pages of a document are given to worker PROCESSES, one task per page, and the main process puts the
results back in page order, so the output is byte for byte what one process would write.

  worker_count(n)          how many workers a job of n pages uses (1 = everything stays in this process)
  spread(pages)            start the workers that [(document, page)] go to; submit(...) then gives them the pages
  submit(key, index, fn, *args)   run fn(*args) in the worker that owns page `index` of document `key`
  scan_page(...)           the task of one scanned page (render, OCR, find photos / codes / signatures, measure the
                           fonts, paint) -> the JPEG of the new page and everything the page recorded
  read_page(...)           the OCR words of one page of a PDF file
  shutdown()

Rules that keep the result identical to the sequential run:
- one task = one page, read from the file by path (the worker opens the PDF itself), nothing shared with the main
  process; the main process assembles the pages in page order, and so do the log lines, the records and the trace;
- a document whose config numbers its replacements ('{n}' in a replacement: the numbers follow the page order) is not
  split (`splittable`);
- a worker that dies, or any pool failure, gives the page back to the main process, which does it itself.

Workers are created on first use and stay (the pool is reused by the next job). Each worker is its own process with a
slot of its own: page k of a document always goes to the same worker, so what that worker remembered of the page (the
OCR, the fonts, the faces: anonymizer/memo.py) serves the next job on it. Nothing is written to disk. Setting
ANON_WORKERS=N caps the workers (1 = never use a pool); the default leaves one core to the app and a share of the
free memory to the rest of the PC.
"""
from __future__ import annotations

import atexit
import ctypes
import multiprocessing as mp
import os
import threading
import time
import zlib
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

WORKER_MB = int(os.environ.get("ANON_WORKER_MB", "800"))        # what one worker is expected to hold (peak, estimated)
MAX_DEFAULT = 6                                                   # no more by default: the cores are shared by threads

_lock = threading.Lock()
_broken = False                                                   # the pool cannot be created here: no workers
_slots: dict[int, ProcessPoolExecutor] = {}
_size: dict[int, int] = {}                                        # configured workers -> the pool size, fixed once chosen
stats = {"started": 0, "start_seconds": 0.0, "tasks": 0, "fallbacks": 0}


# ---------------------------------------------------------------------------
# how many
# ---------------------------------------------------------------------------

class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32), ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64), ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64), ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64), ("sullAvailExtendedVirtual", ctypes.c_uint64)]


def available_mb() -> int | None:
    """Free physical memory (MB), None when it cannot be read (not Windows)."""
    try:
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


def configured_workers() -> int:
    """The workers the pool may have: ANON_WORKERS, else the cores minus one (the app keeps one), at most MAX_DEFAULT."""
    if _broken:
        return 1
    env = os.environ.get("ANON_WORKERS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(1, min((os.cpu_count() or 2) - 1, MAX_DEFAULT))


def worker_count(n_tasks: int) -> int:
    """Workers a job of `n_tasks` pages can use: no more than pages and than the pool size. 1 = no pool."""
    if configured_workers() <= 1:
        return 1
    return max(1, min(n_tasks, pool_size()))


def splittable(cfg: dict) -> bool:
    """Can the pages of a document be done apart? Not when a replacement numbers its values ('{n}'): the numbers
    follow the order of the pages (pii.Numberer)."""
    if any("{n}" in str(v) for v in cfg.get("terms", {}).values()):
        return False
    return not any("{n}" in str(r.get("replace", "")) for r in cfg.get("regex") or [])


# ---------------------------------------------------------------------------
# the pool: one single-process executor per slot, so that a page always goes to the same process
# ---------------------------------------------------------------------------

def _init_worker():
    """A worker's start: the face model and the font catalog are loaded once. onnxruntime comes FIRST: loading it after
    the Windows OCR packages (winrt) crashes the process on some PCs."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pass
    from . import faces, fontmatch, ocr

    ocr.engine()
    fontmatch.faces()
    faces._load()


def _ready() -> int:
    return os.getpid()


def _env_for_workers():
    """The environment workers are started with: one thread each for numpy / onnxruntime (the processes are the
    parallelism), unless the user chose otherwise."""
    cores = os.cpu_count() or 2
    per = max(1, cores // max(configured_workers(), 1))
    saved = {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ANON_ORT_THREADS")}
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, "1")
    os.environ.setdefault("ANON_ORT_THREADS", str(per))
    return saved


def _start(indexes) -> None:
    """Start the workers of these slots that are not running, all at once (each one loads its models while the others
    do): returns when they are ready."""
    global _broken
    t0 = time.perf_counter()
    waiting = []
    with _lock:
        saved = _env_for_workers()
        try:
            for i in indexes:
                if i not in _slots:
                    ex = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"),
                                             initializer=_init_worker)
                    _slots[i] = ex
                    waiting.append((i, ex, ex.submit(_ready)))       # the process is spawned by this first submit
        except Exception:  # noqa: BLE001  (e.g. a daemonic process may not have children: everything stays here)
            _broken = True
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    for i, ex, future in waiting:
        try:
            future.result()
            stats["started"] += 1
        except Exception:  # noqa: BLE001  (a worker that cannot start: its pages are done by the main process)
            _drop(i)
    if waiting:
        stats["start_seconds"] += time.perf_counter() - t0


def _slot(i: int) -> ProcessPoolExecutor:
    if i not in _slots:
        _start([i])
    ex = _slots.get(i)
    if ex is None:
        raise BrokenProcessPool(f"worker {i} did not start")
    return ex


def _drop(i: int):
    with _lock:
        ex = _slots.pop(i, None)
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)


def pool_size() -> int:
    """The number of slots pages are spread over, fixed for the life of the process (so that a page always meets the
    worker that remembers it): the configured workers, less what the free memory cannot hold when the first is asked."""
    n = configured_workers()
    if n not in _size:
        free = available_mb()
        size = n
        if n > 1 and free is not None and not os.environ.get("ANON_WORKERS"):
            size = max(2, min(n, (free - 1500) // WORKER_MB))
        _size[n] = max(1, size)
    return _size[n]


def slot_of(key: str, index: int, workers: int) -> int:
    """The slot of page `index` of document `key`: the same every time, consecutive pages on consecutive slots."""
    return (zlib.crc32(key.encode("utf-8")) + index) % max(workers, 1)


def _call(fn, engine_choice: str, args: tuple):
    """What a worker runs: the task, with the OCR engine the user chose in the app (a worker does not see the choice
    made in the main process after it started)."""
    from . import ocr

    ocr.set_engine(engine_choice)
    return fn(*args)


class Task:
    """A page given to a worker. result() is the worker's answer, or None if the pool failed (the caller then does the
    page itself); an error raised by the page is raised again here."""

    def __init__(self, slot: int, fn, args: tuple):
        from . import ocr

        self.slot, self.fn, self.args = slot, fn, args
        self.future: Future | None = None
        try:
            self.future = _slot(slot).submit(_call, fn, ocr.choice(), args)
            stats["tasks"] += 1
            stats[fn.__name__] = stats.get(fn.__name__, 0) + 1
        except Exception:  # noqa: BLE001  (the pool could not start: the main process does the work)
            stats["fallbacks"] += 1
            _drop(slot)

    def result(self):
        if self.future is None:
            return None
        try:
            return self.future.result()
        except BrokenProcessPool:
            stats["fallbacks"] += 1
            _drop(self.slot)
            return None

    def cancel(self):
        if self.future is not None:
            self.future.cancel()


def spread(pages: list[tuple]) -> int:
    """Get ready for the pages [(document key, page index)]: the workers they go to are started together (the first
    time). Returns the pool size to give to submit()."""
    n = pool_size()
    _start(sorted({slot_of(key, index, n) for key, index in pages}))
    return n


def submit(key: str, index: int, workers: int, fn, *args) -> Task:
    """fn(*args) in the worker of page `index` of document `key` (with `workers` slots)."""
    return Task(slot_of(key, index, workers), fn, args)


def _forget() -> int:
    from . import memo

    memo.clear_all()
    return os.getpid()


def clear_caches() -> None:
    """Empty the memos, the OCR cache and the renderings of every running worker (the benchmark starts cold)."""
    futures = [ex.submit(_forget) for ex in list(_slots.values())]
    for f in futures:
        try:
            f.result()
        except Exception:  # noqa: BLE001
            pass


def shutdown():
    for i in list(_slots):
        _drop(i)


atexit.register(shutdown)


# ---------------------------------------------------------------------------
# the tasks (they run in the workers; they only import what they need)
# ---------------------------------------------------------------------------

def scan_page(path: str, index: int, n_pages: int, cfg: dict, ocr_lang: str) -> dict:
    """One scanned page of a PDF file, done from the file: the JPEG of the new page, its records, its extras (black
    boxes, MRZ, trace), the log lines of the page and the time it took by stage."""
    from . import scanpaint, timing
    from .pii import Numberer, fitz

    records, extras, lines = [], {"blackouts": [], "mrz": [], "trace": []}, []
    doc = fitz.open(path)
    try:
        with timing.collecting() as tm:
            img = scanpaint._scan_page(doc[index], index + 1, n_pages, cfg, scanpaint.pii_cfg(cfg), ocr_lang,
                                       Numberer(), records, extras, lines.append)
            with timing.stage("save"):
                jpeg = scanpaint._jpeg(img)
    finally:
        doc.close()
    return {"jpeg": jpeg, "records": records, "extras": extras, "log": lines, "stages": dict(tm.seconds),
            "pid": os.getpid()}


def read_scan(path: str, index: int, ocr_lang: str) -> dict:
    """A scanned page of a PDF file as the scan of a folder reads it: its OCR words, its grey image and the native
    resolution of the scan (what the fonts of the names are measured on)."""
    import numpy as np

    from . import scanpaint, timing
    from .ocr import ocr_words, render_page
    from .pii import fitz

    doc = fitz.open(path)
    try:
        with timing.collecting() as tm:
            with timing.stage("render"):
                img = render_page(doc[index], 300)
            words = ocr_words(img, ocr_lang)
            with timing.stage("render"):
                gray = np.asarray(img.convert("L"))
                scale = scanpaint.native_scale(doc[index], 300)
    finally:
        doc.close()
    return {"words": words, "gray": gray, "scale": scale, "stages": dict(tm.seconds), "pid": os.getpid()}


def name_faces_page(gray, words: list[dict], scale: float, wanted: list[tuple], cap: int) -> dict:
    """The rough face (PageFonts.quick_face) of the name words of one scanned page: `wanted` = [(word index, name key)]
    in reading order, at most `cap` faces found per name key on the page (the scan keeps `cap` per name over all the
    pages, so no more can be needed from one page)."""
    from . import scanpaint, timing

    cfg = {"style": {"mode": "replace", "font_match": True, "quality_match": True}}
    found, count = {}, {}
    with timing.collecting() as tm:
        pf = scanpaint.page_fonts(gray, words, cfg, scale)       # no slant measure here: a rough face is enough
        for i, key in wanted:
            if count.get(key, 0) >= cap:
                continue
            face = pf.quick_face(words[i]["box"])
            found[i] = face
            if face is not None:
                count[key] = count.get(key, 0) + 1
    return {"faces": found, "stages": dict(tm.seconds), "pid": os.getpid()}


def read_page(path: str, index: int, ocr_lang: str) -> dict:
    """The OCR words of one page of a PDF file, rendered at 300 dpi (the reading of an output page by the checks)."""
    from . import timing
    from .ocr import ocr_words, render_page
    from .pii import fitz

    doc = fitz.open(path)
    try:
        with timing.collecting() as tm:
            with timing.stage("ocr_check"):
                words = ocr_words(render_page(doc[index], 300), ocr_lang)
    finally:
        doc.close()
    return {"words": words, "stages": dict(tm.seconds), "pid": os.getpid()}
