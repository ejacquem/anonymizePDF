"""
In-memory memos of what is measured on a page, so that the same page is not measured twice in one app process: the scan
of a folder, its anonymization, and an anonymization run again (Apply, a changed config) read the same pixels.

Every memo is keyed by the pixels of the page (and by the words OCR read on it where the measure depends on them), and
holds the result of a deterministic function: a memo hit gives exactly what the function would have computed. They are
bounded (least recently used out) because the app runs for hours, they live in memory only, and nothing is ever written
next to the documents. `clear_all()` empties them (the benchmark starts each run cold).
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from functools import wraps

import numpy as np


class Lru:
    """A small thread-safe least-recently-used map bounded by a count of entries."""

    def __init__(self, name: str, limit: int):
        self.name, self.limit = name, limit
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.limit:
                self._d.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._d.clear()
            self.hits = self.misses = 0

    def __len__(self):
        return len(self._d)


_registry: list[Lru] = []


def lru(name: str, limit: int) -> Lru:
    m = Lru(name, limit)
    _registry.append(m)
    return m


_clearers: list = []


def on_clear(fn) -> None:
    """Also call `fn` (an lru_cache's cache_clear, the OCR cache's clear) when clear_all() is asked."""
    _clearers.append(fn)


def clear_all() -> None:
    for m in _registry:
        m.clear()
    for fn in _clearers:
        fn()


def stats() -> dict[str, tuple[int, int, int]]:
    """{memo: (entries, hits, misses)}, for the benchmark and the tests."""
    return {m.name: (len(m), m.hits, m.misses) for m in _registry}


def array_key(a: np.ndarray) -> str:
    """The pixels of an array (sha1 is not for security: it is the quickest way to tell two images apart)."""
    h = hashlib.sha1()
    h.update(f"{a.dtype}|{a.shape}".encode())
    h.update(np.ascontiguousarray(a))
    return h.hexdigest()


def image_key(img) -> str:
    """The pixels of a PIL image, with its mode and size."""
    h = hashlib.sha1()
    h.update(f"{img.mode}|{img.size}".encode())
    h.update(np.ascontiguousarray(img))
    return h.hexdigest()


def by_image(name: str, limit: int = 24):
    """Decorator for `finder(img, *args) -> list of tuples` that reads the pixels of the image and nothing else: the
    boxes found on an image are remembered by its pixels (and the other arguments)."""
    def deco(fn):
        cache = lru(name, limit)

        @wraps(fn)
        def run(img, *args):
            key = (image_key(img), args)
            got = cache.get(key)
            if got is None:
                got = cache.put(key, tuple(fn(img, *args)))
            return list(got)
        return run
    return deco


def words_key(words: list[dict]) -> str:
    """The text, box and line of every OCR word: what the measures of a page read from its words."""
    h = hashlib.sha1()
    for w in words:
        h.update(repr((w["text"], tuple(w["box"]), w.get("line"))).encode())
    return h.hexdigest()
