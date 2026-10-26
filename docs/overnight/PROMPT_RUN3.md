# Run 3: detection gaps, speed, leftovers (8 hours at most)

You are the lead engineer of this run. You work alone and unattended; the owner's lead session supervises and may send you a message:
answer it by acting on it. Nobody answers questions: decide, write the decision down, go on. `main` (13e0594) holds run 1 (backgrounds, tilt,
bend), run 2 (sharp / poor scans, typewriter text) and Stage A (the privacy findings of the review of run 2). Read first (grep the headings,
do not read CLAUDE.md in one go): `docs/overnight/REPORT.md`, `REPORT_SHARP.md`, `REPORT_STAGE_A.md` and the end of `JOURNAL.md`; CLAUDE.md
sections 2 (rules), 5 / 7 (pipeline, detection), 9 (OCR), 12 / 12d (tests, the scorecard).

## Time budget (hard stop at 8 hours; the report is written even if a stage is unfinished)

| time | work | priority |
|---|---|---|
| 0.5 h | Phase 0: baseline + six frozen held-out cases | must |
| 3.5 h | Stage B: detection, the garbled-value leftover check, the three random-key failures | must |
| 2 h | Stage D: speed | high |
| 1.5 h | Stage C + F: the worst page, typed text in proportional faces, the highlighter step | only if time is left, cut first |
| 0.5 h | Final: full bench, `check_all --fast`, report | must |

If you are late, drop C + F first, then shorten D. Never drop the final step.

## Phase 0 - baseline and frozen held-out cases (30 minutes)

1. Full bench on main (`python tools/challenge_bench.py run docs/overnight/run3_baseline_full.json`, 64 cases) and `tools/bench.py` (the 8
   documents, speed) to `docs/overnight/run3_speed_base.json`. Commit both.
2. APPEND six held-out generalisation cases (family `gen_`, split `held`; new file section in `tools/make_test_documents.py` /
   `make_test_challenge.py`, do not edit existing cases): whole pages with other persons / values, other fonts (a typed page in Times, a
   Verdana letter, a statement in Calibri with a Courier block), other scan settings than the existing cases (170 dpi hard cut, 130 dpi high
   contrast, a 204 x 98 dpi fax, a 120 dpi soft copy with speckle). They are FROZEN the moment they are committed: run them now, record the baseline
   (`docs/overnight/run3_gen_base.json`), do NOT tune on them, run them again at the end. They are the check that the work generalises.

## Stage B - detection and what is read back (3.5 hours; the owner allows it, with guardrails)

`pii.py` and `verify.py` are NOT frozen in this run; `fake.py` and `fields.py` stay untouched (`fields.py` only if a rule below cannot be done
without it: say why in the journal). `engine.py` only for new report fields. GUARDRAILS: detection may only WIDEN (never remove a detector, never
raise a threshold of an existing one); every new rule has a test in `detector_tests` (or a new group in the `--fast` set) that fails before; all
existing detector / KEEP tests pass unchanged; over-redaction must not flood the non-PII text: count the replaced words on the KEEP documents
(Dossier Test 14 / 15) and on `python tools/check_public_docs.py --quick` before and after: no new replaced common word, and the total number of
replaced values on the public documents may not rise by more than 5 %.

B1. The three random-key failures of `check_all --fast` ('second check finds nothing readable': PERSON 'IDF' on `carte_identite_faible_qualite.pdf`,
STREET_ADDRESS 'Avenue de la S ... 132' on `contrat_assurance_scan.pdf`, 'Avenue de Ila' on `releve_150_scan.pdf`): the second check reads a FAKE back with OCR
errors and reports it as a leak. The key of that run is `%TEMP%\anonymizePDF_check_keys\20261026-210500-key0.key` if it still exists (see `tools/check_all.py
--key`, `tools/key_hunt.py`). Make the read-back tolerant (`verify.remove_fakes`, `_read_back`, `_ocr_fold`: a fake read with a letter garbled, a street whose
name is a fake word) WITHOUT letting a real original value through: add tests with the three strings AND with real originals one or two characters
garbled that must still be flagged. Measure: replay the failing key and 20 other fixed keys on the three folders (`--folder`, `--key`) before / after: the
failure rate must fall to 0 of 20 and no leak test may be loosened.

B2. Detection gaps seen on the old typed letters and the pages of `doc_*` (the `det` column of the bench: values the config has no replacement for):
- a reference after a policy / contract / file label (`Police n° AUT-9035127`, `HAB-5120368`, `n° de police`, `contrat n°`): three capital letters, a dash, six to eight
  digits, also with OCR garbling (`HAB-51 20368`, `HAB.5120368`); NOT the firm's own reference `N/Réf. : 98/0417` (that is not the client's data);
