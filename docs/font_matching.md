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
