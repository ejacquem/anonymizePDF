# Overnight run: quality of the replaced text (phases 1 to 3) - report

Branch `claude/overnight-quality-74f554` (the `overnight/quality` of the brief), tags `overnight-phase-1`, `overnight-phase-2`
(the phase tags are on the commit of each phase; later commits fix what the checks showed), `overnight-phase-3` (the last
commit). Nothing was pushed, `main` was not touched. The journal of every experiment, with its numbers: `JOURNAL.md`.

## In one page

| phase | what | result (full bench, `baseline_full.json` -> `final_full.json`) |
|---|---|---|
| 1 | the box of a replaced value is erased to the background the page would show (closing per channel, `backdrop.py`), not one flat colour; the OCR reads coloured pages once more on the page divided by its paper | highlighters: `deco:hl_yellow` bg95 122 -> 19, `hl_blue` 92 -> 10, `hl_magenta` found 0 -> 100 %, `hl_blue` found 33 -> 83 %; underline bg95 31 -> 6; `doc_releve_bend` bg_excess 7.1 -> 0.7 |
| 2 | a page turned by 2 degrees or more, or bent, is turned straight to be read and painted, only the changed pixels go back on the scan (`dewarp.py`) | `geom` found 78 -> 100 % (`tilt12` 0 -> 100, `tilt18` 60 -> 100, `bend_strong` 60 -> 100); documents found 69 -> 86 %; `geom` dark 17 -> 3 |
| 3 | the fake is as heavy as the text it replaces (`scanpaint.weight_matched`) | `font:rockwell` dark 46 -> 7, `tone:textlayer` 47 -> 7, `bahnschrift` 18 -> 6, `lucida` mass 1.25 -> 1.10 |

ALL (319 values): found 86 % -> 95 %, bg_excess 0.35 -> 0.17, bg95 4.3 -> 3.4, ink_rel 0.65 -> 0.56, dark 6.1 -> 3.7, dx 3.2 -> 2.4;
dev 89 -> 97 % found, held 83 -> 92 %.

```
python tools/challenge_bench.py run out.json [--only PREFIX]... [--sheet s.html]      # the scorecard (CLAUDE.md 12d)
python tools/bench_cmp.py before.json after.json --groups case: geom:                  # before / after, with the acceptance verdicts
python tools/sheet_view.py s.html out_prefix --match "deco=hl_blue"                    # the worst values as PNG montages
python tools/check_all.py --fast                                                        # the gate
python tools/bench.py run out.json --repeat 1 ; python tools/bench.py compare a.json b.json   # speed
```

## The table (full run, 51 cases, 319 values; the full list is in `final_full.json`)