- a person's name in a signature block: the line(s) under a closing formula or a role line (`Le directeur,`, `La gestionnaire de dossiers`, `Pour le
  conseil,`) and after labels such as `Gestionnaire :`; capitalised words, a known first name or a title; not a company;
- a street + postcode + town in a letterhead on one line separated by dashes or bars (`Rue du Pont 12 - 5000 Namur`): find out why it is missed (`det` 77 % on
  `doc_vieux_*`) and fix the cause;
- do NOT start replacing dates (kept by design except birth dates).

B3. A leftover check for garbled values (privacy-positive, only warns): in `verify`, for the config values of long kinds (IBAN, phone, e-mail, account /
national numbers, 8 characters or more) look in the OUTPUT's OCR reading for words or lines within 1-2 edits (after the OCR folding already used) of the value
that are not within the same distance of a fake: report them as suspects of a new kind ('near an original value') with their place (`hits`), so the Recap
shows them. False positives: zero new warnings on the clean outputs of the 64 bench cases and of the `check_all` sample folders.

Targets: `det` on `doc_vieux_*` and `doc_sharp_*` from 77 % to at least 90 % (no value that was found is lost: `found` never drops); the bookman / courier
reference values found; `check_all --fast` green (with the keys above). Phase end: `check_all --fast` once, commit, tag `run3-b`.

## Stage D - speed (2 hours)

Run 1 + 2 + Stage A made `anonymize` slower (run 1 final: 170.6 s for the 8 documents of `tools/bench.py`; measure main now: `run3_speed_base.json`).
Profile (`cProfile` of `run_anonymize` on `contrat_assurance_scan.pdf` and on a `doc_vieux` page: the top 5 costs), then cut: the background surface is rebuilt
per value (windows of neighbouring values overlap: build one per line / per region, or cache by window), `weight_matched` renders the text about 11 times per
value (cache the coverage, scale it), the three candidate surfaces of `Surface._clean` (stop at the first clean one: check it does), the tone fit
(`quality.fit_tone`, a grid of 36 curves x 10 samples: gate earlier or reduce the grid), the extra OCR readings on a poor page (only the ones that gain),
`tinted` / `flatten` per page. Target: remove at least half of the added cost, i.e. within +10 % of 170.6 s, with RESULTS UNCHANGED: a pure speed-up must give
IDENTICAL results with `tools/snapshot_results.py save` before / after and `compare` (see CLAUDE.md 11b); a change that is not exact (a cache with a different
numeric path) must pass the bench with no measure moving beyond the third decimal and be said so. Tag `run3-d`.

## Stage C + F - only if time is left (1.5 hours)

C. The worst page `doc_vieux_worse` (100 dpi blocky, speckle): found 38 %, `mass` 1.53, `height` 1.38, `edge_rel` 0.29. One diagnostic pass (the pictures of
`tools/sheet_view.py`): where do the weight and the height come from (blocks of the native grid, the OCR box of a blocky word, the fit)? Fix if it is a bug, else
write it down as a limit. F. On a typed page values whose own block votes proportional are written in a proportional face (`doc_vieux_fax` 3 of 11): let the
vote of neighbouring blocks (same column / same size) back the monospace bonus. The half-highlighted area is one flat colour with a visible step: try the
area's gradient only if it is cheap. Same acceptance as always.

## Rules that never bend

- Privacy first: over-redact rather than leak. Never weaken a check; the flat patch is always an acceptable fallback. No new dependency (numpy + Pillow), no network,
  no real documents (`sample_data/Dossier Test Reel/` is off limits). No special-casing: nothing in `anonymizer/` mentions a case name, a seed, a value of the
  generators or a position. Do not edit existing case definitions (append only, say so).
- Work in a git worktree on your own branch; commit after each ACCEPTED change with the attribution line the session gives; NEVER push, never touch `main`.
- One heavy run at a time (bench, check_all, snapshot, bench.py); long commands in the background; do not poll.
- Token thrift (the owner's quota is limited): grep and line ranges instead of whole files; subset bench runs to iterate (`--only ...`, about 3 minutes), the full
  run only at the end of a stage; at most 8 full bench runs, 3 `check_all` runs; subagents only to read pictures or to review your own diff once per stage (ask for the
  leak and over-reach angles).

## Acceptance (every change)

The target group improves; `found` never drops anywhere; `held` and the frozen `gen_` cases not worse; the soft families (`bg`, `deco`, `geom`, `font`, `tone`,
`doc_releve/assurance/contrat`) unchanged beyond the third decimal unless the change is meant to move them (say so, with numbers); `python tools/check_all.py --fast`
green at the end of each stage (about 17-24 minutes). Otherwise revert and log it under 'tried and not kept'. Look at pictures (`tools/sheet_view.py`) of the ten
worst values before and after a stage: the numbers have blind spots.

## How to work

Journal after EVERY experiment in `docs/overnight/JOURNAL.md`, heading 'Run 3' (hypothesis, numbers before / after, decision, commit): your memory across context
windows; read it again at the start of each. Tests for everything you keep (`tools/check_all.py`, `--fast` set), CLAUDE.md updated where behaviour changed.

## Deliverables

Commits, tags `run3-b`, `run3-d`, `run3-done`; `docs/overnight/REPORT_RUN3.md`: per stage what was done, a before / after table of the groups that matter from the
full bench and of the frozen `gen_` cases, the speed table, the pictures that changed most, what was tried and not kept, what is left, the risks (what could leak or
over-reach, especially the new detection rules and the read-back change), `check_all --fast` result with its last lines pasted (do not claim what you did not run).
