# Font and quality matching (branch `feature/scan-font-matching`)

Goal: a replacement written on a scanned page (or in an image of a digital page) looks like the text around it:
same typeface, same weight, same print / scan quality. Today a scan replacement is always Arial (regular / bold),
crisp, on a flat paper-coloured rectangle; image text on digital pages is Helvetica vector text.

Digital text layers already reuse the embedded / installed font (`render.FontBook`): not changed.

## 1. Fonts available (`anonymizer/fontmatch.py`, `anonymizer/fonts/`)

- Catalog of families with their regular / bold files: the Windows fonts found like today (registry HKLM + HKCU,
  `C:\Windows\Fonts`): Arial, Times New Roman, Calibri, Cambria, Georgia, Verdana, Tahoma, Segoe UI, Trebuchet,
  Courier New, Consolas, Garamond, Century Gothic... plus a few free fonts bundled in `anonymizer/fonts/`
  (OFL / Bitstream licences, licence files next to them): DejaVu Sans / Serif, Roboto, Open Sans, Lato, Source Sans 3,
  Barlow (DIN-like), Courier Prime. A family that is not on the PC is skipped.

## 2. Font detector (analysis by synthesis)

- For a scanned word (OCR text + box): the ink box inside the OCR box, the ink height, the ink width.
- For each candidate face (family x weight): the same text rendered at the size that gives the same ink height,
  then compared with the crop: width ratio (a strong clue: Calibri is narrow, Verdana wide), and normalised
  correlation of both images, slightly blurred and resized to the same box (robust to scan quality).
- Votes per line: a replacement takes the face that scores best over its own words and the words of its line
  (similar height, same baseline), the value's own words counting more. Weight (bold) comes from the same
  comparison; `BoldMeter` stays as the fallback when the detector is not sure (too few letters, no contrast).
- Result: `Face(family, bold, file, score, margin)`; low confidence -> Arial as today.

## 3. Quality detector (`anonymizer/quality.py`)

Measured on the line around the value, by fitting the rendered template to the crop:
- native resolution of the scan (`page.get_images` pixel size vs page size: a 150 dpi scan is rendered at 300 dpi
  by the pipeline, its letters are soft / blocky),
- blur (Gaussian sigma that makes the template look like the crop),
- stroke gain (ink spread or faded thin strokes: threshold of the blurred template),
- ink and paper levels (least squares per colour channel: faded grey ink, yellowed paper),
- paper grain (std of the paper around the value, and its correlation length).

## 4. Rendering

- `render.scan_text_layout` takes a face instead of `bold` (same size / baseline / squeeze rules, metrics of the
  matched font).
- `quality.paint_word`: text mask rendered 4x supersampled, stroke gain applied, down to the native resolution and
  back, blurred, composited between the fitted paper and ink colours, then the same grain as the paper is added.
- The erased area gets paper grain too (no flat rectangle on a noisy scan). Noise is deterministic (seeded by the
  secret-free page number and box), so a rerun gives the same file.