| group | n | found | bg_excess | bg95 | ink_rel | mass | dark | dy | dx | coll |
|---|---|---|---|---|---|---|---|---|---|---|
| ALL | 319 | 86% -> 95% | 0.35 -> 0.17 | 4.3 -> 3.4 | 0.65 -> 0.56 | 1.05 -> 1.07 | 6.1 -> 3.7 | 0.22 -> 0.18 | 3.2 -> 2.4 | 0.0 -> 0.0 |
| split:dev | 187 | 89% -> 97% | 0.30 -> 0.16 | 3.9 -> 3.3 | 0.58 -> 0.52 | 1.05 -> 1.07 | 5.1 -> 3.8 | 0.20 -> 0.17 | 3.0 -> 2.2 | 0.0 -> 0.0 |
| split:held | 132 | 83% -> 92% | 0.43 -> 0.19 | 4.7 -> 3.7 | 0.71 -> 0.67 | 1.05 -> 1.08 | 7.9 -> 3.6 | 0.25 -> 0.22 | 3.3 -> 2.5 | 0.0 -> 0.0 |
| family:bg | 45 | 100% -> 100% | 0.13 -> 0.13 | 3.4 -> 3.2 | 0.49 -> 0.49 | 1.03 -> 1.04 | 4.2 -> 3.7 | 0.18 -> 0.17 | 1.9 -> 1.9 | 0.0 -> 0.0 |
| family:deco | 25 | 92% -> 92% | 0.71 -> 0.71 | 6.9 -> 6.1 | 0.57 -> 0.55 | 1.06 -> 1.06 | 3.6 -> 3.1 | 0.14 -> 0.13 | 2.4 -> 2.0 | 0.0 -> 0.0 |
| family:geom | 50 | 78% -> 100% | 0.09 -> 0.03 | 3.4 -> 3.3 | 0.72 -> 0.66 | 0.92 -> 1.12 | 17.2 -> 3.1 | 0.46 -> 0.33 | 4.5 -> 2.2 | 0.0 -> 0.0 |
| family:font | 65 | 98% -> 98% | 0.17 -> -0.01 | 3.7 -> 3.0 | 0.61 -> 0.61 | 1.06 -> 1.08 | 5.7 -> 3.7 | 0.18 -> 0.16 | 4.4 -> 3.7 | 0.0 -> 0.0 |
| family:tone | 40 | 100% -> 100% | 0.43 -> 0.35 | 3.9 -> 3.5 | 0.47 -> 0.47 | 1.00 -> 1.04 | 10.8 -> 6.8 | 0.12 -> 0.10 | 2.1 -> 1.9 | 0.0 -> 0.0 |
| family:doc | 94 | 69% -> 86% | 0.86 -> 0.76 | 5.8 -> 5.8 | 0.90 -> 0.67 | 1.22 -> 1.12 | 7.4 -> 3.9 | 0.88 -> 0.32 | 4.0 -> 2.2 | 20.8 -> 17.3 |
| deco:underline | 1 | 100% -> 100% | 3.17 -> 0.80 | 30.6 -> 6.1 | 0.67 -> 0.61 | 0.89 -> 0.92 | 21.2 -> 20.1 | 0.19 -> 0.06 | 5.9 -> 5.6 | 0.0 -> 0.0 |
| deco:hl_yellow | 7 | 71% -> 71% | 13.18 -> 2.04 | 122.0 -> 18.6 | 1.30 -> 0.49 | 0.97 -> 1.05 | 26.4 -> 3.9 | 3.36 -> 0.62 | 4.7 -> 2.2 | 20.8 -> 17.3 |
| deco:hl_blue | 6 | 33% -> 83% | 9.09 -> 1.24 | 91.7 -> 10.1 | 2.03 -> 0.93 | 0.99 -> 1.36 | 29.8 -> 1.0 | 3.68 -> 0.30 | 3.2 -> 1.8 | 11.8 -> 17.3 |
| deco:hl_magenta | 4 | 0% -> 100% | - -> 1.63 | - -> 9.8 | - -> 0.56 | - -> 1.17 | - -> 1.6 | - -> 0.68 | - -> 1.9 | - -> 11.2 |
| geom:tilt12 | 5 | 0% -> 100% | - -> 0.10 | - -> 3.4 | - -> 0.44 | - -> 1.18 | - -> 6.9 | - -> 0.48 | - -> 2.5 | - -> 0.0 |
| geom:tilt18 | 5 | 60% -> 100% | 0.59 -> 0.04 | 6.2 -> 3.2 | 1.58 -> 0.49 | 1.38 -> 1.15 | 31.5 -> 4.9 | 3.66 -> 1.15 | 12.7 -> 2.2 | 36.2 -> 0.0 |
| geom:bend_strong | 5 | 60% -> 100% | 0.93 -> 0.02 | 8.5 -> 3.1 | 1.03 -> 0.81 | 1.12 -> 1.07 | 24.0 -> 3.7 | 2.21 -> 0.29 | 9.5 -> 3.4 | 0.0 -> 0.5 |
| geom:wave | 5 | 100% -> 100% | 0.08 -> -0.00 | 3.4 -> 3.2 | 0.73 -> 0.68 | 0.90 -> 1.11 | 16.3 -> 3.0 | 0.61 -> 0.40 | 3.0 -> 6.9 | 0.0 -> 0.0 |
| geom:persp | 5 | 100% -> 100% | 0.10 -> 0.02 | 3.5 -> 3.3 | 0.66 -> 0.68 | 0.81 -> 1.15 | 39.9 -> 3.6 | 0.45 -> 0.71 | 3.4 -> 6.6 | 0.0 -> 0.0 |
| font:rockwell | 10 | 100% -> 100% | 0.48 -> 0.28 | 3.2 -> 3.1 | 0.66 -> 0.67 | 0.75 -> 1.03 | 45.6 -> 7.3 | 0.11 -> 0.17 | 4.9 -> 2.8 | 0.0 -> 0.0 |
| font:bahnschrift | 5 | 100% -> 100% | 0.24 -> -0.01 | 3.7 -> 3.1 | 0.76 -> 0.75 | 0.96 -> 1.13 | 18.4 -> 5.9 | 0.16 -> 0.16 | 8.5 -> 6.7 | 0.0 -> 0.0 |
| font:lucida | 5 | 80% -> 80% | 0.06 -> -0.07 | 3.6 -> 2.9 | 0.91 -> 0.86 | 1.25 -> 1.10 | 9.4 -> 7.8 | 0.13 -> 0.14 | 15.4 -> 11.2 | 0.0 -> 0.0 |
| tone:textlayer | 5 | 100% -> 100% | 0.54 -> 0.52 | 3.2 -> 3.2 | 0.77 -> 0.83 | 0.75 -> 1.06 | 47.2 -> 6.9 | 0.05 -> 0.03 | 5.2 -> 2.1 | 0.0 -> 0.0 |
| tone:halo | 5 | 100% -> 100% | -0.18 -> -0.21 | 4.5 -> 4.2 | 0.74 -> 0.73 | 0.90 -> 0.89 | 31.1 -> 31.5 | 0.21 -> 0.20 | 1.7 -> 1.7 | 0.0 -> 0.0 |
| tone:dark | 5 | 100% -> 100% | 1.03 -> 0.46 | 4.7 -> 3.4 | 0.35 -> 0.34 | 0.94 -> 0.95 | 21.5 -> 20.5 | 0.05 -> 0.06 | 2.2 -> 1.9 | 0.0 -> 0.0 |
| tone:boost | 5 | 100% -> 100% | 0.56 -> 0.55 | 3.1 -> 3.0 | 0.46 -> 0.46 | 1.01 -> 1.02 | 14.1 -> 14.3 | 0.15 -> 0.12 | 2.9 -> 2.5 | 0.0 -> 0.0 |
| tone:coarse | 5 | 100% -> 100% | 0.25 -> 0.15 | 4.8 -> 4.6 | 0.47 -> 0.47 | 1.01 -> 1.06 | 2.1 -> 2.2 | 0.15 -> 0.13 | 1.7 -> 3.9 | 0.0 -> 0.0 |
| case:doc_releve_bend | 15 | 60% -> 87% | 7.14 -> 0.68 | 25.5 -> 7.0 | 1.09 -> 0.64 | 1.05 -> 1.03 | 7.0 -> 4.7 | 1.30 -> 0.63 | 3.4 -> 2.8 | 19.2 -> 17.3 |
| case:doc_contrat_bend | 16 | 69% -> 94% | 0.72 -> 0.77 | 5.5 -> 5.8 | 0.99 -> 0.67 | 1.39 -> 1.07 | 8.5 -> 2.3 | 1.67 -> 0.28 | 4.8 -> 3.3 | 46.1 -> 2.3 |
| case:doc_releve_tilt | 15 | 73% -> 100% | 0.56 -> 0.86 | 5.8 -> 6.4 | 0.45 -> 0.27 | 1.24 -> 1.13 | 6.7 -> 2.4 | 0.48 -> 0.15 | 1.6 -> 0.9 | 20.8 -> 18.8 |


