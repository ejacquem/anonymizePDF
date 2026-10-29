# Run P: where the fake goes on a scan (placement, coverage, size) - 3 to 4 hours

Replaces the run 3 brief (`PROMPT_RUN3.md`, postponed). You work alone in your own git worktree / branch. The owner's lead session supervises:
the run is in TWO STAGES and you STOP after stage 1 with a short report (see "Stop point"); the lead reviews it and sends you the go-ahead
for stage 2. Nobody else answers questions: decide, write the decision in the journal, go on.

## Why

On the owner's REAL scans (bank statements, contracts, ~150 dpi JPEG, many words per page) the replaced text is still easy to see, and the
owner names these defects first: the fake is drawn at the WRONG PLACE (shifted, over a neighbour), the original is only PARTLY COVERED
(pieces of its letters stay beside / under the fake), the fake has the WRONG SIZE. Runs 1, 2 and Stage A worked on backgrounds, tone, tilt
>= 2 degrees and fonts; none measured placement. The bench's `dx` / `dy` are the shift of the ink CENTRE against the ideal, which mixes a
real offset with the normal width difference of another text: it cannot tell a misplaced fake from a shorter one. Real files cannot be
shown: work on generated documents (and, optionally, the public samples already in `sample_data/Dossier Public*`, rendered as poor scans,
never committed).

Read first (grep headings, line ranges, never whole files): CLAUDE.md sections 2, 5 (scan page), 9 (OCR, `ocrpage`), 10 (scans: the value's
box, `ink_start`), 10b (size and baseline, `metrics`, `fit_rows`, `layout_value`), 10d (dewarp, `TILT_MIN`), 12 / 12d (tests, the scorecard);
`docs/overnight/REPORT_STAGE_A.md`; the end of `docs/overnight/JOURNAL.md`. Python: `C:/Users/Lucas/Project/anonymizePDF/.venv/Scripts/python.exe`
(the worktree has no venv of its own; the system python has no packages).

## Hypotheses to test (the lead's reading of the code; confirm or reject each with numbers, do not assume)

- H1 OCR box vs ink: the box of a value comes from OCR word boxes (Windows OCR boxes are loose or tight, glue punctuation; words added by the
  rescaled readings of `ocrpage.merge` are brought back by a fitted plane, `_align`). If the box is off, the erase, the size and the baseline
  are all off. The erase is bounded by the box (+ `ink_tail`), so a box that misses a letter leaves it (partly covered).
- H2 small skew: a page turned 0.3 to 1.5 degrees is below `dewarp` (`TILT_MIN` 2 deg) and below the turned paint (1.5 deg): a long value
  (an IBAN of 40 mm) drifts by several pixels across its length; an axis-aligned box then leaves the top of the last letters and the bottom of
  the first ones, and the fake is straight on a turned line.
- H3 part of a word: a value glued to a label or to another token ('IBAN:BE71...', 'N°12345', 'Tél.0471...') is cut by font advances
  (`pii.match_boxes(advance=)`): one wrong advance and the cut eats the label or leaves the first digit.
- H4 size / baseline on coarse JPEG text (150 dpi, 4:2:0, blocks, `fit_rows` on 2 px strokes): the fitted scale or top is wrong on small text
  (7-9 pt) and short values (digits).
- H5 dense layouts: tight line spacing (1.0-1.15 em) and tight table columns: the box or the ink tail reaches the next line / cell, the fake is
  written over a neighbour, or the box is clipped by the neighbour rule and leaves part of the value.
- H6 page geometry (cheap check): `/Rotate` 90 / 270, a CropBox not at the origin, an image placed with a margin or a non-uniform scale, a page
  made of two image strips: the pixel path should be consistent (`page.rect`, `get_pixmap`); check that the replacement and the report boxes land
  on the value. Fix only if broken.

## Stage 1 (about 1.25 hours): measure placement, build the cases, diagnose - then STOP

1. Placement measures in `tools/challenge_bench.py` (append, keep every existing column; same numbers for the old columns). The generator knows
   the original value's ink (original page minus `clean`) and the ideal (same start, font, size, baseline, other text). Per value:
   - `resid`: share of the ORIGINAL value's ink pixels that are still visible in the output (darker than the clean page by more than the
     page's noise allowance) and are not explained by the fake's ink (outside the ideal's ink grown 1 px AND outside the output's own fake ink
     grown 1 px). This is "partly covered". Also `resid_px` (count) and `resid_max` (darkest remnant, grey levels).
   - `x0`: left edge of the output's ink in the value's area minus the ideal's left edge (px, 200 dpi); `x1err`: right edge of the fake against
     the right end of the space the original filled (overrun into a neighbour > 0).
   - `base`: baseline of the output's fake minus the ideal's (px; baseline = bottom row of the non-descender ink, robust: median of column bottoms).
   - `xh`: x-height (or cap height for digits / capitals) of the fake over the ideal's.
   - `nbr`: damage to the neighbours: share of the ink of the OTHER words within one line height of the value (original page, not the value)
     that the output changed by more than 40 levels.
   - `boxerr` (diagnostic): the report box of the value (`items[].boxes`) against the bounding box of the original value's ink: the four edge
     errors in px. It tells a detection / OCR box error (H1) from a painting error.
   Add them to `compare`, to `tools/bench_cmp.py` (toward 0, `xh` toward 1) and to the `--sheet` crops' captions.
