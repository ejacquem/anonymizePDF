"""Square max / min filters on page images, numpy only.

A square max (or min) filter of odd size k is the max (min) over the rows of the max (min) over the columns, so it
takes two passes of k shifted copies instead of k * k comparisons for each pixel. The result is the same as PIL's
MaxFilter / MinFilter, which clips the window at the edges: the padding below uses the neutral value (0 for a max,
the largest value for a min), which never changes a max or a min. Used by the code search (regions.find_codes) and by
the paper colour estimate of the MRZ erase (render._erase_ink).
"""
from __future__ import annotations

import numpy as np


def _along(a: np.ndarray, k: int, axis: int, op, fill) -> np.ndarray:
    """op over the k shifted copies of `a` along one axis, the window centred on each pixel."""
    r = k // 2
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    padded = np.pad(a, pad, mode="constant", constant_values=fill)
    n = a.shape[axis]
    out = None
    for i in range(k):
        index = [slice(None)] * a.ndim
        index[axis] = slice(i, i + n)
        shifted = padded[tuple(index)]
        out = shifted if out is None else op(out, shifted)
    return out


def _square(a: np.ndarray, k: int, op, fill) -> np.ndarray:
    if k < 1 or k % 2 == 0:
        raise ValueError("the square size must be odd")
    return _along(_along(a, k, 1, op, fill), k, 0, op, fill)


def square_max(a: np.ndarray, k: int) -> np.ndarray:
    """The largest value of each k x k square (k odd)."""
    return _square(a, k, np.maximum, np.iinfo(a.dtype).min)


def square_min(a: np.ndarray, k: int) -> np.ndarray:
    """The smallest value of each k x k square (k odd)."""
    return _square(a, k, np.minimum, np.iinfo(a.dtype).max)


def close(a: np.ndarray, k: int) -> np.ndarray:
    """Morphological closing: dilation (max) then erosion (min), fills gaps smaller than k."""
    return square_min(square_max(a, k), k)
