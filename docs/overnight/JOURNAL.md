# Overnight journal (branch `claude/overnight-quality-74f554` = the `overnight/quality` of the brief)

Method: `tools/challenge_bench.py` (see CLAUDE.md 12d). Full run = 51 cases (45 five-row + 6 `doc_` pages), ~6.5 min. Subset runs
(`--only`) are compared with a baseline of the SAME subset made on the base commit in a second checkout
(`C:\Users\Lucas\Project\_oq_base`, a detached worktree at afb5ebb, outside the repo). Pictures: `tools/sheet_view.py`.
Run files live in `docs/overnight/runs/` (logs and sheets are not committed, the JSONs are small and are).

## B0 baseline (afb5ebb, full run, `baseline_full.json`, 381 s: scan 34 s, anonymize 170 s)

ALL 319 values: found 86 %, det 98 %, bg95 4.3, ink_rel 0.65, coll 0.0 (five-row cases) / 20.8 (doc_). What the worst 12
pictures show (sheet `runs/baseline_sheet.html`):
- under a highlighter the replaced value gets the paper colour from the 3 px ring, which lies outside a tight highlight: a
  white patch with a yellow rim (and the dark `bg95` 90-150 of `hl_yellow` / `hl_blue`);
- on tilted / bent doc pages the fake is written in another font than the text around it (serif italic over a regular
  serif) and its baseline is off; part of the values under a highlighter are not read at their place.

## P1-1 backdrop surface (`anonymizer/backdrop.py`)

Hypothesis: the patch of an erased box is the page's background without the strokes: per channel, the grey CLOSING of a
window around the box (removes what is thinner than ~0.3 text heights, keeps highlighter / stripe / gradient and their
edges) corrected by the smooth bias of the paper pixels (the closing keeps the brightest noise), ink = darker than the
closing in any channel by max(14, 3.5 x grain) grown by 3 px. Only the closing and paper pixels are used, never the
pixels of the strokes (no ghost, checked on the pictures). A first try that averaged the known pixels with Gaussians left
a pale ghost of the letters (the coarse scales draw in the paper outside a tight highlighter): dropped for the closing.
Wired in `scanpaint._scan_page` (erase + ink tails) when the quality match is on.

Results (full run `p1g_full.json` vs `baseline_full.json`; the runs of this step are in `docs/overnight/runs/`, not committed):
ALL bg_excess 0.35 -> 0.16, bg95 4.28 -> 3.40, bg_worst 50.2 -> 19.4, dark 6.05 -> 4.70; doc_* coll 20.9 -> 16.5, doc_releve_bend
bg_excess 7.1 -> 0.64 (bg95 25.5 -> 5.9); deco:hl_yellow bg95 122 -> 12.7, hl_blue 91.7 -> 9.5, dark 26 -> 3; bg:* all better or equal;
found unchanged everywhere. No ghost of the letters on the pictures (flat, paper, highlighters, a coarse 150 dpi copy: a faint
smear stays on the coarse one).
Things found on the way:
- Underline: `bg95` 30.6 stayed. The rule restore (`scanpaint._rules_in`) restored only the core pixels of the rule: the soft
  edge was erased, the line came out thin and light. The mask grows 2 px up and down: bg95 30.8 -> 5.75.
- A closing the size of a stroke leaves a ghost on a coarse / blurred scan (thick strokes); a closing of 0.55 text heights
  removes it but rounds the corners of a tight highlighter (bg95 of the opaque / part highlights +2..3). Kept: the small closing
  (0.3 h) everywhere, the large one only where the small one still keeps something dark in EVERY channel (>= 3 % of the paper:
  a highlighter edge is paper in one or two channels).
- Still worse than the baseline (all under 1 unit of bg95, or a measure on 1-5 values): tone:coarse dx 1.7 -> 3.9 / mass 1.01
  -> 1.06 (the faint smear), tone:binary bg95 0.54 -> 1.58, hl_green_opaque / hl_yellow_opaque bg95 +0.4 / +1.4 (the edge of the
  pasted box on a crisp highlighter), doc mass 1.22 -> 1.28 (the fake is heavier than the original under a highlighter: the weight
  is decided on the highlighted pixels, see P1-2).
- `check_all.py --fast`: 1766 passed, 0 failed (906 s).
Decision: ACCEPTED (the target groups improve on dev and held, found never drops, coll falls, check_all green; the remaining losses
are in the third decimal or tiny absolute values and are listed above).

## P1-2 OCR on the page without its tints (`backdrop.tinted` / `flatten`, `ocrpage.page_words`)

Hypothesis: under a highlighter (magenta, blue, some yellow) the OCR does not read the value at its place; dividing the page
per channel by its local paper level (closing 15 px) turns it into dark ink on white; one more reading on that image, merged
like the others (`ocrpage.merge`), only on pages that have coloured areas (chroma > 70 on > 0.15 % of the page, ~1.5 s a page).
Scratch check on three doc pages: the plain reading garbles or misses the magenta e-mail, the magenta phone, the blue IBANs; the
flat reading gives every one right.
Full run `p1h_full.json` vs `p1g_full.json`: deco:hl_magenta found 0 % -> 100 % (n=4), doc_* found 69 % -> 73 % (doc_releve_bend
60 -> 73 %, doc_assurance_wave 75 -> 81 %, doc_assurance_bendtilt 50 -> 56 %), nothing else moves ('same' on every five-row
group). The medians of ink_rel / dark of the doc groups rise because values that were missed before are now in them (the
magenta ones are the hardest). ACCEPTED (this is a commit of its own, `git log`).

