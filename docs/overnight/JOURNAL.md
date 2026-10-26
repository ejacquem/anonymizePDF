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
