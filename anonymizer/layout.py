"""Lines and blocks of the OCR words of a page (boxes in pixels). Used to measure the style of the text around a value
(its block: paragraph, label column, table cell), never to change the text itself.

A line is a run of words on one row (their vertical centres within half a height of each other); a line is cut where
the horizontal gap between two words is over 3 word heights (two columns, a label and its value far apart). A block is
a run of lines, read from the top down, that look alike: the gap between them is at most one text height, their
heights are alike (0.8 to 1.25), and they overlap horizontally (or start at the same x).
"""
from __future__ import annotations

import statistics

GAP_SPLIT = 3.0         # horizontal gap, in word heights, that cuts a line into two segments
BLOCK_GAP = 1.0         # vertical gap between two lines of a block, in line heights
HEIGHT_RATIO = (0.8, 1.25)
ALIGN = 1.5             # left edges closer than this many heights count as aligned


def _h(box) -> float:
    return max(box[3] - box[1], 1.0)


def _segments(words: list[dict]) -> list[dict]:
    """Lines of the page: {"idx": [word indices, left to right], "box": [x0, y0, x1, y1], "h": median height}."""
    order = sorted(range(len(words)), key=lambda i: (words[i]["box"][1] + words[i]["box"][3]) / 2)
    rows: list[dict] = []
    for i in order:
        b = words[i]["box"]
        h, cy = _h(b), (b[1] + b[3]) / 2
        best = None
        for r in rows[-6:]:                              # the rows near the word are the only candidates
            if abs(cy - r["cy"]) <= 0.5 * min(h, r["h"]) and 0.6 <= h / r["h"] <= 1.6:
                if best is None or abs(cy - r["cy"]) < abs(cy - best["cy"]):
                    best = r
        if best is None:
            rows.append({"idx": [i], "cy": cy, "h": h})
        else:
            best["idx"].append(i)
            best["cy"] = statistics.fmean(
                (words[j]["box"][1] + words[j]["box"][3]) / 2 for j in best["idx"])
            best["h"] = statistics.median(_h(words[j]["box"]) for j in best["idx"])
    segments = []
    for r in rows:
        idx = sorted(r["idx"], key=lambda i: words[i]["box"][0])
        cut = [0]
        for a, b in zip(idx, idx[1:]):
            gap = words[b]["box"][0] - words[a]["box"][2]
            if gap > GAP_SPLIT * r["h"]:
                cut.append(idx.index(b))
        cut.append(len(idx))
        for start, end in zip(cut, cut[1:]):
            part = idx[start:end]
            boxes = [words[i]["box"] for i in part]
            segments.append({"idx": part,
                             "box": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                     max(b[2] for b in boxes), max(b[3] for b in boxes)],
                             "h": statistics.median(_h(b) for b in boxes)})
    segments.sort(key=lambda s: (s["box"][1] + s["box"][3]) / 2)
    return segments


def _joins(prev: dict, line: dict) -> bool:
    gap = line["box"][1] - prev["box"][3]                 # vertical gap (negative: the lines overlap)
    if gap > BLOCK_GAP * prev["h"]:
        return False
    if not (HEIGHT_RATIO[0] <= line["h"] / prev["h"] <= HEIGHT_RATIO[1]):
        return False
    a, b = prev["box"], line["box"]
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    aligned = abs(a[0] - b[0]) <= ALIGN * prev["h"]
    return overlap > 0.3 * min(a[2] - a[0], b[2] - b[0]) or aligned


def blocks(words: list[dict]) -> tuple[list[dict], list[int | None]]:
    """-> (blocks, block_of): each block {"id", "lines": [[word indices]], "words": [...], "h": median word height,
    "box"}; block_of[i] is the block of word i (every word has one)."""
    segments = _segments(words)
    groups: list[list[dict]] = []
    for seg in segments:
        if groups and _joins(groups[-1][-1], seg):
            groups[-1].append(seg)
        else:
            groups.append([seg])
    out, block_of = [], [None] * len(words)
    for k, group in enumerate(groups):
        idx = [i for seg in group for i in seg["idx"]]
        boxes = [words[i]["box"] for i in idx]
        for i in idx:
            block_of[i] = k
        out.append({"id": k, "lines": [seg["idx"] for seg in group], "words": idx,
                    "h": statistics.median(_h(b) for b in boxes),
                    "box": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes)]})
    return out, block_of