## P1-3 fonts measured on the flattened grey (tried, NOT kept)

Hypothesis: the weight and blur of a value under a highlighter are measured against the highlighter colour, so the ink looks
lighter / heavier; `PageFonts` on the flattened grey would decide them against white paper. Full run `p1i_full.json` vs
`p1h_full.json`: deco:hl_blue dark 30.8 -> 15.3, mass 1.51 -> 1.27, ink_rel 1.36 -> 1.21; doc dark 8.4 -> 5.9 - but
doc_releve_bend found 73 % -> 67 %: one value ('1050 Bruxelles', second place) is not found at its place any more (the match
box of the second place lands on the street line, `pii.match_boxes` cut by the measured advance; a town is left readable and a
fake town written over the street) although nothing in the reading changed. Rule 3 (found never drops) rejects it. The cause
is in how the boxes of a value that spans two OCR lines of a bent page are cut, which phase 2 (flattening bent pages) is
meant to remove: retry after phase 2. Reverted.

## P2-1 pages turned and bent: `anonymizer/dewarp.py`

Hypothesis (from the baseline: `geom:tilt12` found 0 of 5, tilt18 60 %): the Windows OCR straightens a page it finds turned by ~10
degrees or more and gives the boxes of the STRAIGHTENED page, so the fake is painted at the wrong place; the fonts, quality and
paint also assume horizontal lines. Turn the page straight ourselves, read and paint it flat, and bring back only what changed.
Mechanism: the skew of the whole page (`regions.text_angle` on the page reduced 4 times, from 2 degrees), then the bend: the lines
are followed from strip to strip on the reduced page, a vertical displacement field (Gaussians on a 170 px grid + a plane) is
fitted to their centres, used when it makes the rows of ink 20 % more contrasted. Flat page on a larger canvas, `_scan_flat` =
the old `_scan_page` body, records / trace / black boxes mapped back, `Flat.compose` pastes the changed pixels (grown 2 px).
Full runs (p2e vs the phase-1 state p1h): found 88 % -> 95 % (geom 78 -> 100 %: tilt12 0 -> 100, tilt18 60 -> 100, tilt6 80 -> 100,
bend_strong 60 -> 100, bend_tilt 80 -> 100; doc 73 -> 86 %: releve_tilt 73 -> 100, contrat_bend 69 -> 94, releve_bend 73 -> 87,
persp 88 -> 94), `dark` 5.3 -> 4.1, ink_rel 0.65 -> 0.56, dx 3.2 -> 2.4, geom:tilt18 dy 12.3 -> 1.2, dark 33.5 -> 4.9, doc_contrat_bend
coll 26.9 -> 2.5. The pictures of the doc pages (view with tools/sheet_view.py / the page montage) read right: a bent and
tilted contract is replaced line by line where the baseline pasted fakes over the wrong lines.
Not better: bg95 of the doc and the tilted five-row cases +0.2..0.5 (resampled twice where the paint is), mass of tilted / bent
values 0.9 -> 1.1, doc_assurance_bendtilt found 69 % -> 62 % against the rotation-only run (one value, '4000 Liège', measured
0.43 of its box by the bench but found and replaced when the page is run alone), doc_contrat_persp coll 16 -> 160 (a signature
black box now found, wider than the signature: over-redaction).
Things tried on the way (not kept): a field gated by the curvature of the chains (a card of straight lines still got one: tracking
mistakes); the blur of one resampling taken off the fake's blur (`quality.unresample`: `dark` +1.1, no gain); the field applied from
1 degree (`contract_tests` failed: the second check read a fake e-mail back as a leak with a little more blur; page 2 of the contract
is turned by 1.0 degree and the old per-word path is good enough below 2 degrees).
Linear interpolation of the field warp was replaced by a cubic (Catmull-Rom): less blur.
Known: `check_all` (random keys) now fails about 1 key in 5 on `carte_identite_faible_qualite.pdf` ('second check finds nothing
readable', 'Eltoe' = the OCR's reading of the fake first name 'Eline' written in a serif font over the pink SPECIMEN
watermark of a 150 dpi poor copy). 0 of 12 keys on the base commit. The background of the erased name now continues the watermark
(faithful to the card), which makes the OCR of the thin serif fake worse; the pixels differ by 1.5 levels on average. A
chaotic flip of the OCR on a font mismatch (the first name should be written in the bold sans of the card, the font match picks a serif
for it), not a leak; see REPORT.md.

## P3-1 tone curve in `Quality` (tried, NOT kept)

