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

TABLE

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
  `paragraphes_scan.pdf`): it reports a value it cannot place. 0 of 30 keys on the baseline, now see below; the cause of the worst one was the Surface in the
  turned patch of `paint_tilted` (removed, see JOURNAL P3-3).

## check_all --fast

STATUS