- Tilted words (`paint_tilted`) use the same face and quality.
- Image text of digital pages (`_pixel_pass`): the replacement is drawn as a small image patch (paper + text with
  the image's quality) instead of Helvetica vector text, placed on the redacted area.

## 5. Config / UI / report

- `style: {mode, digital_fill, font_match: true, quality_match: true}`; missing keys -> true. Off = today's Arial.
- Config tab › Settings: two switches. Trace notes say which face and quality were used; the page log line counts
  the faces.

## 6. Tests

- `tools/make_test_fonts.py` -> `sample_data/Dossier Test 17/`: scans whose lines use known fonts / weights,
  at several qualities (clean 300 dpi, 150 dpi soft, faded + grain + JPEG), with fake personal values, and
  `fonts_truth.json` (font of every line, quality of every page).
- `check_all.font_tests`: detector accuracy on the truth lines (family, weight), quality estimates (dpi exact, blur
  and grain ordered as generated, close to truth), and after anonymization: every replacement in the output is
  detected as the same face as its line, its blur / grain / ink level are close to its neighbours'; switches off ->
  Arial as before. Dossier Test 17 in EXPECT (no leak, empty second check).
- Full `check_all` must still pass (rendering changes can change what the verification OCR reads).

## 7. What was built, and how it measures (Windows OCR, this PC)

- Dossier Test 17 originals (48 lines, 16 fonts, 3 qualities): font family 47/48 (the miss: Georgia read as Segoe UI
  on an e-mail of the 150 dpi page), sans / serif / mono 47/48, weight 48/48. Blur per page 0.4 / 1.0 / 1.4 px for
  generated 0.0 / 1.1 / 1.6 px; native resolution read exactly from the PDF image.
- Outputs measured again: the replacement reads as the font it was written in (same kind and weight 45/48, same
  font ~43/48, the others are near-twins: DejaVu Sans Bold / Verdana Bold, Open Sans / Segoe UI); its blur, its ink
  level and the grain of the erased area match the values it replaced; a rerun gives the same file.
- Synthetic pages (no OCR, `font_unit_tests`): sharp pages 9/9 fonts with weight; soft pages give a font of the same
  kind 27/27 (same family 23/27: Arial / Roboto, Courier New / Courier Prime, Georgia Bold / Cambria Bold).
- Cost: about 0.1-0.3 s per replaced value (Dossier Test 17: 29 s instead of 13.5 s for 52 values on 3 dense pages).

Changes made on the way:
- Weight is decided by synthesis (regular and bold, degraded like the page, which explains the pixels), not by
  stroke width: blur thickens thin strokes, a ratio could not tell.
- The candidates are blurred like the page before the shape comparison (serifs lost in a soft copy).
- The ink colour is the ink of a full stroke consistent with the fitted blur (darkest pixels / peak coverage).
- Slanted words are not measured (their OCR box is not their ink): Arial with the page quality, as before for the font.
- A word only partly covered by a value is cut by the widths of its measured font (a Consolas e-mail cut with Arial
  widths left pieces of letters).
- The grainy erase paints exactly the pixels the flat rectangle painted.

## 8. Second round: an insurance contract scanned at a middle / low quality (Dossier Test 17)

`contrat_assurance_scan.pdf`: 3 pages, paragraphs with the values in the middle of sentences, names written 'Prénom
**NOM**' (surname in bold), reversed, all bold, in capitals and Title case, a name and an address split across two
lines, several fonts per page (Verdana labels, Times / Calibri / Garamond paragraphs, Georgia headings, Segoe UI
signatures), 200 and 150 dpi, faded ink, pages turned by 0.8, -1.0 and 0.6 degree.

Found and fixed:
- a name in two weights was written in one ('Rachid DELACROIX' all regular): each word now takes its own weight;
- fakes 10-15 % too big in the paragraphs (the box of a long value on a turned page): size and baseline fitted word
  by word on the letters;
- a comma glued to a word (OCR 'VERHAEGEN,') made the word look smaller and regular: punctuation is left out;
- weight on faded Calibri at 200 dpi: the error test alone overlapped (regular 0.58-0.88, bold 0.34-0.77): the ink
  mass is a second clue, the two averaged (233/234 words);
- gaps before a comma when the fake is shorter: the box is filled (spaces, then letters a little);
- a split address cut by widths ('Avenue de' squeezed in the box of one word): cut by words;
- inks too light on some short values: the page's ink as a floor;
- a stroke spread of -0.25 px fitted on a faded page thinned every fake: kept only with a clear gain;
- detection: 'Avenue' read 'i\venue' (the street was left), a phone number over a line break (left), 'Monsieur' read
  'Nlonsieur' (replaced as a first name).

Left as is: a single short word can only be stretched 8 %, so a small gap can stay before its comma; the descender of
a letter far outside the OCR box (the tail of a Garamond 'J') can leave a dot.

Known limits: look-alike fonts on soft copies; a page entirely in bold reads as regular with ink spread; no italics;
fonts outside the catalog get the closest candidate.

## 9. Third round: coarse scans in four inks (`paragraphes_scan.pdf`, Dossier Test 17)

Four pages of paragraphs, each with its own ink and scan: pixelated blue Calibri (110 dpi, point-sampled), black and white
Times (100 dpi, threshold), thick grey Verdana / Tahoma (200 dpi, copier spread), faded Garamond (200 dpi). The user's
real scans showed fakes darker or lighter than the text around them, smooth where the page is pixelated.

Found and fixed (numbers: ink mass of the replacement / the original over the union of both boxes, median, 10th
percentile, on the words that were replaced):
- Privacy first: on the black and white page the OCR missed 'Clément', an e-mail and a phone number, garbled an IBAN and a
  reference; names and a reference were not detected either. 11 values of 31 were left readable by eye in the output
  (22 replaced); now 31 of 31 (a page is read four times: `ocrpage.page_words`, see CLAUDE.md section 9; detectors for
  the shapes above).
- The fit took a thick copy for a blurred one (blur 2.5, spread 0): p3 median 0.88 -> 0.92, 10th percentile 0.81 -> 0.85,
  strokes 0.78 -> 0.89 of the original (10th percentile 0.67 -> 0.78). A joint search (every blur at every spread) and
  candidates thickened by the fitted spread (family right on 8 of 9 values of that page instead of 3).
- A scan enlarged more than twice is drawn by MuPDF as blocks: the fakes were smooth and heavier than the pixels around
  them. They are blocks on the page's grid now (p1: mass median 1.13 -> 1.04, 90th percentile 1.20 -> 1.11; the jagged
  steps are the page's).
- A black and white scan: grey, smooth fakes (a quarter of the values only were replaced then, so the comparison is on 9
  words: ink 23 levels away) -> black and white (25 words, ink 11 levels away, mass median 0.99, 10th percentile 0.79,
  which is the threshold: a 1-pixel stroke is ink or nothing).
- The weight threshold: the bold names of the pixelated page scored 0.41 and 0.47 (threshold 0.5): written regular. The
  gap between regular and bold words is 0.31 .. 0.79 on the other scans: `BOLD_AT = 0.35`, and a score resting on fewer than
  4 letters is shrunk toward regular (regular digits scored up to 0.58).
Left: a value written in another family than its line (an IBAN in Consolas in a Garamond sentence) takes the line's;
a fake narrower than the box of its original can leave a pale fragment of the last letter.

## 10. Fourth round: the style of a block instead of the style of each value

The complaint: inside one paragraph, two replaced values come out with different stroke thickness and quality. Each
value's weight, ink, grain, size and family were decided from its own 1-3 words, so each got its own noisy draw. The goal
is replacements that agree with the text around them, not a perfect font match.

### 10.1 The scoreboard (`tools/style_bench.py`)

- Sample set: the three fake scans of Dossier Test 17 with their truth (`polices_scan.pdf`, `contrat_assurance_scan.pdf`,
  `paragraphes_scan.pdf`: 229 values, 10 pages at 100 to 300 dpi). No new generator and no public scans were made: the Test
  17 paragraph file already covers paragraphs in one font at four qualities.
- Two runs per file on a copy: the fake run (the real output) and an identity run (every value replaced by itself). Measures
  on 300 dpi crops: thickness (median horizontal ink run / letter height), darkness (5 % darkest level), contrast,
  sharpness, height, grain; round trip ratios and an SSIM (3x3 windows, best shift of +-2 px); consistency of each fake
  value against the unreplaced OCR words of its block (z-scores; the share with |z| > 2 on thickness or darkness; the
  spread of the values against the spread of the others, 1 = the same); detection of family and weight on the original.

### 10.2 Baseline and final (main numbers; the full per-page table: `python tools/style_bench.py run out.json`)

| measure (all pages, n = 229) | baseline | block style |
|---|---:|---:|
| family right (detector) | 91 % | 91 % |
| weight right (detector) | 98 % | 98 % |
| round trip: thickness ratio, median | 1.00 | 1.00 |
| round trip: |darkness| difference, median (levels) | 3.0 | 3.0 |
| round trip: sharpness ratio, median | 0.96 | 0.97 |
| round trip: SSIM, median | 0.758 | 0.747 |
| consistency: values with |z| > 2 on thickness or darkness | 44 % | 36 % |
| consistency: |z| thickness, 90th percentile | 2.62 | 2.39 |
| consistency: |z| darkness, 90th percentile | 9.10 | 9.11 |
| spread of the values / their block, thickness (median of blocks) | 1.00 | 0.92 |
| spread of the values / their block, darkness | 1.50 | 1.61 |
| spread of the values / their block, sharpness | 0.32 | 0.33 |
| spread of the values / their block, height | 0.29 | 0.29 |

Per page, the outlier share (baseline -> block): contrat p1 70 -> 58 %, contrat p2 41 -> 44 %, contrat p3 57 -> 29 %,
paragraphes p1 36 -> 41 %, paragraphes p3 45 -> 23 %, paragraphes p4 41 -> 41 %, paragraphes p2 5 -> 0 %, polices 0 -> 0 %.
Family per page: contrat p1 98 -> 98 %, paragraphes p3 74 -> 74 %, all others unchanged.

Timings (bench, scan + anonymize, one run each on a machine with other programs running; read the proportions):
baseline polices 47.5 s, contrat 49.1 s, paragraphes 76.7 s; block style polices 41.6 s, contrat 49.6 s, paragraphes 66.0 s.
The timings differ run to run by 10 to 15 %, so no speed-up or slow-down is claimed; the block style costs one measure
per block (bold scores of up to 24 words, grain, size), cached per page.

### 10.3 What the block style does (`anonymizer/layout.py`, `fontmatch.PageFonts.block_style`)

- Lines and blocks of the OCR words of a page (`layout.blocks`). A block with at least 4 measurable words is measured
  once: family (the vote of `face_for` over its words), weight threshold (the mid gap of its bold scores when they form
  two clusters of at least 2 words with a gap of 0.25; else `BOLD_AT`), letter size, grain, and ink / paper (`block_colours`).
- The values of the block take these in `look`, `face_for`, `is_bold`, `word_faces` and `metrics`. A value keeps its own
  size only beyond 12 % of its block's. Its baseline and the per-word weight decision stay per value.
- Ink and grain from the block: the consistency of the thickness and the outliers improve; the darkness spread widens
  (see 10.4).

### 10.4 Tried and kept or dropped

- Family of a value with few letters taken from its block (`WEAK_LETTERS = 6`): family 98 -> 77 % on contrat p1. Dropped.
- Ink of the block (kept): the ablation with each value's own ink gives outliers 41 % (block ink 36 %) and darkness spread
  1.39 (block ink 1.61); the thickness spread is 0.88 (0.92). The spec puts the ink on the block, so it stays, and the
  darkness spread is the price: a known trade-off, to look at next.
- Darkness z-scores stay high (90th percentile about 9): the 5 % darkest level of a small crop reacts to the blur and the
  colour of its neighbours; a better darkness measure is the next step.

### 10.5 What is not done

- The synthetic paragraph generator and the public-domain scans: not built (see 10.1).
- Quality (blur, spread, scale) is still per page, not per block; a block is never measured on its own quality.
- Family per block only as a vote: a value in another font than its block (an IBAN in Consolas) is still read as its line
  says, as before.

### 10.6 Checking the ruler (round five, 2026-10-22): what the consistency measure does and does not show

The first consistency measure was wrong: on the ORIGINAL values with nothing replaced it gave 31 % outliers, so it
followed the letters of each word, not the rendering. The ruler was corrected step by step; each step was measured.

Corrections, in the order they were found (`tools/style_bench.py`):

1. Each measure is divided by a PREDICTION for the same text, painted with `quality.erase` / `quality.paint` in the
   style of its block (size, page quality with the block's grain, paper and ink). Thickness and sharpness as ratios,
   darkness as a difference of levels. The letters cancel out.
2. The threshold is the Student t quantile for n-1 other words (sample spread times sqrt(1 + 1/n)), 2.5 % per measure
   so that 'thickness or darkness' is 5 % under the null (`t_critical`, computed).
3. A value is measured on the union of the OCR words that cover it, not on its ground-truth box. The truth box reads
   thicker than a word's box: 11 % thickness outliers against 5 % for the same values measured on their words.
4. The fake of a truth word is the word at its index in its value's configured replacement when the counts match
   (`exact`, 138 of 229 values); otherwise it is read back from the OCR of the output under the box. The first version
   looked up the word itself in the config, so most fakes were predicted as their original text.
5. The prediction uses the FACE the pipeline paints with (`face_for` of the value or of the reference word). The
   block's family is a vote over its words and is wrong for values set in another font: of 21 judged mismatches the
   pipeline's face matched the truth in 13, the block's in 7. Family accuracy has its own column (`fam`, 91 %).
   This means the consistency no longer penalises a family error: it measures the painting, given the face.

Remaining checks that found nothing: the colour fallback (identical numbers), the read-back against the written text
(correct for the cases checked; OCR errors on digits remain for values without an exact mapping), the ink of each
value against its block (equal on every value of contrat p2).

Results (Dossier Test 17, 176 values with a z-score; outlier = thickness or darkness beyond its threshold):

| | `b449308` (no block style) | current `main` (block style) | own-ink ablation |
|---|---:|---:|---:|
| fakes, share outside the threshold | 5 % | 8 % | 8 % (identical to main) |
| original values, nothing replaced (null, target 5 %) | 5 % | 5 % | 5 % |
| identity replacements | 6 % | 3 % | 3 % |
| spread of values / their block: thickness, darkness, sharpness | 0.82 / 0.67 / 0.54 | 0.71 / 0.49 / 0.55 | 0.71 / 0.49 / 0.55 |

- The null is at target. The fakes are close to it (8 % against 5 %), which is within a few values of noise on 176.
- Per page, the remaining excess is on `contrat p2` (26 % of 27 values with z) and `paragraphes p4` (18 % of 17). On
  `contrat p2` the flags are darkness: the fakes' 5th-percentile darkness is lighter than the prediction's (the ink
  is the block's, checked on every value). Not explained. A candidate, not checked: the darkest-5 % statistic of a small
  crop depends on how much ink the text covers, and the fake's layout (squeeze, filling, baseline) differs from the
  prediction's.
- The block style shows no measurable consistency gain on this ruler (8 % against the baseline's 5 %, not significant
  on 176 values). The 36 % vs 44 % of 10.2 came from the first, invalid ruler and is withdrawn. The block style is kept
  as the design (the spec puts the look on the block), but no gain is claimed.
- The own-ink ablation is identical to the block style on every row: the patch of `PageFonts.block_colours` takes effect
  (checked), so on these generated files the block's ink and each word's own ink are the same. A file with uneven ink is
  needed before the block ink can be judged.
- Not done here: rendering changes. No change to the fakes is justified by these numbers.