Hypothesis (brief 3c): `tone:boost` / `dark` / `halo` have a nonlinear tone curve; fit it on the pixels (monotone knots from the median scan
darkness at each level of the degraded candidates) and paint through it. On the three pages the fitted knots were the identity to within
the noise (the linear fit of each word, `a ~ c p + b`, already takes the scale), the residual never fell by more than 1 %: no curve kept.
Not kept, nothing committed. A halo (unsharp mask) is not a tone curve: its overshoot is clipped by the coverage normalisation.

## P3-2 weight of the fake matched to the original (`scanpaint.weight_matched`)

Hypothesis: the `dark` (darkness of the ink, 3rd percentile) and `mass` of `font:rockwell` (44, 0.76), `tone:textlayer` (47), `tone:halo`
(32), `tone:dark` (20), `font:bahnschrift` (19) come from the matched face being lighter than the page's font (a slab, a font outside the
catalog) or from a tone that thins the fake: whatever the cause, the ink of the fake is too little. Measure the ink the original value has
(grey page before painting, normalised by the Surface and the ink colour, rules left out), the ink the same text has when written in the
matched face at the page's quality, and move the fake's stroke spread so that the fake's ink is that ratio times its own, only when the ratio is
outside 0.89 .. 1.13.
First version (observed against the paper colour of the look): the values under a highlighter measured 1.5-1.8 (the highlight was ink):
doc_assurance_bendtilt / wave `dark` 7 -> 10 / 5.9 -> 8.3. Against the Surface of the box: fixed.
Full run `p3d_full.json` vs `p2e_full.json` (phase 2): font:rockwell dark 44.3 -> 6.2, mass 0.76 -> 1.03; tone:textlayer dark 46.7 -> 6.9,
mass 0.76 -> 1.06; tone:dark dark 20.5 -> 5.3; tone:halo dark 31.9 -> 14.4, mass 0.87 -> 1.02; font:bahnschrift dark 19.3 -> 5.9; sylfaen 10.5 -> 4.8;
lucida mass 1.27 -> 1.10; font:courier (docs) dark 29.8 -> 7.8, mass 2.25 -> 1.59; family:font dark 4.95 -> 4.21, family:tone 9.99 -> 6.78,
family:doc 4.91 -> 4.40; found unchanged. Slightly worse: rockwell / textlayer ink_rel +0.03..0.06 (the glyph shapes are still the catalog's),
a few `dx` medians of 5 values (franklin 9.8 -> 14.1, doc_contrat_persp `dark` 2.9 -> 4.2), bahnschrift mass 0.98 -> 1.13.
Time: the full bench takes 478 s against 381 s at the baseline (phase 1 flat OCR on coloured pages, phase 2 dewarp, this): see REPORT.md.

## P3-3 the 'Eltoe' flake of `carte_identite_faible_qualite.pdf` found: the Surface of a turned word (fixed)

`check_all` (random keys) failed about one run in three on the poor ID copy (Dossier Test 09): the second check read the fake first
name back as a different word ('Eltoe', 'Ehne', 'Lime'). 0 of 30 random keys at the baseline, 3-9 of 20 with the branch. Bisected with a fixed
set of 16 keys (`Random(1000 + i)`, the same keys on both trees): not the Surface of the main paint path (`FLATSURF`), not the weight match,
not the soft edge of the rules; all three off still failed, so one more difference: `render.paint_tilted` (the words of the card slanted by
more than 1.5 degrees, painted in a turned patch) also erased with a Surface: with it off, 0 of 16 / 0 of 24. In the turned patch the tight
ink band is short, the closing is small, the pink SPECIMEN watermark runs on through the erased name and the OCR misreads the thin serif fake over
it. Decision: `paint_tilted` keeps the flat patch (the page's bend is handled by dewarp; a turned word on a highlighter is rare). A closing taken
from a band grown by 30 or 60 % (the height of an OCR box) did not remove the failures (1 of 16, 2 of 24).
The soft edge of a rule (+-2 px) is now put back only where no ink of a letter is within 2 px (a descender that crosses the line would leave a piece
of the value): bg95 of `deco:underline` stays at 5.7.

## P3-4 speed (tools/bench.py, the 8 documents, scan + anonymize)

First measure of the branch: 159.6 s -> 232.2 s (+45 %). Profile (cProfile of the contract, one process): `dewarp.Field.at` 20 s of 81 (a sum of 280
Gaussians per point over the reduced page), `_warp_rows` / flatten / compose 18 s, on pages the field had been fitted to although they
only are turned by 1 degree (the field was a tilt). Done: the Gaussians are separable on a regular grid, one product of two small matrices
per point (`Field.at`, same values); the gain is evaluated on a coarse grid; a field is used only when it BENDS the page by 16 px or more
once its plane is taken out (`Field.bend`, a tilt is not a bend: the contract pages 14.6 / 15.6 px, the bent pages 17 .. 100 px).
Result: 159.6 -> 170.6 s (+7 %: weight match and Surface). Bench: found / measures unchanged (p3h vs p3f: nothing worse), full run 468 s
against 381 s at the baseline: the five-row cases are all turned or bent or coloured pages by design.
A scalar point in `Field.at` lost the Gaussian part for a while (found 94 -> 56 % on doc_contrat_persp in the intermediate run p3g): fixed.

## P3-5 a page with text slanted line by line is not a bent page

`check_all` (random keys) failed once on `textes_inclines_scan.pdf` (Dossier Test 07, lines slanted 5-15 degrees each): the field (gain 1.33, bend 100 px)
flattened it as if it were bent and the second check read fakes back wrongly. Gate added: the median difference of slope between neighbouring lines
must stay under 0.06 (`dewarp._slope_scatter`: bench pages 0.001-0.041, that page 0.073); the old per-word path keeps such pages
(0 of 16 keys, the baseline 0 of 16). `dewarp_tests` has the synthetic case.


# Run 2 (branch `claude/sharp-quality-32d24f`, brief `PROMPT_SHARP.md`)

Method: as run 1. Full bench = 64 cases (`baseline_sharp_full.json`, 661 s: scan 53 s, anonymize 352 s). Iteration subset = `--only sharp_
--only doc_sharp_ --only doc_vieux` (121 values, ~3.5 min; its own baseline `runs/sub_base.json` made with the Phase 0 code: the fakes
of a case depend on the other cases of the folder, so only runs of the SAME selection are compared). Pictures: `tools/sheet_view.py`.
`check_all.py` budget: 2 runs. The first one (Phase 0) was started in the background and I kept editing `ocrpage.py` while it ran, so jobs
that started later imported the half-done Phase 1 code (1796 passed, 0 failed anyway); the Phase 0 tag is therefore verified only by that
run and by the end-of-run one.

## R2-B0 baseline (code of main + generator fix)

- Generator fix (not a case definition): `make_test_challenge.write_pdf` placed the scan with `insert_image(page.rect)` whose default
  `keep_proportion=True` squashed the 200 x 100 dpi fax into the middle band of the page (half height, text flat): `doc_vieux_fax` was an
  artefact, 0 found because the page was not what the generator meant. Now `keep_proportion=False` (the fax fills the page: non-square pixels).
  With it the fax is read (206 words) and found 69 %.
- Full baseline (440 values): found 86 %, edge 0.81 overall (ALL), `family:sharp` 35 values found 100 % edge 0.69, `doc_sharp_*` found 25-87 %
  (`assurance_bw200` 25 %), `doc_vieux_*` bad 62 / worse 46 / fax 0 (squashed, see above).
- What the worst pictures show: the fake is a grey, soft sans text where the page is hard, black and typed (Courier): `Rue de la Vallée 25`
  in a sans face, blurred; thin serif page (bw200) whose strokes are broken: the OCR reads 'Ihauss'e de N',unur'. Highlighted lines of
  `doc_sharp_contrat_contrast120` OK in colour but the fake is a regular sans over a bold serif.
- `det` (the config has a replacement for the value) caps `found`: `doc_vieux_bad` / `fax` det 77 %, `doc_sharp_assurance_bw200` 75 %: 'Claire
  Dubois' (a signer), dates and the company's street are not detected by `pii.py` (frozen in this run), so the 90 % target of the brief for
  `doc_vieux_*` cannot be reached by the pipeline alone.

## R2-P0 three review findings (commit 36eeb97, tag sharp-p0)

1. `backdrop.is_dark_paper`: the polarity is the median of the value's own box, not of the window: white text on a navy band 1.5 text heights
   tall (mostly light window) is now opened, not closed. Test: lightest pixel of the erased box 255 -> < 60.
2. `scanpaint._scan_page`: `dewarp.straighten` also runs in black-box mode (the boxes are drawn on the flat page and brought back); try/except around
   `straighten` and the flat paint + `compose` (the fallback redoes the page the old way and forgets the records of the failed attempt, log line).
   Test (OCR): geom_tilt12 in black-box mode, the ink of every value is under black: 1 of 5 -> 5 of 5.
3. `backdrop.ghost_of`: the surface over the strongest ink pixels of the box against the surface right next to the strokes; above 4 % of the
   contrast a larger closing (0.9 heights), else no surface (the flat patch of the old code). A blurred 150 dpi copy (x2 bilinear + Gaussian 3 px):
   the darkest pixel left in the erased box 23 -> < 14; the same through `_erase_patch` (pictures of digital pages). A sharp page keeps its surface.
All in `sharp_review_tests` (fails on the previous code: 4 of 6 checks). `backdrop_tests`, `dewarp_tests`, `underline_tests`, `background_quality_tests` green.

## R2-P1 reading (commit after P0)

Hypothesis: the OCR reads coarse pages badly (stairs of repeated pixels), broken thin strokes badly; extra readings on cleaned variants repair it.
- Block factor from the pixels (`ocrpage.block_factors`: MuPDF draws a scan enlarged more than ~1.3 times without interpolation, so only 1/f of the
  column / row boundaries next to ink ever change; fax 1.5 x 3, 100 dpi 3 x 3, 150 dpi 2 x 2; 200 dpi pages drawn bilinear: 1). Image only, so the
  output pages are read the same way by the checks.
- First try (always add `smooth` readings on a coarse page): found 69 -> 66 % on the subset (`doc_vieux_bad` 54 -> 38 %), no gain: the OCR's
  mistakes on these pages are not the stairs. Reverted.
- Single-reading token test on 6 pages (tokens of the truth values read exactly): base 26 / 27 / 21 / 29 / 31 / 40 of 41 / 33 / 35 / 35 / 39 / 40;
  `min3` (grey erosion 3 x 3 = thicker strokes) 30 / 31 / 23 / 26 / 32 / 39, 'native' (back to 200 dpi) 28 / 30 / 23 / 26 / 30 / 39; merged
  min3 + min5 + native 32 / 31 / 22 / 32 / 31 / 40 (not monotone: min5 hurts the fax). Kept: thick + native (+ smooth when blocky).
- Gate: the share of base words another existing reading gives back (`ocrpage.agreement`): clean pages 96-100 %, the six poor pages 81-94 %; below 93 %
  (and >= 20 words) the extra readings are made. The share of implausible tokens is NOT a gate (clean statements have 5-11 %).
- Result on the subset (p1b vs sub_base): found 69 -> 71 % (`assurance_bw200` 25 -> 38 %, `worse` 38 -> 46 %, `bad` 54 = 54, `fax` 69 = 69), det
  unchanged. `coll` 2.0 -> 2.8 (doc_vieux 2 -> 6): looked at: not painted collateral, values of OTHER documents of the folder found on this page
  (an N/Réf `98/0417` replaced), and the signature black box of `bw200` that was there before. Time: anonymize 126 -> 154 s on the subset (poor pages
  read 2-3 times more).
- The misses left are reading errors of single characters ('+32' read '4.32', 'Namur' read 'N',unur'); a value the OCR garbles is not found whatever
  the variants, and matching a garbled reading against the config is `pii.py` (frozen).
- Safety net: `scanpaint._unread_warning` (ink >= 0.3 % of the page and fewer than max(5, 8 x ink %) words): log line + `extras.warnings` -> the file's
  `warnings` in the report (`engine._finish_page`, `entry["warnings"]`), so the Recap shows it. Tests: `sharp_read_tests`.

## R2-P0b the ghost guard on highlighters (commits 1d14018, 3c83798)

The first guard (flat patch when `ghost_of` > 4 %) was too eager on highlighted values of turned / bent pages: the full run p1_full showed
`bg_worst` 4.9 -> 21 on `doc_releve_tilt`, 6.2 -> 20.8 `doc_contrat_bend`, 7.6 -> 22.9 `doc_contrat_persp`, `hl_blue` 5 -> 19 (a white box in a
highlighter: private but ugly, and the acceptance rule says soft families do not move). Looked at the window pictures:
- the blue band's ghost was real but small (a 10 % darker smear on the digits, a page resampled by the dewarp) and the metric also counted the white
  page beyond the rim of the highlighter as 'paper' next to the strokes: the ring is now only the pixels of the same colour (within 0.25 of the
  strokes' contrast of the surface at the strokes);
- the larger closing (0.9 heights) removes a thin highlighter altogether (white blobs): the check now also sees HOLES (`ghost_parts`: smear, hole);
- a fallback between the two, `_fill_around_ink`: around the strokes (dark in EVERY channel, grown 5 px) each pixel takes the lightest level of its
  neighbours within 8 px that is at most 0.2 of the contrast above its own (a bounded rise: a rim of another colour is out of reach), then a
  little smoothing. First tries (normalised Gaussian average over the paper: the white of the page beyond the band; the same with a max filter of
  similar colours, 1 s a value) were wrong or slow; the bounded rise is 0.07 s.
- Ladder: closing 0.55 (<= 4 %), larger closing 0.9 (<= 4 %), the lifted level; none clean: the least bad if its defect is under 12 %, else the
  flat patch. A synthetic blurred page (blur 3) still gets the larger closing (0.10 -> 0.0), 150 dpi blur 4 as well.
Result (p3_full vs the fixed baseline): `bg_worst` doc_contrat_bend 6.2 -> 6.3, doc_releve_bend 18.9 -> 6.3 (after the last tolerance change, run alone),
doc_releve_tilt 4.9 -> 4.9. Left: `doc_contrat_persp` row 4 (yellow, 'Solène Wauters': smear 16 % / 12.5 % / 12.2 %) gets the flat patch, bg 22.
Privacy first: kept (a 15 % limit would have let it through with a faint shadow of the strokes).

## R2-P2 the fake is as hard as the page

Reading of the problem: the output page is the 300 dpi render of the scan, so unreplaced text keeps its hard edges; the replaced text is drawn with
a soft model (fitted blur, box average to the native grid and bilinear back) and has softer edges. The fit residual cannot see it (the letter shapes
differ more than the edges: tone curves never lowered it, grid of 36 curves x blurs on `sharp_bw150`: 0.0204 -> 0.0199).
Measure: `edge_rel` (new, `challenge_bench.context_edge`): edge of the fake over the edge of the original, divided by the same ratio for the rest of
the output page (the mean of the 3000 strongest gradients away from the values). The original `edge` has a ceiling: the clean ideal page has
edge(ideal)/edge(original) 1.00-1.11, but our output page is a 300 dpi JPEG, so a perfect fake would score less than 1: `edge_rel` is the fair one.
Method: `quality.Quality.steep` / `mid` (a sigmoid on the coverage of the native pixels, `_native_aligned` on the scan's grid and bilinear between
native centres: a first version boxed the native pixels from the sample's corner, off the grid, and the hard cut had no effect), fit by the strong
edges of the page's words (`fit_tone`), the weight match moves `mid`.
- Gate: first any page with the scan's edges 1.2 x the candidates': font_* and tone_* soft five-row pages passed it (ratios 1.06-1.43, their
  `ink_rel` 0.58 -> 0.64, `dark` 3.9 -> 5.1: the soft families moved, REJECTED). Final gate: ratio >= 1.5, or >= 1.2 with a mid-tone share
  under 0.35 among the ink pixels (`hardness` at any scale: hard pages 0.10-0.31, soft 0.50-0.70), and linear blur <= 1.9: `family:font`,
  `bg`, `geom`, `deco` show no change in the full run, tone: tone_binary / halo get a curve (dark 7.9 -> 4.9).
- Mass: the first selection (edges, then the best residual) gave a heavier fake (mass 1.03 -> 1.12); choosing among the candidates whose edges are
  within 12 % the one whose ink equals the fit's without a curve: 1.05. `weight_matched` also on coarse pages with a curve.
Results on the iteration subset (p2_off -> p3_a, 121 values): `edge_rel` 0.63 -> 0.97 (sharp family 0.91 -> 0.99, docs 0.48 -> 0.96), `edge` 0.57 ->
1.01, `dark` 17.0 -> 12.2, `height` 1.04 -> 1.00; `ink_rel` 0.68 -> 0.69 and `mass` 1.03 -> 1.05 a hair worse (a sharp fake is not better
aligned than a soft one, the glyph shapes are the catalog's), `found` 71 % = 71 %. The pictures (`sheet_view`): '6700 Arlon', 'Chaussée du Stade 94,'
hard and black on the fax, as the neighbours.
Not kept: a curve for a coarse page whose blur is large (`doc_vieux_worse`: blur 3.2 at 100 dpi = 1 native pixel): `edge_rel` 0.66 -> 0.88,
`ink_rel` 1.30 -> 1.23, but `dark` 20 -> 31 and one value of the page lost its coverage (found 46 -> 38 %: rule 'found never drops').

## R2-P3 family and size

- Pitch from the OCR boxes does not work (width / characters CV 0.19-0.22 on the typed letters, 0.16-0.34 on proportional pages: the boxes are too
  noisy); the face votes do: `PageFonts.mono_page` (share of the 40 longest words best written in a monospace face: typed 0.35-0.57, proportional
  0.00-0.30, `MONO_SHARE` 0.33). `face_for` restricts the families to monospace ones on such a page. The faces written on the letters: before,
  Courier + Verdana, DejaVu Sans, Georgia, Barlow among the values; after, Courier New / Courier Prime / Consolas only (`doc_vieux_bad`: all
  Courier). Look at the fax: 'Chaussée du Stade 94,' in Courier. Consolas still comes up on `worse` (the OCR box of a blocky word decides).
- Size: a value of fewer than 9 letters takes the median size of its line's other words when its own differs by more than 6 %
  (`metrics`, `SIZE_LINE_*`). No measurable change on the bench (the effect is on a town written smaller than its street, rare).
- Family measure: not built (the pictures and the `written in` log lines do it); `lucida` five-row pages `ink_rel` 0.86 -> 0.37 (monospace prior).
- Optional glyph copying: not tried (no time, and `edge_rel` is already ~1).
- Tried and not kept: 'thicker' (MinFilter(5)) extra reading: found unchanged, +10 % time.

## R2-final

Full run `final_sharp_full.json` vs `baseline_sharp_fixed.json`: found 87 -> 88 % (0 values lost, 5 gained), edge 0.80 -> 0.84, edge_rel 1.00, dark 4.8 -> 4.4; soft families
unchanged except the intended gains. `tools/bench.py` 170.6 -> 168.6 s. `check_all --fast` on 3c83798: 1813 passed, 0 failed. Two debugging hooks (a dump of a surface window, a value
printed by `ANON_DEBUG_SURFACE`) were removed afterwards; the four new test groups and `backdrop_tests` re-run green (28 checks). Report: `REPORT_SHARP.md`.


# Stage A (branch `claude/stage-a-5e52b1`, from `claude/sharp-quality-32d24f` 5e85cc7; brief: close the review findings of run 2)

Method: a test that fails on the old code first (`stage_a_backdrop_tests`, `stage_a_font_tests`, `stage_a_scan_tests` in `tools/check_all.py`, all in
`--fast`), then the fix. Bench: `challenge_bench.py` on one fixed selection (`--only deco_ bg_ doc_ tone_ sharp_ font_`, 390 values; geom_ left out), the base
run made on an export of 5e85cc7, the others on exports of the commits named below. Pictures: `sheet_view.py`.

## A1 the ghost guard fails open / A2 12 % accepted / A3 polarity (commit 8f4be45, then 9834f9c and ddcfaf8)

Old code, `stage_a_backdrop_tests`: 15 of 17 checks failed. A dense bold value on a blurred 100-150 dpi copy: darkest residual -75 / -41 / -57 levels
(the box showed the original strokes at 20-30 % of their contrast) with the rung 'closing 0.000' (cannot measure = clean). Two more ways to read clean
that the finding did not name: `near_here` empty (a smear above 25 % of the contrast found no ring pixel of the colour of the level and reported 0), and a
solid heavy value whose ink mask (made against the level that follows it) is almost empty (a ring searched INSIDE the blob).
- Fix: `ghost_parts` returns a `Ghost` in grey levels, infinite when it cannot measure (contrast < 2 x MIN_INK, fewer than 8 strong pixels, no ring even grown
  around the box, nothing of the level's colour next to the strokes). The strokes the page shows (dark in every channel against the paper just outside the box)
  count as ink for the ring; the paper of the contrast is the larger of the box's and the ring's. No `GHOST_LAST`: clean = 4 % of the contrast (a few levels of
  grain allowed: `max(4 %, min(6, 4 % + 2))`, the review asked that faint copies do not get 6 of 28 levels). None clean: no surface; the box is flat, with the
  ring colour of the caller, or, in a tint (a highlighter that ends at the box: the ring is the white of the page), with the area's colour (`flat_colour`).
- First version of `flat_colour` took the box + 6 px: a blue highlighter hugging the text came out white (bg 21 on `doc_releve_tilt`); now the 80th percentile of the
  non-stroke pixels INSIDE the box, a tint only when its chroma differs by > 20 or it is lighter than the ring by > 12 (a smear darkens and keeps the hue: first version
  painted yellow bands 15 levels too dark).
- A3: `is_dark_paper` reads the ring 1-4 px outside the box. The review of the diff (leak angle) found a regression: a band exactly as tall as the box has the light page
  above and below, so the ring alone called it light; the left / right (above / below) sides now decide too (test written first, it failed).
- After: 20 of 20 pass (+ `sharp_review_tests`, `backdrop_tests` green). Bench, base -> last, same selection: `found` identical on all 390 values (0 lost, 0 gained);
  bg_, sharp_, tone_ families bit for bit the same; deco `bg_worst` 4.53 -> 7.59 (`hl_half`: a highlighter that covers half of the value is now one flat colour, no
  shadow of the letters in it), `bg_excess` 0.63 -> 0.71; `doc_contrat_persp` `bg_worst` 22.9 -> 7.6 (the known leftover of run 2, a yellow row, now keeps its
  colour), `doc_releve_bend` 6.2 -> 9.3, `doc_releve_tilt` 4.9 -> 6.3, `doc_contrat_bend` 6.3 -> 7.1. Looked at the highlighter and `hl_` pictures of the last sheet:
  yellow and blue bands keep their colour, no shadow of the digits.
- Tried and dropped: tint colour from the box + 6 px (white hole in a blue band), from the median (too dark on blur).

## A4 fonts (commits f4be0d9, b1fdd5e)

Old code: a proportional paragraph beside a bigger Courier block took Consolas (`mono_page` share 0.6); a short figure beside bigger words took the line's size
(23.8 px for 20). `mono_page` -> `mono_block` (vote on the block of the value's words, else its line, else, when nothing around it tells, the page); prior = a bonus
`MONO_BONUS` x evidence weight on the monospace families, no filter. First version (no page fallback): `doc_vieux_bad` 8 of 9 replaced values in Courier faces (a Verdana among them: the two-word blocks of an address have no vote), fax 7 of 11; with the page
fallback (the page's vote only when nothing around the value tells; local evidence always wins) `doc_vieux_bad` 9 of 9 (as before), fax 8 of 11 (it was 12 of 12 with the whole-page rule; the 3 others have a block that votes
proportional: DejaVu Sans x2, Georgia Bold), `font_lucida` 4 of 5 (5 of 5 with the whole-page rule). (Counted with a script on the trace `look` of the report, run on an export of 5e85cc7 and on the branch.) Bench: `font:lucida` `ink_rel` 0.37 -> 0.56 (the run-2 gain was 0.86 -> 0.37), `font:courier` `dark` 5.8 -> 6.8,
`doc_vieux_fax` `dark` 3.9 -> 4.4, `coll` 5.3 -> 5.7; `found` unchanged. The `SIZE_LINE_*` rule is removed (no measurable change on the bench).

## A5 (commit dc9b669)

Old code: duplicate warning and numbers after a failed `compose`, a painting error swallowed (both tests failed). `_scan_page`: only `straighten` / `flatten` and the way back are in
the try; `_mark` / `_rollback`. `ocrpage.agreement` indexed (same value on 40 random readings). `ANON_DEBUG_SURFACE` removed, `ANON_TONE` kept and documented (test with it off). The two
backslash-broken one-liners (+ one in `quality.py`) rewritten, `backdrop.py` docstring updated.

# Run P (branch `worktree-agent-a11dd171726032f77`, from main d2ca872; brief `PROMPT_PLACEMENT.md`): placement, coverage, size

## P1 stage 1: measures, `place_` cases, baseline, diagnosis (tag `runP-s1`)

Measures (`challenge_bench.measure_place`, on the value's own band; the other values' boxes, the words the run replaced on purpose (report
boxes of other items) and the clean page's other words are left out): `resid` = share of the original value's ink still in the output (a
connected part of the output's ink >= 85 % on the original's ink AND still the original scan's pixels, median |out - orig| < 22, outside the
ideal's ink grown 1 px), `resid_px`, `resid_max`; `x0` (fake's left ink edge - ideal's), `x1err` (fake's right edge - original's; summary
`x1over` = share > 3 px); `base` (median of column bottoms, fake - ideal); `xh` (baseline - median column top, fake / ideal); `nbr` (share of
the neighbour words' ink within one line height changed by > 40 levels); `boxerr` (report box vs the original's ink, + = misses ink;
`boxmiss` = worst side). `--sheet-by place` (worst placement first; 6th panel: fake ink blue, remnant red). `compare` and `bench_cmp` know
them. Truth masks cut to their bbox (a 51-value page held 200 MB of full-page masks; the process reached 7.4 GB).
Validated on a synthetic case (the ideal shifted 3 px with the original's last 30 columns pasted back: x0 3, resid 0.12; the ideal: all 0)
and on the pictures. Each of these was a false defect in the first runs and is fixed: the ideal ignored match_case (a value in capitals vs a
Title fake), the fake read at the noise level (10) and the ideal at 20 (every fake 1 px wider each side: `xh` 1.10 everywhere -> 1.00),
neighbour lines / labels / other values' fakes taken for the fake (x0 = -PAD, base +17), a fake glyph sitting on the original's strokes
taken for a remnant. The runs before the last fix are kept as `runP_base_*_xhbiased.json` (their old columns are valid).

`place_` (`make_test_documents.place_cases`, 16 pages x 51 values, 8 dev / 8 held, held frozen from 31bdc1d): a dense statement (header,
address, info block, two paragraphs, a 22-row table whose counterpart IBAN column ends 14 px before the amounts, 7 pt footer), 150 dpi JPEG
q 60-66 4:2:0, Arial / Calibri / Verdana / Times 7-10 pt, leading 1.0-1.25, labels glued ('IBAN:', 'N°', 'Tél.', 'Réf:', '(réf.X)'), turned
0.3 / 0.7 / 1.2 / 1.8 degrees, a 9 px bend, a dark lid border with the page off-centre in white margins, /Rotate 90, a CropBox offset, two
image strips, 150 x 136 dpi. The displayed PDF = the truth page (mean |pdf - truth| 2.0; border 4.3 for its white margins, strips 2.8).
H6 (page geometry): rotate / crop / strips / margin / scale place the fakes like the plain pages (medians 0-1 px, no boxmiss): not broken.

Baseline (`runP_base_place.json`, place_ alone, 593 s; `runP_base_full.json`, 80 cases, 1398 s). place_: found 83 % (det 83 %); medians
resid 0.000, |x0| 1, |base| 0, xh 1.00, nbr 0, boxmiss 0. Tails of the 680 found: resid > 0.02: 26, |x0| > 4: 7, |base| >= 3: 9, |xh - 1| >
0.15: 13, nbr > 0.02: 10, boxmiss > 2: 7. Old families (391 found of 440): resid > 0.02: 10, |x0| > 4: 22 (20 on doc_), |base| >= 3: 10,
|xh - 1| > 0.15: 14, nbr > 0.02: 9, boxmiss > 2: 20 (most without a remnant: the ink tail or a neighbour box covered it).

Diagnosis (sheet pictures): the synthetic 150 dpi statements are mostly placed right; what the owner sees on real files is not reproduced
at scale. Causes found, by count: detection misses (136 place_ values, out of scope); the right end of a shorter fake leaves a piece of the
original's last glyph or the fake's last glyph is spread onto it (most of the 26 resid, x1err < 0: 'Chaussée de Namur 31', 'NL45 ... 28',
e-mails ending '.be' where the same letter sits at the same place, a measure limit); a fake starting 10-16 px right of the value (13: 'Rue des
Tanneurs 14' at the start of a wrapped line in 5 place_ pages, '5000 Namur' / '4000 Liège' in doc_contrat_*); tight leading 1.0: report box
spans two lines (boxerr top -22) and the fake's size / baseline follow it (6, place_tight_verdana9 rows 7-10, glued_verdana8 row 3); a glued
label erased with the value ('E-mail:' + address, nbr 0.44; 'IBAN:BE48' box misses 25-44 px of ink on the left: 4); weight: Times / Verdana
capitals written heavier (seen, not counted by these measures). Decision: stage 2 waits for the lead.