`final_sheet.html` / `baseline_sheet.html`: the 80 worst values before and after (original, clean page, ideal, output, difference).
The pictures that changed most (case, row, value; bg95 before -> after): `doc_releve_tilt` row 0 'Solène Wauters' (yellow highlighter) 173 -> 12;
`doc_releve_bend` row 0 'Mathis Verhaegen' (yellow) 178 -> 19, row 1 'Rue des Tanneurs 14' 173 -> 15, row 9 'Étienne Lambert' 149 -> 9;
`doc_assurance_wave` row 5 'Rue des Tanneurs 14' 112 -> 7; `doc_assurance_bendtilt` row 7 (blue IBAN) 111 -> 11; `deco_rules` row 0 (underline)
31 -> 6; `geom_tilt12` all five values (not found -> found), `geom_tilt18`, `geom_bend_strong`. Whole pages (Flat / bent / tilted contract and
letter): the output pages of `doc_contrat_persp` and `doc_assurance_bendtilt` read line by line where the baseline pasted fakes over
the wrong lines.

## Phase 1 - background and decorations

- `anonymizer/backdrop.py`, `Surface`: per channel, a grey closing of a window around the box (0.3 text heights; 0.55 where the small
  one still keeps something dark in every channel, which is what a blurred or coarse stroke leaves), the ink (darker than the closing in any
  channel, grown 3 px) is never used, a smooth bias from the paper pixels. A yellow or magenta highlighter is paper, a highlighter edge and a
  stripe edge stay, no ghost of the letters (looked at on pictures, `backdrop_tests`).
- Rules (underline, frame): the soft edge of a rule is put back with it, never next to the ink of a letter (`scanpaint._rules_in`): underline
  bg95 31 -> 6.
- OCR under a highlighter: pages with coloured areas (chroma > 70 on > 0.15 % of the page) are read once more on the page divided by its local paper
  level (`backdrop.flatten`, merged by `ocrpage.merge`): magenta found 0 -> 100 %.
