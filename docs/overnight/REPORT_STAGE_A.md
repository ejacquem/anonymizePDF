# Stage A: the review findings of run 2 - report

Branch `claude/stage-a-5e52b1` (from `claude/sharp-quality-32d24f` 5e85cc7). Nothing merged, nothing pushed, `main` and the run 2 branch untouched. `pii.py`, `fields.py`,
`verify.py`, `fake.py` and `engine.py` were not touched. Journal with the numbers: `docs/overnight/JOURNAL.md` ("Stage A"). Tag `stagea-done` on the last commit.

Commits: `8f4be45` A1-A3 (one commit: one code path), `f4be0d9` + `b1fdd5e` A4, `dc9b669` A5, `9834f9c` flat colour of a tint (A2, found by the bench), `ddcfaf8`
polarity of a tight band + faint copies (found by the review of the diff), then the documentation commit.

## What each test showed on the old code, and the fix

New groups in `tools/check_all.py`, all in `--fast`: `stage_a_backdrop_tests` (20 checks), `stage_a_font_tests` (5), `stage_a_scan_tests` (5). The tests were written first.

**A1 guard fails open.** Old code: 15 of the 17 first checks failed. A dense bold value on a blurred 100-150 dpi copy kept its strokes under the fake (darkest residual -75, -41, -57 levels;
rung 'closing 0.000'). Two more fail-open paths turned up: a smear above 25 % of the contrast found no ring pixel "of the colour of the level" and counted as 0, and a solid heavy value
whose ink mask (made against the level, which follows it) is empty had its ring searched inside the blob. Fix: `ghost_parts` returns a `Ghost` in grey levels, infinite when it cannot measure
(low contrast, < 8 strong pixels, no ring even grown around the box, nothing of the level's colour next to the strokes); the strokes the page shows count as ink for the ring.

**A2 12 % accepted.** `GHOST_LAST` is gone. Clean = 4 % of the contrast, with a few levels of grain (`max(4 %, min(6, 4 % + 2))`: the review of the diff pointed out that a flat 6 levels is 20 % of
a faint copy's strokes). Nothing clean: no surface; the box is flat, in the ring colour of the caller, or, in a tint (a highlighter that ends at the box), in the area's own colour
(`flat_colour`: 80th percentile of the box's non-stroke pixels, a tint only when its chroma differs by > 20 or it is > 12 levels lighter than the ring). Test: std of the erased box against
the page without the value < 2 levels on white and on a yellow band (it was 2.6-4.3, max 13-21 on the old code), and a blue highlighter that ends at the box keeps its colour.

**A3 polarity.** `is_dark_paper` reads the ring 1-4 px outside the box (a heavy black blurred value was "dark paper", 235 levels of the original stayed). The review of the diff found a
regression of that fix: a band exactly as tall as the box has the light page above and below; the left / right (above / below) sides of the ring now decide too. Both directions are tested
(heavy black on white; white on a thin navy band, and on a band as tall as the box).

**A4 fonts.** Old code: a proportional paragraph beside a bigger Courier block took Consolas; a short figure among bigger words took the line's size (23.8 px for 20). `mono_page` is now
`mono_block` (block of the value's words, else its line, else, when nothing around it tells, the page: local evidence always wins) and a bonus on the monospace families, not a filter.
`SIZE_LINE_*` removed. Cost, measured by counting the faces written (`doc_vieux_*`, `font_lucida`; script run on 5e85cc7 and on the branch): `doc_vieux_bad` 9 of 9 in Courier faces (as before),
`doc_vieux_fax` 8 of 11 (was 12 of 12), `font_lucida` 4 of 5 (was 5 of 5). Without the page fallback it was 8 of 9 and 7 of 11. The values that lose are in blocks whose own vote is proportional.

**A5.** Tests: a failed `compose` left a duplicate warning and the failed attempt's numbers; a painting error was hidden by the second pass. Now only `straighten` / `flatten` and the way back are
in the try, `_mark` / `_rollback` forget records, every list of the extras and the numberer; an error of `_scan_flat` propagates (the file ends in error, as before the fallback existed).
`agreement` indexed (equal on 40 random readings); `ANON_DEBUG_SURFACE` removed; `ANON_TONE` kept, documented in CLAUDE.md, tested off; the two broken one-liners (and a third in `quality.py`) rewritten; `backdrop.py` docstring rewritten.

## Bench: `challenge_bench.py run`, selection `--only deco_ --only bg_ --only doc_ --only tone_ --only sharp_ --only font_` (390 values; `geom_` left out)

Base = export of 5e85cc7 (`docs/overnight/stagea_base_sel.json`), last = export of `ddcfaf8` (`stagea_last_sel.json`). Same selection, so the same folder.

- **`found`: identical on all 390 values (0 lost, 0 gained).** `bg_`, `sharp_`, `tone_` families: bit for bit the same.
- Backdrop (A1-A3): `deco` `bg_worst` 4.53 -> 7.59 and `bg_excess` 0.63 -> 0.71 (the highlighter that covers half of the value is now one flat colour: no shadow of the letters, but a
  visible step in the half-highlighted area); `doc_contrat_persp` `bg_worst` 22.9 -> 7.6 (the known leftover of run 2: a yellow row now keeps its colour); `doc_releve_bend` 6.2 -> 9.3,
  `doc_releve_tilt` 4.9 -> 6.3, `doc_contrat_bend` 6.3 -> 7.1; `dark` of `doc_contrat_bend` 3.8 -> 2.3. A first version of `flat_colour` gave 21 on `doc_releve_tilt` (white hole in a blue band): fixed.
- Fonts (A4): `font:lucida` `ink_rel` 0.37 -> 0.56 (run 2 had 0.86 -> 0.37 with the whole-page rule), `font:courier` `dark` 5.8 -> 6.8, `doc_vieux_fax` `dark` 3.9 -> 4.4, `coll` 5.3 -> 5.7.
- Pictures looked at (`sheet_view.py`, last sheet): the yellow and blue highlighter rows keep their colour with no shadow of the digits; the worst `doc_vieux` values (a proportional italic
  fake on the typed page `worse`). The picture of the sheet before the tint fix showed the white hole in the blue band that led to the `flat_colour` rewrite.

## check_all

`python tools/check_all.py --fast` on the last code commit (export of `ddcfaf8`): **1842 passed, 3 failed (1423 s). Not green.** The three failures are `second check finds nothing readable` on
`carte_identite_faible_qualite.pdf` ('IDF'), `contrat_assurance_scan.pdf` ('Avenue de la S ... 132') and `releve_150_scan.pdf` ('Avenue de Ila'), all with the random key of that run
(kept at `%TEMP%\anonymizePDF_check_keys\20261026-210500-key0.key`). I replayed that key with the three folders on an export of the UNTOUCHED 5e85cc7: the same three checks fail there (837 passed, 5 failed:
those three plus two checks of the contract / font groups that the `--key` replay runs with that key), so they are key-dependent failures of run 2, not caused by Stage A. The same three folders plus the unit
groups with two other random keys on the last commit: **855 passed, 0 failed (647 s)**. The new groups passed in the full run. I did not run a second full `--fast`.

```
1842 passed, 3 failed (1423s)
  FAILED: carte_identite_faible_qualite.pdf: second check finds nothing readable   [PERSON 'IDF']
  FAILED: contrat_assurance_scan.pdf: second check finds nothing readable          [STREET_ADDRESS 'Avenue de la S ... 132']
  FAILED: releve_150_scan.pdf: second check finds nothing readable                 [STREET_ADDRESS 'Avenue de Ila']
855 passed, 0 failed (647s)      # Dossier Test 09 / 17 / 20, 2 other random keys, unit groups
```

## Review of the diff (one subagent, leak and over-reach angles) and what I did with it

Fixed: tight-band polarity regression (test first), faint-copy ghost allowance. Left, on purpose: (3) an error of the painting no longer falls back to the per-word path (the brief asked for that: it ends the
file in error instead of hiding it); (4) log lines of the failed attempt stay in the log (cosmetic); (5) the tint surface is a constant window, so an ink-tail erase just outside the box paints the tint there;
(7) the bias map uses the ink mask, not the page-seen strokes (the level is validated, the bias is smooth and small); (8) `MONO_MIN_WORDS` 8 -> 4 is part of the per-block vote (blocks are smaller than pages).

## What is left

- `doc_vieux_fax`: 3 of 11 values in a proportional face on a typed page (their block votes proportional). A smoothing across neighbouring blocks would bring them back; not done.
- The flat patch in a half-highlighted area has a visible step (privacy over looks).
- The three key-dependent second-check failures above exist on the run 2 code too (`Avenue de Ila` is a fake street read back by the OCR as a street); that is `verify.py` / OCR read-back territory (frozen here).
- `engine.py:1207` still has a backslash-broken one-liner (engine.py was off limits).
