# Run P report: placement, coverage and size of the fakes on scans

Branch `worktree-agent-a11dd171726032f77` (from main d2ca872), brief `PROMPT_PLACEMENT.md`, journal "# Run P". Nothing pushed or merged.

## The measures (`challenge_bench.measure_place`, on the value's own band)
`resid` = share of the original value's ink still in the output; `x0` = fake's left ink edge minus the ideal's; `x1err` / `x1over` = right
edge against the original's; `base` = baseline, fake minus ideal; `xh` = x-height of the fake over the ideal's; `nbr` = share of the neighbour
words' ink changed; `boxerr` / `boxmiss` = the report box against the original's ink. The ideal is the page rendered with other texts of the
same font, size and geometry (`make_test_challenge`). Fakes and ideal are read at the same ink level (reading them at different levels had
made every fake look 1 px wider on each side: a bench defect, fixed and kept in `*_xhbiased.json`).

## The `place_` cases
16 dense 150 dpi JPEG statements x 51 values (8 dev, 8 held, held frozen): Arial / Calibri / Verdana / Times 7-10 pt, leading 1.0-1.25, glued
labels ('IBAN:', 'N°', 'Réf:', '(réf.X)'), pages turned 0.3-1.8 degrees, a bend, /Rotate 90, CropBox offset, image strips, off-centre page.
Page geometry (rotate, crop, strips, margin, scale) places the fakes like plain pages: not broken.

## Stage 1 diagnosis (baseline: place_ found 83 %, medians resid 0, |x0| 1, base 0, xh 1.00)
Causes by count: detection misses (136 values, out of scope); the end of a shorter fake sitting on the original's last glyph (a bench limit,
not remnant ink outside the erase box); a fake "10-16 px right" (the bench took the neighbour value's longer ideal text for the ideal's
start); tight leading 1.0 (size and baseline wrong); a glued label erased with its value (4 cases); Times / Verdana capitals heavier.

## Fixes
- Fake 10-16 px right: KEPT as a bench fix only (2c40ae8). `layout_value` already starts at the box's x. x0 of those values 13-14 -> 0-2.
- Erase growing right onto connected ink: DROPPED. The red pixels are the fake's own glyph on the original's; `ink_tail` already erases the rest.
- Tilted / bent doc_ pages: no geometry error found; the misses are parts detection did not take. Nothing changed.
- Tight leading: KEPT (a6b8322, 74183cb), `fontmatch.PageFonts`: a word's fitted baseline must lie in its OCR box; a value fitted much bigger
  (ln > 0.18) than its nearest line words takes their size and baseline; those nearest words must have their centre in the value's rows
  (a shared OCR line id otherwise gave the baseline of the next rows: 121 for 60). `placement_tests` has 5 checks, the shared-id one fails
  on the old code (baseline 121.5 for 60). The first try of 74183cb (row filter without the size rule) lost the size fix (36 for 30) and was
  corrected before commit.
- Glued labels: NOT done (time).

## Before / after (full set, 1256 values; base = `runP_base3_full.json`, after = `runP_final_full.json`)
| place_ (680 found of 816) | before | after |
|---|---|---|
| found | 83 % | 83 % |
| resid > 0.02 | 25 | 25 |
| abs(x0) > 4 px | 7 | 7 |
| abs(base) >= 3 px | 5 | 5 |
| abs(xh - 1) > 0.15 | 10 | 9 |
| nbr > 0.02 | 15 | 15 |
| boxmiss > 2 | 8 | 8 |
(Before the bench fixes, `runP_base_full.json`: abs(x0) > 4: 12, abs(base) >= 3: 8, xh: 13; that was the bench, not the code.)
Acceptance rules (`challenge_bench compare`, `bench_cmp`): found 85 % -> 85 % overall (never drops); resid unchanged (median 0.00, resid_n
0.03 -> 0.03); held place_ not worse (`dark` 4.45 -> 4.40); the old families (bg, font, sharp, tone, doc) unchanged or within +-0.04;
small rises in `deco` dark (3.89 -> 3.99), `geom` dy (0.26 -> 0.30), overall bg_excess (0.36 -> 0.38), `place` dark (4.23 -> 4.25): not
explained, within what the bench shows between runs; they were already in `runP_done_full.json`. The effect of run P on the synthetic set is
small: one tight-leading value fixed (xh tail 10 -> 9), the rest was the bench.

## What is left
- Detection misses: about 17 % of place_ values are not found; parts of a value not taken (the street number of 'Avenue des Arts 8' on a fax,
  the 'g.' of 'g.delcourt@...', a postcode on a fax); glued labels ('IBAN:', 'E-mail:' erased with the value, 'IBAN:BE48' box 25-44 px short).
- 'REF DOM-...' erased with nothing written: not reproduced (the OCR reads 'DOMS307423'; the fake is written, in a wrong italic face).
- Times / Verdana capitals written heavier (seen, not measured).
- Second check, 3 failures in `check_all --fast` that are NOT from run P (same output on main with the same keys, tests not loosened):
  `releve_150_scan.pdf` STREET_ADDRESS 'Avenue de Ila' (page 2, both keys) and `carte_identite_faible_qualite.pdf` PERSON 'IDF' (key 1).

## Privacy risks
A value the detection does not find stays readable (the second check is the net). A box short of the value's ink (glued labels, street
number) can leave a piece of the original beside the fake. No change of run P erases less or reads less than before: the fixes only move
the size and baseline of text that is written. Wrong size / baseline of a fake was a quality defect, not a leak.

## check_all --fast, last lines as they are
```
1847 passed, 3 failed (2063s)
  longest jobs: colour_tests 999s, Dossier Test 17, key 2 938s, Dossier Test 17, key 1 869s, ruler_tests 474s, Dossier Test 05, key 2 449s
  FAILED: releve_150_scan.pdf: second check finds nothing readable
      [{'page': 2, 'type': 'STREET_ADDRESS', 'value': 'Avenue de Ila', 'hits': [{'page': 2, 'box': [103.7, 145.4, 153.1, 152.2]}]}]
  FAILED: carte_identite_faible_qualite.pdf: second check finds nothing readable
      [{'page': 1, 'type': 'PERSON', 'value': 'IDF', 'hits': [{'page': 1, 'box': [96.8, 409.0, 108.6, 415.0]}]}]
  FAILED: releve_150_scan.pdf: second check finds nothing readable
      [{'page': 2, 'type': 'STREET_ADDRESS', 'value': 'Avenue de Ila', 'hits': [{'page': 2, 'box': [103.7, 145.4, 153.1, 152.2]}]}]
  key of a run with 1 failure(s): C:\Users\Lucas\AppData\Local\Temp\anonymizePDF_check_keys\20261029-171255-key0.key
    replay: python tools/check_all.py --key "C:\Users\Lucas\AppData\Local\Temp\anonymizePDF_check_keys\20261029-171255-key0.key"
  key of a run with 2 failure(s): C:\Users\Lucas\AppData\Local\Temp\anonymizePDF_check_keys\20261029-171255-key1.key
    replay: python tools/check_all.py --key "C:\Users\Lucas\AppData\Local\Temp\anonymizePDF_check_keys\20261029-171255-key1.key"
```
