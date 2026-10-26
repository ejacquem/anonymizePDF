# Run 2: sharp, poor-quality scans and old typed documents - report

Branch `claude/sharp-quality-32d24f`, tags `sharp-p0` (36eeb97), `sharp-p1` (3608f44), `sharp-p2` (61df40f), `sharp-p3` and the last code
commit (3c83798), `sharp-done` (the report). Nothing was pushed, `main` was not touched. The journal of every experiment, with its
numbers: `JOURNAL.md` ("Run 2"). Brief: `PROMPT_SHARP.md`.

**`python tools/check_all.py --fast` was green on the last code commit (3c83798): 1813 passed, 0 failed (1006 s)**, log `docs/overnight/runs/check_final.log`
(not committed; last lines at the end of this file). A first run at the end of phase 0 (1796 passed, 0 failed) was started in the background
while I kept editing `ocrpage.py`, so it is not a clean check of that commit; the final one is.

## In one page

| phase | what | result (iteration subset = `sharp_` + `doc_sharp_` + `doc_vieux`, 121 values; full table below) |
|---|---|---|
| 0 | the three open privacy findings of the review of run 1, a failing test first each (`sharp_review_tests`): white text on a thin dark band (polarity from the value's own box), black-box mode on a turned page (dewarp now runs there too), a ghost of blurred strokes under the surface (`ghost_parts`, a ladder of larger closing / lifted level / flat patch); try/except around `straighten` and the flat paint | the four checks that fail on main pass; a blue highlighter with blurred digits and a sharp page keep their surface |
| 1 | OCR: a page whose readings disagree (agreement < 93 %) is also read with thicker strokes, at 200 dpi, and, when its pixels are runs of equal pixels, smoothed; a scanned page with ink where the OCR reads (almost) no word is a warning in the log and in the report | `found` 69 -> 71 % on the subset (`doc_sharp_assurance_bw200` 25 -> 38 %, `doc_vieux_worse` 31 -> 38 %); the 90 % target of the brief for `doc_vieux_*` is out of reach: detection (`pii.py`, frozen) caps it at 77-92 % |
| 2 | `Quality` gets a tone curve (`steep`, `mid`) fitted on the strong edges of the page's words, drawn on the scan's own pixel grid; the weight match moves its centre | `edge_rel` 0.63 -> 0.97, `edge` 0.57 -> 1.01, `dark` 17.0 -> 12.2, `height` 1.04 -> 1.00; `ink_rel` 0.68 -> 0.69, `mass` 1.03 -> 1.05 |
| 3 | a typewritten page (share of long words best written in a monospace face >= 0.33) gets monospace values; a value of few letters takes the size of its line | `doc_vieux_bad` written in the Courier faces only (Verdana, DejaVu Sans, Georgia, Barlow before); `font:lucida` `ink_rel` 0.86 -> 0.37 |

New bench measure: `edge_rel` = `edge` over the same ratio for the REST of our output page (`challenge_bench.context_edge`). The original `edge`
(output over original) cannot reach 1 for a fair fake because our output is a 300 dpi JPEG of the page; `edge_rel` says whether the replaced text is as sharp as the
text beside it. Both are in the table.

```
python tools/challenge_bench.py run out.json [--only PREFIX]... [--sheet s.html]
python tools/bench_cmp.py before.json after.json --groups case: --measures found,edge,edge_rel,ink_rel,mass,dark,height,coll --min 0
python tools/sheet_view.py s.html out_prefix --match "doc_vieux" --count 8 --cols 2
python tools/check_all.py --fast                      # the gate
python tools/bench.py run out.json --repeat 1 ; python tools/bench.py compare a.json b.json   # speed
```

## The table (full run, 64 cases, 440 values: `baseline_sharp_fixed.json` -> `final_sharp_full.json`)

`baseline_sharp_fixed.json` = the code of main (740638e) with the generator fix below, so the folder holds the same pages as the final run (see "Generator fix").
`baseline_sharp_full.json` is the first baseline, with the fax squashed into half a page. No value that was found at the baseline is lost (0 lost, 5 gained).

| group | n | found | edge | edge_rel | ink_rel | mass | dark | height | bg_worst | coll |
|---|---|---|---|---|---|---|---|---|---|---|
| ALL | 440 | 87% -> 88% | 0.80 -> 0.84 | 1.00 | 0.61 -> 0.61 | 1.07 -> 1.07 | 4.8 -> 4.4 | 1.00 -> 1.00 | 17.1 -> 22.9 | 0.0 -> 0.0 |
| split:dev | 264 | 90% -> 90% | 0.79 -> 0.84 | 1.00 | 0.57 -> 0.56 | 1.07 -> 1.06 | 5.0 -> 4.4 | 1.02 -> 1.00 | 17.1 -> 16.8 | 0.0 -> 0.0 |
| split:held | 176 | 82% -> 84% | 0.81 -> 0.82 | 1.01 | 0.66 -> 0.67 | 1.07 -> 1.07 | 4.3 -> 4.2 | 1.00 -> 1.00 | 7.6 -> 22.9 | 0.0 -> 0.0 |
| family:bg | 45 | 100% -> 100% | 0.83 -> 0.83 | 1.01 | 0.49 -> 0.49 | 1.04 -> 1.04 | 3.9 -> 3.9 | 1.04 -> 1.04 | 3.4 -> 3.4 | 0.0 -> 0.0 |
| family:deco | 25 | 92% -> 92% | 0.83 -> 0.83 | 1.01 | 0.55 -> 0.55 | 1.06 -> 1.06 | 3.3 -> 3.3 | 1.04 -> 1.04 | 4.5 -> 4.5 | 0.0 -> 0.0 |
| family:geom | 50 | 100% -> 100% | 0.82 -> 0.82 | 0.99 | 0.66 -> 0.66 | 1.12 -> 1.12 | 3.1 -> 3.1 | 1.01 -> 1.01 | 2.0 -> 2.0 | 0.0 -> 0.0 |
| family:font | 65 | 98% -> 98% | 0.75 -> 0.75 | 0.92 | 0.58 -> 0.56 | 1.08 -> 1.08 | 3.9 -> 3.9 | 1.00 -> 1.00 | 1.8 -> 1.8 | 0.0 -> 0.0 |
| family:tone | 40 | 100% -> 100% | 0.72 -> 0.74 | 0.96 | 0.47 -> 0.46 | 1.03 -> 1.03 | 7.9 -> 4.9 | 1.00 -> 1.00 | 2.5 -> 2.4 | 0.0 -> 0.0 |
| family:sharp | 35 | 100% -> 100% | 0.69 -> 0.76 | 1.00 | 0.62 -> 0.61 | 1.00 -> 1.00 | 16.0 -> 13.7 | 1.00 -> 1.00 | 2.8 -> 2.8 | 0.0 -> 0.0 |
| family:doc | 180 | 69% -> 72% | 0.76 -> 0.94 | 1.04 | 0.69 -> 0.71 | 1.11 -> 1.12 | 6.1 -> 4.9 | 1.00 -> 1.00 | 17.1 -> 22.9 | 5.0 -> 6.5 |
| case:doc_sharp_assurance_bw200 | 16 | 25% -> 38% | 0.44 -> 0.66 | 1.21 | 0.73 -> 0.92 | 1.10 -> 1.14 | 7.2 -> 9.5 | 0.98 -> 0.96 | 1.6 -> 1.6 | 24.7 -> 27.4 |
| case:doc_sharp_contrat_contrast120 | 16 | 62% -> 69% | 0.52 -> 1.24 | 0.86 | 0.96 -> 1.03 | 1.41 -> 1.43 | 9.1 -> 19.0 | 1.08 -> 1.00 | 17.1 -> 16.8 | 2.8 -> 2.8 |
| case:doc_sharp_releve_bw150 | 15 | 87% -> 87% | 0.57 -> 1.11 | 0.94 | 0.65 -> 0.68 | 0.90 -> 0.95 | 38.3 -> 12.5 | 1.00 -> 1.00 | 4.9 -> 5.1 | 21.7 -> 21.5 |
| case:doc_vieux_bad | 13 | 54% -> 54% | 0.64 -> 1.11 | 0.94 | 0.70 -> 0.90 | 1.09 -> 1.14 | 25.4 -> 6.8 | 1.00 -> 1.00 | 2.9 -> 2.9 | 2.0 -> 5.5 |
| case:doc_vieux_fax | 13 | 69% -> 69% | 0.49 -> 1.07 | 1.17 | 0.80 -> 0.70 | 0.94 -> 1.15 | 45.6 -> 3.9 | 1.10 -> 1.04 | 3.0 -> 3.0 | 2.1 -> 5.3 |
| case:doc_vieux_worse | 13 | 31% -> 38% | 0.71 -> 0.51 | 0.29 | 1.12 -> 1.04 | 1.46 -> 1.53 | 17.2 -> 16.1 | 1.21 -> 1.38 | 3.5 -> 3.3 | 2.2 -> 7.2 |
| sharp:bw100 | 5 | 100% -> 100% | 2.12 -> 2.12 | 1.14 | 0.86 -> 0.86 | 1.05 -> 1.05 | 12.6 -> 12.6 | 1.00 -> 1.00 | 0.8 -> 0.8 | 0.0 -> 0.0 |
| sharp:bw150 | 5 | 100% -> 100% | 0.69 -> 0.80 | 0.99 | 0.46 -> 0.50 | 0.99 -> 0.99 | 20.9 -> 14.4 | 1.04 -> 1.00 | 0.8 -> 0.8 | 0.0 -> 0.0 |
| sharp:bw200 | 5 | 100% -> 100% | 0.50 -> 0.57 | 0.91 | 0.71 -> 0.72 | 0.98 -> 0.98 | 18.7 -> 16.9 | 1.00 -> 1.00 | 0.5 -> 0.5 | 0.0 -> 0.0 |
| sharp:contrast150 | 5 | 100% -> 100% | 0.75 -> 0.89 | 1.09 | 0.78 -> 0.77 | 1.24 -> 1.22 | 6.6 -> 1.5 | 1.04 -> 1.00 | 2.8 -> 2.8 | 0.0 -> 0.0 |
| sharp:jpeg200 | 5 | 100% -> 100% | 0.62 -> 0.70 | 1.04 | 0.62 -> 0.63 | 1.02 -> 1.02 | 11.0 -> 9.5 | 1.00 -> 1.00 | 0.4 -> 0.3 | 0.0 -> 0.0 |
| sharp:thick150 | 5 | 100% -> 100% | 0.76 -> 0.76 | 0.94 | 0.59 -> 0.59 | 0.99 -> 0.99 | 16.5 -> 16.5 | 1.04 -> 1.04 | 0.7 -> 0.7 | 0.0 -> 0.0 |
| sharp:thin200 | 5 | 100% -> 100% | 0.41 -> 0.60 | 0.99 | 0.62 -> 0.60 | 0.97 -> 0.98 | 26.1 -> 15.2 | 1.00 -> 1.00 | 0.4 -> 0.5 | 0.0 -> 0.0 |

Reading it against the acceptance rule of the brief:
- `edge` >= 0.85 on the target groups: met for `doc_sharp_*` and `doc_vieux_*` except `doc_vieux_worse` (0.51); `sharp:*` 0.57-0.89 for `edge`, but `edge_rel` is 0.91-1.14 (the output page itself is softer than the original: see above). 200 dpi pages (`bw200`, `thin200`, `jpeg200`) keep `edge` 0.57-0.70.
- soft families (`bg`, `deco`, `geom`, `font`, `tone`): the five-row cases are unchanged (same numbers to the third decimal) except where the new code helps on purpose: `tone:binary` and `tone:halo` get a tone curve (`dark` 7.9 -> 4.9), `font:lucida` `ink_rel` 0.86 -> 0.37 (monospace prior), `family:font` `ink_rel` 0.58 -> 0.56. `bg_excess`/`deco` `dark` 3.28 -> 3.34 (hl_yellow_part). The doc pages (`doc_releve`, `doc_assurance`, `doc_contrat`) move because they are the pages with coarse text or typed parts: `edge` 0.76 -> 0.94.
- `found` never drops anywhere (0 values lost of 440).
- `ink_rel` and `mass` are NOT better on every target group: `doc_vieux_bad` `ink_rel` 0.70 -> 0.90, `doc_sharp_assurance_bw200` 0.73 -> 0.92, `doc_sharp_contrat_contrast120` 0.96 -> 1.03; `dark` of contrast120 9.1 -> 19.0 (the fake is now darker than the generator's ideal: the page's strokes are very dark and the fake follows them). A sharp fake that does not sit exactly on the pixels of the ideal costs more in the L1 distance of `ink_rel` than a soft one (blur wins L1 whenever the shapes differ); I judged the pictures instead: the sharp, hard fake is what the owner's complaint is about. The numbers say so plainly: the brief's rule "ink_rel / mass / dark not worse" is met overall (ALL 0.61 = 0.61, 1.07 = 1.07, 4.8 -> 4.4) and not per case.
- `coll` (collateral change of the page away from the values) rises on `doc_vieux_*` (2 -> 5-7) and `family:doc` 5.0 -> 6.5: not a painted-over label, but values of OTHER documents of the folder found on this page now (the N/Réf `98/0417`, the date), and the signature black box of `bw200` that was there before (checked by pictures of the changed bands).
- `bg_worst` 17.1 -> 22.9: ONE value, `doc_contrat_persp` row 4 (a yellow highlighter on a perspective page): the smear of the strokes in its surface is 16 % / 12.5 % / 12.2 % of the strokes' contrast for the three ways to take it out, over the 12 % limit, so it gets the flat patch of the old code (private, but a white box in the highlighter). Chosen by the privacy rule; see risks.
- speed: `tools/bench.py` (8 documents, scan + anonymize) 170.6 s (run 1 final) -> 168.6 s (-1 %). The full challenge bench 720 -> 705 s (same folder: baseline_sharp_fixed vs final; the poor pages are read two or three times more, the rest is as fast); the subset of poor pages anonymizes in 126 -> 163 s.

## Generator fix and the baselines

`make_test_challenge.write_pdf` placed the scan with `page.insert_image(page.rect, ...)`, whose default `keep_proportion=True` squashed the 200 x 100 dpi fax (a 1654 x 1170
image) into the middle band of the page, half its height, text flat: the "0 values replaced, silent" finding of the brief on `doc_vieux_fax` was the generator,
not the pipeline (the OCR reads 206 words of the fixed fax at once). Fixed with `keep_proportion=False` (a fax fills its page: non-square pixels), and the fax is a real
test now (found 0 -> 69 %). The first full baseline (`baseline_sharp_full.json`, committed) has the squashed fax; the comparisons use `baseline_sharp_fixed.json`, made with the code of
740638e and the fixed generator in a second checkout (the fakes of a case depend on the other cases of the folder: with the readable fax in it `Claire Dubois`
is no longer detected as a name on three tilted pages, `vocab` rule, so those values flip on any run: a context effect that is in both baselines now).

## Phase 0 - the three findings

1. `backdrop.is_dark_paper`: polarity from the median of the value's box (not the window's). Test: a name in white on a navy band 1.5 text heights tall, lightest pixel left in the erased box 255 -> < 60 (the band's level 36).
2. `scanpaint._scan_page`: `dewarp.straighten` runs in black-box mode (the boxes are drawn on the flat page and mapped back), both it and the flat paint + `compose` are in try / except with a log line and the old per-word path as fallback (the records of a failed attempt are forgotten). Test (OCR): `geom_tilt12` in black-box mode, the ink of every value is black in the output: 1 of 5 -> 5 of 5.
3. `backdrop.ghost_parts` / `Surface._clean`: smear and hole of the level over the strongest ink pixels of the box, against the paper of the same colour next to the strokes; ladder in the journal. Tests: blurred 150 dpi copy (darkest pixel left 23 -> < 14), the same through `_erase_patch`, a thin blue highlighter with blurred digits keeps its colour, a clean page keeps its surface. The first version of the guard fell back to the flat patch on highlighted values of turned / bent pages (`bg_worst` 5 -> 21 in the first full run): fixed by looking at pictures, three changes (JOURNAL R2-P0b).

## Phase 1 - reading

- `ocrpage.block_factors` finds a scan enlarged without interpolation from the pixels alone (fax 1.5 x 3, 100 dpi 3 x 3, 150 dpi 2 x 2; five-row pages drawn bilinear: 1). `agreement` (clean 96-100 %, poor 81-94 %) gates `extra_readings` (thick, native, smooth).
- The OCR's errors on these pages are single characters ('+32' read '4.32', 'Namur' read 'N',unur'); readings repair part of them. The values still missed are mostly those; matching a garbled reading against the config is `pii.py` (frozen).
- `scanpaint._unread_warning`: ink >= 0.3 % of the page and fewer than max(5, 8 x ink %) words -> log + `warnings` of the file (Recap). Tests in `sharp_read_tests`.
- Targets of the brief: `doc_vieux_*` >= 90 %: 54-69 % (`det`, what the config holds, is 77-92 %: 'Claire Dubois' the signer, the company's street and a date are not detected by `pii.py`); `doc_sharp_*` >= 85 %: 38 / 69 / 87 %. Not met; `found` never fell anywhere.

## Phase 2 - the fake is as hard as the page

`Quality.steep` / `mid` (default 0: soft pages untouched), `fit_tone` (gate: strong edges of the scan >= 1.5 x the fitted candidates, or >= 1.2 with a mid-tone share under 0.35, linear blur <= 1.9), `_native_aligned` / `_toned_up` (the scan's own grid, bilinear between native centres), `weight_matched` moves the centre. See CLAUDE.md 10b for the mechanism and JOURNAL R2-P2 for what was tried: the residual never chooses a curve (shapes dominate), a gate of 1.2 on all pages moved the soft families (rejected), a curve for a coarse page with a big blur improved `edge_rel` 0.66 -> 0.88 but lost one value (rejected).

## Phase 3 - family and size

`PageFonts.mono_page` and the monospace restriction in `face_for`; `metrics`: a value of fewer than 9 letters takes the size of its line. The pitch from OCR boxes does not separate typed from proportional pages (CV 0.19-0.22 against 0.16-0.34), the face votes do (0.35-0.57 against 0.00-0.30). Family measure: not built, by pictures and the `written in` log line instead. Glyph copying: not tried.

## Pictures that changed most (case, row; edge, dark before -> after; look at them with `sheet_view.py` on `runs/final_sharp_sheet.html`, not committed)

- `doc_sharp_contrat_contrast120` rows 4, 5, 6 ('Mathis Verhaegen' on a yellow highlighter, '02/11/1979', 'Rue des Tanneurs 14'): edge 0.53 -> 1.32, 0.55 -> 1.33, 0.52 -> 1.29; the fake is a hard black text now, a little heavier than the generator's ideal (dark 17 -> -27).
- `doc_vieux_fax` rows 3 and 7 ('Avenue des Arts 8' / 'Chaussée du Stade 94,'): edge 0.37 -> 1.16, 0.33 -> 1.09, dark 46 -> 2.4 / 4.4, written in Courier New like the letter.
- `doc_vieux_bad`: all the values in the Courier faces, dark 25 -> 7; `doc_sharp_releve_bw150` dark 38 -> 12.5, edge 0.57 -> 1.11; `sharp_thin200`, `sharp_bw150` rows (edge 0.41 -> 0.60, 0.69 -> 0.80).
- `doc_releve_tilt` row 4 and `doc_releve_bend` row 4 (blue highlighter IBANs): `bg_worst` kept at 5-6 by the lifted level (21 with the first guard).

## Tried and not kept

- Smoothed OCR reading always added on coarse pages: no gain, `doc_vieux_bad` 54 -> 38 %. Extra `MinFilter(5)` reading: found unchanged, +10 % time.
- Tone curve chosen by the fit residual: it never found one (a grid of 36 curves on `sharp_bw150`: 0.0204 -> 0.0199). Tone gate 1.2 on all pages: moved the soft families (`font` ink_rel 0.58 -> 0.64, dark 3.9 -> 5.1). Tone for a coarse page with blur 3.2: lost a value (above).
- Guard fallbacks: a normalised Gaussian average of the paper (white of the page beyond a highlighter), a gated max filter (1 s a value).
- Pitch of a block from the widths of its OCR boxes.

## What is left

- `found` of the poor pages is bounded by the OCR (single characters garbled) and by `det`; a fallback that matches the config's values against garbled readings (fuzzy, OCR-tolerant by shape) is in `pii.py` / `engine.py`, which this run did not touch.
- `doc_vieux_worse` (100 dpi, blocky, speckle, blur fit 3.2): `edge_rel` 0.29, the fake is soft and the weight 1.5 times the original (an Arial-like bold in place of the typed text). A coarse page whose fit saturates the blur grid needs a model of speckle and blocks of its own.
- `ink_rel` of the sharp fakes: the glyph shapes are the catalog's (Courier New is there, a typewriter of 1950 with jitter is not): glyph copying from the page (exemplars of clean words) is the next step and was not tried.
- Typed text whose OCR box is cut (the town of an address): the line-size rule helps by one; nothing measures it.
- 200 dpi pages (`bw200`, `thin200`, `jpeg200`) keep `edge` 0.57-0.70 (`edge_rel` 0.91-1.04).

## Risks (what could leak or over-reach)

- Nothing touches `pii.py`, `fields.py`, `verify.py`, `fake.py`; `engine.py` only for the report's `warnings`. A value the OCR does not read is still not replaced; the warning only helps when a whole page is unread.
- More OCR readings add words to a poor page: the `vocab` rule of the name detector (a word written in lower case anywhere makes a name word ordinary) can flip a name on a page; seen only with the readable fax in the folder, not from the extra readings (the words of `doc_releve_tilt` are identical with and without). Worth a check on real documents.
- The surface may keep a faint SHADOW of the strokes (up to 12 % of their contrast, at the scale of the closing: blobs, not letters) when no way takes it all out and the highlighter would be lost; above it the flat patch is used (1 value in the bench). A forensic contrast stretch could show the blob where the strokes were; it does not carry letter shapes. Lower `backdrop.GHOST_LAST` to 0.04 to be strict (every such value gets the flat patch: a white box in a highlighter).
- Over-reach: `mono_page` (>= 33 % of the long words best written in a monospace face) forces monospace values on the whole page; a proportional page with many Consolas-looking words could get them wrongly (style only). The tone curve only changes how a fake is painted. The unread-page warning also fires on pages that are mostly photograph (a warning, not an action).
- `ANON_DEBUG_SURFACE=1` prints how each surface was made (no value, no text); the debugging dump of a window of the page that I used during the run was removed before the end.

## check_all --fast

Last lines (code commit 3c83798, `runs/check_final.log`, not committed):

```
1813 passed, 0 failed (1006s)
  longest jobs: colour_tests 500s, Dossier Test 17, key 2 434s, Dossier Test 17, key 1 434s, Dossier Test 05, key 2 245s, Dossier Test 05, key 1 245s
```

After that check_all the only code changes were the removal of two debugging hooks (a dump of a page window, a value in a print); `sharp_review_tests` and `backdrop_tests` were run again (green).

Budget used: 6 full bench runs (baseline, p1, the fixed baseline in a second checkout, p2, p3, final), 2 `check_all` runs (the first, at the end of phase 0, polluted: see the top).