2. New family `place_` (append to `tools/make_test_challenge.py` / `make_test_documents.py`; do not edit existing cases): pages that look like the
   owner's statements (CLAUDE.md section 11 of docs/font_matching.md: JPEG q 60-70 at 150 dpi placed at 595 x 842 pt, 4:2:0 on colour, Arial /
   Calibri / Verdana / Times 7-10 pt, 200-500 words a page):
   dense table with tight columns and amounts beside the values; tight line spacing (1.0 / 1.1 em); values glued to a label ('IBAN:', 'N°', 'Tél.',
   'Réf:'), and inside a longer token; small text 7 pt; a long IBAN and an address line on pages turned 0.3 / 0.7 / 1.2 / 1.8 degrees (and a slight
   bend under the dewarp gate); a scan with a dark border and an off-centre placement; the H6 geometry variants (`/Rotate 90`, CropBox offset,
   margin, non-uniform scale, two strips). About 12-16 cases, half `dev`, half `held`. Commit them; the held ones are FROZEN from that commit.
3. Baseline: `challenge_bench run` on `--only place_` and on the full set (one full run), commit the json (`docs/overnight/runP_base_*.json`).
4. Diagnosis: for the 15 worst values by `resid`, `x0`, `base`, `xh`, `nbr`, say which hypothesis explains each (use `boxerr`, the trace, the
   sheet pictures via `tools/sheet_view.py`: look at them). Rank the causes by the number of values they spoil, on `place_` AND on the old families.

### Stop point

End stage 1 by returning a report of at most 40 lines: the new measures (one line each), the cases, the baseline table (`place_` per case and the
whole set: found, resid, x0, base, xh, nbr, boxerr), the ranked causes with counts and two or three examples each, and your proposed fixes for
stage 2 in order (what, where in the code, expected effect, risk to privacy). Commit everything first. Then stop and wait.

## Stage 2 (about 2 hours, after the lead's go-ahead): fix, in the order agreed

Likely directions (decide from the diagnosis, not from this list): snap the value's box to its own ink (the connected ink of the value inside the
OCR box grown by a fraction of the text height, stopped by the gaps to the neighbour words and by rules; never SMALLER than the OCR box for the
erase: privacy first, the erase covers the union); a slanted box (a quad along the line's measured angle) for small skews below the turned-paint
threshold, or lowering the thresholds where the measures say it is safe; baseline and size from the value's own ink rows when its OCR box disagrees
with them; sub-word cuts from the ink gaps (a column projection between characters) rather than from font advances alone; reject a merged-reading
word whose box does not sit on ink.

## Rules that never bend

- Privacy first: the erase may grow, never shrink below what it covers now; the flat patch is always an acceptable fallback. Never weaken a check.
- `found` never drops anywhere; `resid` never rises on any value by more than noise; the frozen held `place_` cases and the old `held` split not
  worse; the old families unchanged beyond the third decimal unless the change is meant to move them (say so, with numbers).
- `pii.py`, `fields.py`, `verify.py`, `fake.py` untouched (this run is about painting, not detection). `engine.py` only for report fields.
- No special-casing (nothing in `anonymizer/` mentions a case, a seed, a generated value or a position); no new dependency (numpy + Pillow); no
  network except reading the public samples already on disk; no real documents (`sample_data/Dossier Test Reel/` is off limits).
- Commit after each ACCEPTED change (message ending with the attribution line the session gives); NEVER push, never touch `main`.
- One heavy run at a time (bench, check_all), long commands in the background, no polling; subset runs (`--only place_ ...`) to iterate, at most
  4 full bench runs and 2 `check_all --fast` runs in the whole run. Token thrift: grep and line ranges, not whole files.
- Tests: every kept behaviour gets a group in `tools/check_all.py` in the `--fast` set (a test that fails on the old code first). CLAUDE.md
  updated where behaviour changed (sections 10 / 10b / 12d).

## Deliverables

Commits on your branch, tags `runP-s1` (end of stage 1) and `runP-done`; `docs/overnight/JOURNAL.md` heading 'Run P' after every experiment
(hypothesis, numbers before / after, decision, commit); `docs/overnight/REPORT_PLACEMENT.md`: per fix what changed and why, before / after tables
(`place_` per case, the families, held vs dev), the pictures that changed most, tried and not kept, what is left, the privacy risks, and the last
lines of `check_all --fast` pasted (do not claim what you did not run).
