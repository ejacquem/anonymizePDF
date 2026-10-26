# Run 2: sharp, high-contrast, poor-quality scans (and old typed documents)

You are the lead engineer of this run. You work alone and unattended. Nobody answers questions: decide, write the decision
down, go on. The first run (`docs/overnight/PROMPT.md`, `REPORT.md`, `JOURNAL.md`) is merged on `main`: read its REPORT and JOURNAL
first, they say what was tried and what the acceptance rule is, and CLAUDE.md sections 1-2 (rules), 9 (OCR), 10 / 10b (rendering),
10c / 10d (backdrop, dewarp), 12d (the scorecard).

## The problem, with evidence

The owner's real documents are scans of bad quality but STRONG contrast: sharp, high contrast, low resolution, binarised or nearly,
speckle, harsh JPEG, old typewriter text. The replaced text comes out blurry and grey, and sometimes does not come out at all.
Synthetic cases of this are in the scorecard (`sharp_*` five-row pages, `doc_sharp_*` pages, `doc_vieux_*` an old typed letter in
three qualities; `sample_data/Dossier Test 23` has the PDFs, regenerate with `python tools/make_test_challenge.py "sample_data/Dossier Test 23" --only sharp_ --only doc_sharp_ --only doc_vieux`).
Run on main today (owner-reviewed: "very good synthetic tests"):

1. **Blurry fakes.** New bench measure `edge` = strong edges (95th percentile of the grey gradient) of the output over those of the
   original value (under 1 = the fake is softer): `sharp:*` 0.69 (bw200 0.50, thin200 0.41), `doc_sharp_*` and `doc_vieux_*` about 0.5.
   The page fit finds a blur and the fake is painted with it, although the page is a hard cut (mid-tones almost absent) at 150 / 200 dpi:
   `quality.hardness` only covers coarse pages (native scale < 0.5, "blocks").
2. **Nothing replaced, silently.** `doc_vieux_fax` (a fax: 200 x 100 dpi, non square pixels): 0 values replaced, 0 leftovers, 0 suspects, no
   warning: the OCR read nothing, the file is output with every value readable. `doc_vieux_worse` (100 dpi, blocky, speckle): the IBAN and the
   phone number are not replaced (2 leftovers, 1 suspect), a town came out garbled ('12400 Bruxelles'). `doc_sharp_assurance_bw200`: found 25 %.
3. **Wrong font.** Typewriter text (Courier New, fixed pitch, bold terms) gets a sans face (`Hendrickx`, `Rue de la Vallée 25`), and in one
   address the town is written smaller than the street on the same line.
4. **Three open privacy findings of the review of run 1** (see "Phase 0").

## Phase 0 - close the review findings (privacy; at most 90 minutes)

For each one: a test that FAILS on main first (synthetic page, in `tools/check_all.py`, `--fast` set), then the fix.
1. `backdrop.paper_and_ink` (`dark_paper` from the median of the whole window): light text on a thin dark band (a name in white on a navy
   band only about 1.5 text heights tall) keeps its strokes in the 'level' and the original value is repainted under the fake. Decide the polarity
   from the pixels of the box (and its ring), or take both a closing and an opening and remove the ink of either polarity; the box must be
   repainted from background only.
2. `scanpaint._scan_page` skips `dewarp.straighten` when `style.mode == "blackbox"`: on a page turned 10 degrees or more the black boxes land at
   the wrong place (the Windows OCR returns boxes of the straightened page). Run it in blackbox mode too (the boxes are drawn on the flat page and
   brought back); test with a tilted page in blackbox mode: every value is covered.
3. Ghost of the original strokes under the fake with `backdrop.Surface` on blurred / 150 dpi scans (scale >= 0.5 is not guarded; JOURNAL P1-1
   admits a smear on `tone:coarse`). Measure the residual structure of the surface over the value's ink (it must have none); fall back to the
   flat patch of the old code (or a larger closing) when it has. Also guard `scanpaint._erase_patch` (the pixel pass) the same way. Add a bench
   measure or a check_all test on a blurred 150 dpi page.
Also add `try/except` around `dewarp.straighten` and `Flat.compose` so a failure falls back to the old per-word path (log it).

## Phase 1 - read these pages, and never fail silently (this is the biggest gap)

- A scanned page where the image is not square-pixel (`doc_vieux_fax`: 200 x 100 dpi) or is a 100 dpi hard cut is rendered at 300 dpi
  by MuPDF without smoothing, and the OCR (Windows) reads nothing or garbage. Build the OCR reading from the native image: take its real
  resolution per axis (`scanpaint.native_scale` already finds the image), resample it smoothly (bicubic / Lanczos) to square pixels at about
  300 dpi, and add readings on cleaned variants (despeckled, strokes thickened for thin binary text) merged through `ocrpage.merge` like the
  readings of run 1 (only on pages where the first reading is poor: few words for the ink on the page, or implausible words).
- Safety net (small, allowed in `engine.py` and the report, nothing else there): a scanned page that has ink but where the OCR reads (almost)
  no word gets a warning in the job log AND in the report's `warnings` of the file, so the Recap shows it. No silent pass.
- Targets (bench `found`): `doc_vieux_*` >= 90 % (the fax from 0), `doc_sharp_*` >= 85 %, no group of the other 51 cases below its
  baseline.

## Phase 2 - the fake is as hard as the page

