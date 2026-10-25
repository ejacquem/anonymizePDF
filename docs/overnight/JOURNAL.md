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
