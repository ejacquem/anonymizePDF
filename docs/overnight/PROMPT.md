# Overnight run: quality of the replaced text, phases 1 to 3

You are the lead engineer of this run. You work alone and unattended for several hours. Nobody answers questions, so
decide, write the decision down, and go on. Read `CLAUDE.md` first (sections 1-2 rules, 10 / 10b rendering, 11b speed,
12 / 12d tests and the scorecard) and `docs/challenge_baseline.md`.

## Goal

Make the text that replaces personal data on SCANNED pages (and on pictures inside digital pages) look like it was
there: same background, same decorations, same geometry, same font look. Three phases, in this order:

1. **Background and decorations.** Today the box of a value is erased with ONE flat colour (`render.background_color`: the
   median of a 3 px ring, `quality.erase`: that colour plus grain). Underlines, specks, borders, a highlighter edge, a
   gradient or a stripe in the ring change the colour of the patch, and the patch shows. Replace the flat fill with a
   reconstruction of what the background would be without the text (classify ink against local paper per channel, not in
   grey, so yellow and magenta highlighter are not paper; fit a smooth surface to the background pixels only, robustly;
   follow horizontal bands row by row; correct the box boundary so there is no seam; keep rules and borders, which are
   handled by `scanpaint._rules_in` / `_restore_rules`). The fake text is laid with a multiply blend where the original
   is under a highlighter. Same for `_pixel_pass`. READING comes first: on the 6 `doc_` pages the values under a highlighter
   are mostly not read at their place by the OCR (magenta 0 of 4, blue 2 of 6, yellow 4 of 6, `docs/challenge_baseline.md`):
   normalise the page by its local paper colour per channel before the grey image the OCR reads (`ocr.py` / `ocrpage.py`,
   one more reading in `ocrpage.merge` is fine), and measure `found` on `deco:hl_*` and `doc_*`.
2. **Geometry.** Tilted text is still buggy and bent / warped text is not handled. The scan of a whole page that is tilted
   up to about 18 degrees must be found and replaced (`geom:tilt12` is found 0 of 5 times today); a page bent like a
   book, waved or in perspective must be flattened to be read and painted: estimate a smooth field from the baselines
   of the text lines, run the existing pipeline on the flattened page (OCR, fonts, quality, painting), and bring back
   only the pixels that changed (as `render.paint_tilted` already does for a tilt).
3. **Fonts and tone.** Fonts that are not in the catalog, and scans whose contrast was boosted, binarised or sharpened.
   Try, measure, keep only what wins: (a) copying the glyphs of the page itself for what it has (digits first: take the
   components of clean OCR words whose character count equals their component count, one exemplar per character and
   block, paste at the measured advances, fall back to the matched font for a missing glyph; prefer fakes made of
   letters the page has: `Faker` already picks names by width); (b) a few more free fonts in `anonymizer/fonts` (licence
   file in `fonts/licenses`) only if the held-out fonts improve; (c) a halo / tone-curve term in `quality.Quality`
   fitted on stroke edge profiles. Never take a font from an invisible text layer.

Hard limits: `strike` and `stamp` values that the OCR does not read at all are out of scope unless a cheap general fix
shows (note it in the journal and move on).

## Rules that never bend

- Privacy first (CLAUDE.md 2): over-redact rather than leak. A black box removes data from the file. You do NOT touch
  detection or verification: `pii.py`, `fields.py`, `verify.py`, the config logic in `engine.py`, `fake.py` (except the
  glyph-aware choice of a fake in phase 3, which must keep every existing rule: no original value, no reserved word).
  You may change `scanpaint.py`, `quality.py`, `render.py`, `fontmatch.py`, `regions.py`, `layout.py`, `morph.py`,
  `ocrpage.py`, `ocr.py` (the reading of rotated / bent pages), and add modules.