- Not kept: Gaussian averages of the paper (a pale ghost), fonts measured on the flattened grey (one value lost on a bent page, to retry now that bent
  pages are flattened), a thicker rule restore (no change). Not done: the multiply blend of the fake over a highlighter (the measured ink
  colour on the highlight does the job: `deco:hl_*` dark 26..30 -> 1..4); `strike` and `stamp` (the OCR does not read them: out of scope as the brief says).

## Phase 2 - geometry

- `anonymizer/dewarp.py`: skew of the whole page (projection profile on the page reduced 4 times, from 2 degrees), then the bend (the lines are followed
  from strip to strip, a vertical displacement field of Gaussians is fitted to their centres; used when it bends the page by 16 px or more once its plane
  is taken out, makes the rows of text 20 % more contrasted, and the neighbouring lines lean alike). `scanpaint._scan_page` runs the old body (`_scan_flat`) on
  the flat page, maps the records / trace / black boxes back, and `Flat.compose` pastes only the changed pixels. The Windows OCR straightens a page
  it finds turned by 10 degrees or more and returns the boxes of the straightened page: that is why `tilt12` was found 0 of 5.
- Speed: first +45 % on `tools/bench.py` (the field), after the work in JOURNAL P3-4 +7 % (159.6 s -> 170.6 s, 8 documents); the full challenge bench takes
  468 s against 381 s because all its pages are turned, bent or coloured.
- Not kept: the field gated by curvature (a card still got one), taking one resampling's blur off the fake, the field from 1 degree (`contract_tests` red).
- Known: the pages are resampled twice where something was painted (bg95 of tilted five-row cases +0.2), a signature on a bent page can get a wide black box (over-redaction,
  `doc_contrat_persp` coll 16 -> 160), `doc_assurance_bendtilt` loses one measured value ('4000 Liège', its twin gained; the page run alone replaces both).

## Phase 3 - fonts and tone

- Kept: `scanpaint.weight_matched` (see the one-page table). The ink of the original value over the ink of the same text written in the matched face at the page's
  quality; the stroke spread of the fake moves so that the fake's ink is that ratio times its own, only when they differ by more than 15 %, on one-line values,
  not on coarse (block) scans.
- Not kept: a tone curve in `Quality` (the fitted knots are the identity within the noise); glyph copying from the page (not tried: the weight match recovers
  most of the `dark` gap, and the catalog still has no way to copy letter shapes); more free fonts (no network, and a licensed font file cannot be written from memory;
  adding the Windows fonts the bench uses to the catalog would be tuning on the test set). `tone:halo` / `tone:boost` dark (31 / 14) are still open: a halo is an overshoot the
  coverage normalisation clips.

## Risks (what could leak or over-reach)

- Nothing touches detection, verification, `fake.py`, `pii.py`, `fields.py`, `engine.py`. A value the OCR does not read is still not replaced.
- Surface: the box is repainted entirely from the closing and the paper pixels, never from the pixels of the strokes (checked on pictures and by `backdrop_tests`); the
  original ink inside a box is erased as before. The soft edge of a rule is restored from the original pixels but never next to letter ink.
- Dewarp: a wrong field would move a fake to the wrong line (a leak of the true value). It is used only for a clear bend, with three gates; the maps are tested back and
  forth, and the report boxes are mapped back. A page where it fails falls back to the old path (None). Over-redaction: black boxes on a flat page are mapped back as
  the box of the corners (a little larger).
- The second check (`verify.leak_suspects`) misreads a fake in a few percent of the random keys on coarse or poor scans (`carte_identite_faible_qualite.pdf`,
  `paragraphes_scan.pdf`): it reports a value it cannot place. 0 of 30 keys on the baseline; the cause of the worst one was the Surface in the
  turned patch of `paint_tilted` (removed, see JOURNAL P3-3).

## check_all --fast

`python tools/check_all.py --fast` on the last commit (`overnight-phase-3`): **green, 1790 passed, 0 failed (1010 s)**
(`docs/overnight/runs/check_p3c.log`, not committed). Along the way it was red several times and what it showed is in JOURNAL.md: a fake e-mail read back
as a leak after a dewarp of a page turned by 1 degree (the field is now used only for a real bend), the Surface in the turned patch of `paint_tilted`
(the poor ID copy, 1 key in 5), the weight match of a two-line value, a page with slanted lines (`textes_inclines_scan.pdf`) flattened as if bent. The random keys
of `check_all` change at each run: a failure of the second check on `carte_identite_faible_qualite.pdf` or `paragraphes_scan.pdf` (a fake read back wrongly,
`hits` empty) can still appear for a key in a few tens (0 of 24 / 0 of 24 / 0 of 16 on the fixed key sets of the last state); replay with
`python tools/check_all.py --key <file> --folder "Dossier Test 09"`.