- Detect the tone of the page, not only the blur: share of mid-tones among the ink-boundary pixels of the text (the `hardness` idea, at ANY
  resolution), then fit what the scan did: for a hard cut the threshold level (it sets the stroke thickness: a low level thins, a high one
  thickens) and its softness; for a high-contrast scan a steep tone curve (`sigmoid` k and midpoint) applied to the coverage. Extend
  `quality.Quality` (a field the fit fills, default = today's behaviour so soft pages do not change) and paint the fake through it at the native
  grid (render 4x, bring to the native resolution, apply the cut / curve, no blur on a hard page). Replace the weight match of run 1
  (`scanpaint.weight_matched`, a stroke spread) by the threshold level on hard pages.
- Targets: `edge` median >= 0.85 on `sharp:*`, `doc_sharp_*`, `doc_vieux_*`; `ink_rel`, `mass`, `dark`, `height` not worse than the baseline; the soft
  families (`bg`, `deco`, `geom`, `font`, `tone`, `doc_releve/assurance/contrat`) unchanged within the third decimal (their pages are soft: the
  new code must not touch them; check it).

## Phase 3 - the right family and size

- Typewriter text: estimate the character pitch of a block from its long words (equal advances: coefficient of variation under about 3 %)
  and restrict the candidates to the monospace faces (Courier New, Courier Prime, Consolas, Lucida Console... `fontmatch.CATALOG` kinds); bold
  from the usual rule. Look at `PageFonts.face_for`, `block_style`; run 1's note: giving every value the block's family lost 98 % -> 77 % on one
  contract, so use it as a PRIOR for a fixed pitch only.
- Values on one OCR line (a street and its town) share the size and the baseline of their line unless their own measure says otherwise.
- Add a bench measure for the family (the report `trace` `look` string says the face written; the generators know the true family of each
  value) if it is cheap; else check by pictures.
- Optional if time is left: copy the glyphs of the page (digits and letters of a fixed-pitch block, exemplars from clean words) instead of a
  catalog face; measure against the catalog face with `edge` / `ink_rel`; keep only if it wins on held.

## Rules that never bend (same as run 1, with these changes)

- Privacy first, over-redact rather than leak. Never loosen detection or verification: `pii.py`, `fields.py`, `verify.py`, `fake.py` are not touched
  (the glyph-aware fake of run 1 is not needed). `engine.py` only for the Phase 1 warning. numpy + Pillow only, no new dependency, no network.
- No special-casing: nothing in `anonymizer/` mentions a case name, a seed, a value of the generators or a position. Do not edit the existing case
  definitions after the baseline (you may APPEND new cases or variants, say so in the journal); the `sharp_` and `doc_vieux` cases are the
  owner's reference. No real documents (`sample_data/Dossier Test Reel/` is off limits).
- Work in a git worktree on your own branch, commit after each ACCEPTED change with the attribution line the session gives, NEVER push, never
  touch `main`. One heavy run at a time (bench, check_all); long commands in the background.

## Measure and acceptance

`python tools/challenge_bench.py run out.json [--only PREFIX]... [--sheet s.html]`; the full run is now 64 cases (about 10 minutes): use
`--only sharp_ --only doc_sharp_ --only doc_vieux` (about 3 minutes) to iterate, the full run at the end of a phase. Same selection of cases
= same numbers. `python tools/bench_cmp.py before.json after.json --groups ...` prints the verdicts; `tools/sheet_view.py` turns a sheet into
pictures: LOOK at the pictures of the 10 worst values before and after each phase (numbers have blind spots).
A change is accepted when: the target groups improve on dev, `held` is not worse on the same groups, `found` never drops anywhere, `coll` does not
rise, the soft families are unchanged (see Phase 2), `python tools/check_all.py --fast` is green at the end of a phase (about 17 minutes; not after
every experiment), and `tools/bench.py` stays within +15 % of the run-1 final (170.6 s for the 8 documents) unless you say why. Otherwise revert and
log it under "tried and not kept".

## How to work

- First (30 minutes): the baseline. Full run to `docs/overnight/baseline_sharp_full.json` (commit it), then the 20 worst values as pictures; write
  what you see in `docs/overnight/JOURNAL.md` under a heading "Run 2".
- One idea at a time: hypothesis, smallest change, subset run, accept or revert, journal entry, commit. Prefer a general mechanism to thresholds.
- Journal after EVERY experiment (numbers before / after, decision, commit): it is your memory across context windows; reread it at the start of each.
- Add tests for what you keep (`tools/check_all.py`, `--fast` set), update CLAUDE.md (the section of the code you changed) and the 12d scorecard section
  (new measure `edge`, the `sharp_` and `doc_vieux` cases).
- Token thrift (the owner's quota is limited): do not read whole big files when a grep or a line range answers; use the subset runs; at most 6 full
  bench runs and 2 `check_all` runs in the whole run; subagents only for read-only work (describing pictures, reviewing a diff before committing it:
  ask for the leak and over-reach angles).
- Stop rules: a phase ends when its targets are met, or after three experiments in a row that do not improve them, or after its time box (P0 90 min,
  P1 2 h, P2 2 h, P3 1 h 30). The whole run ends after about 7 hours: then run `check_all.py --fast`, commit, tag `sharp-done`, write
  `docs/overnight/REPORT_SHARP.md`. If something is broken and you cannot fix it in 45 minutes, revert to the last tag and go on.

## Deliverables

- the branch with one commit per accepted change and a tag per phase (`sharp-p0` ... `sharp-done`), nothing pushed;
- `docs/overnight/REPORT_SHARP.md`: per phase what was done and a before / after table of the groups that matter from the full bench, pictures that
  changed most (case, row, file), tried and not kept, what is left, risks (what could leak or over-reach), whether `check_all --fast` was green (paste
  the last lines; do not claim what you did not run), time cost;
- the journal complete.