- numpy + Pillow only for image work (no OpenCV, no scipy, no PyTorch). No new network access, no download.
- Never commit real documents, configs, reports, outputs, keys. Never open `sample_data/Dossier Test Reel/`. Do not delete
  anything in `sample_data/` (the user's own outputs may be there).
- Work in a git worktree on a branch `overnight/quality`. Commit after each ACCEPTED change (CLAUDE.md 2), with the
  attribution line the session gives. NEVER push, never touch `main`, never rewrite history.
- No special-casing: nothing in `anonymizer/` may mention a case name, a seed, a value of the generators or a pixel
  position. The generators are not the app: do not edit existing case definitions after the baseline run (that moves the
  goalposts); you MAY append new cases or variants (new seeds, other fonts) to `tools/make_test_challenge.py` and
  `tools/make_test_documents.py` when the measure needs it, and say so in the journal.
- Every speed measure stays: results must not get slower than +20 % on `tools/bench.py` documents without a note and a reason.

## The measure and the acceptance rule

`python tools/challenge_bench.py run out.json [--only PREFIX]... [--split dev|held]` (CLAUDE.md 12d). 45 five-row cases (`dev` or
`held`) plus 6 whole-page documents (`doc_`: bank statement, insurance letter, lease; mixed fonts, bold / italic, tinted
rows, highlighter, tilt / bend / wave, 3 dev and 3 held). Numbers are deterministic for the same selection of cases and
only comparable for the same selection. The full run is about 9 minutes; use `--only` families to iterate (about 1 min).

A change is ACCEPTED when all of these hold, on a full run before / after (`challenge_bench.py compare`):
1. the group you target improves on `dev` (for example `deco:*` bg95 and bg_excess in phase 1, `found` and `dy` of `geom:*`
   in phase 2, `ink_rel` / `mass` / `dark` of `font:*` and `tone:*` in phase 3);
2. `held` does NOT get worse on any measure of the same group, and `doc_*` (both splits) does not get worse on `found`,
   `bg95`, `ink_rel`, `coll`; a loss on a group you did not target is a rejection unless it is under noise (compare two
   runs of the same code to see the noise: the bench is deterministic, so any difference is real, but judge in the
   sense of the 3rd decimal);
3. `found` never drops anywhere (a value not found is a leak risk) and `coll` (collateral change) does not rise;
4. `python tools/check_all.py --fast` is green (it takes 8-20 minutes: run it at the end of a phase and before the commit
   of a risky change, not after every experiment), and `tools/snapshot_results.py compare` shows no new leftover or
   suspect on the sample folders (outputs may differ in pixels; configs, reports' leftovers and suspects must not get worse).
Otherwise: revert, write the idea and its numbers under "tried and not kept" in the journal, and go on.

Look at pictures, not only numbers: `--sheet sheet.html` writes the worst values (original, clean, ideal, output, diff).
Read the crops of at least the 10 worst before and after a phase (the metric has blind spots: `bg_excess` is diluted, trust
`bg95` and `bg_worst`; `ink_rel` has a floor near 0.5 because the letters of the fake differ from the ideal's).

## How to work

- First: baseline. Run the full bench, copy `out.json` to `docs/overnight/baseline_full.json`, read the 20 worst values of
  the sheet, write what you see in the journal. Run the five documents' sheet and look at the pages.
- One idea at a time: hypothesis -> smallest change -> subset run -> full run -> accept or revert -> journal -> commit.
  Prefer a general, simple mechanism to a pile of thresholds. Keep the code in the style of the surrounding code.
- Journal: append to `docs/overnight/JOURNAL.md` after EVERY experiment (id, hypothesis, files, numbers before / after for the
  groups that matter, decision, commit hash). Read the journal again at the start of each context window: it is your memory.
- Add tests for what you keep: a unit group in `tools/check_all.py` (see `underline_tests`, `memo_tests` for the style), in
  the `--fast` set. Update CLAUDE.md (the section of the code you changed) and `docs/font_matching.md` or a new doc.
- Machine: one heavy run at a time (bench, check_all, snapshot_results); never two at once, it makes both slow and can
  run out of memory (workers use ~800 MB each). Run long commands in the background and wait for the notification.
- Subagents: use them for read-only work that would flood your context (reading sheets and describing what is wrong,
  surveying a technique, reviewing a diff with fresh eyes before you commit: ask for the bugs and for any place the change
  could leak or over-reach). A subagent that writes code works in its own worktree and does not run the benches; you
  merge its result and measure it yourself. Do not run subagents in parallel on the same files.
- Stop rules: a phase ends when its target groups stop improving after three experiments in a row, or after about 3 hours,
  whichever comes first; then run `check_all.py --fast`, commit, tag `overnight-phase-N`, and start the next phase. The whole
  run ends after phase 3 or after about 10 hours. If something is broken and you cannot fix it in 45 minutes, revert to the
  last tag and go on with the next idea.

## Deliverables (what the owner reads in the morning)

- the branch `overnight/quality` with one commit per accepted change and a tag per phase, nothing pushed;
- `docs/overnight/REPORT.md`: per phase what was done, the before / after table of the groups that matter (full bench), the
  pictures that changed most (name the case and value, the sheet file), what was tried and not kept, what is left, risks
  (what could leak or over-reach), how to run the checks;
- `docs/overnight/JOURNAL.md` complete, and `check_all.py --fast` green on the last tag (say so with the output's last lines, or say
  plainly that it was not green and why).
