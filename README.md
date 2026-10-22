# anonymizePDF

Local tool to anonymize PDFs **folder by folder** (one folder = one client = one shared config), with a small web app.
Everything runs on your machine: the server listens on `127.0.0.1` only, nothing is uploaded anywhere.

Personal data is **replaced by plausible fake values** of the same length and format (names, addresses, IBANs,
phone numbers, national numbers, ...), so the documents stay readable. What cannot be faked (identity photos,
signatures, QR codes) is covered with a black box that **really removes** the data from the file.
Works on digital PDFs (real text layer) and on scans (OCR). Documents are mostly French, also Belgian / Dutch.

```
app.py                   web app (Flask, 127.0.0.1 only)
anonymizer/
  engine.py              scan folder -> draft config -> anonymize -> verify -> report
  scanpaint.py           scanned pages and pictures: find, erase and rewrite the values in the font and quality of the scan
  pii.py                 detectors (IBAN, phone, e-mail, addresses, names...), matching and text/box helpers
  ocr.py                 OCR of scanned pages: Tesseract or Windows built-in OCR
  fake.py                fake replacement values (same length, same format, consistent)
  secret.py              secret key for the fake values (created once per PC, outside the repo)
  render.py              writing replacement text on digital pages and scans, black boxes on digital pages
  fields.py              identity documents: values under a label (Nom / Surname ...), machine readable zone (MRZ)
  regions.py             photos, signatures and QR codes to black out
static/                  frontend (index.html, app.js, style.css)
docs/                    config.example.yaml
sample_data/             FAKE test documents (see "Test documents")
tools/                   generators of the test documents
CLAUDE.md                detailed technical description (for maintenance / Claude Code)
```

## Quick start on Windows (no admin rights)

Double-click **`start.bat`**. The first time it creates the Python environment (Python 3.12 or 3.13 must be
installed; the python.org installer works without admin rights) and asks for the folder that contains the client
folders; it then starts the app and opens it in the browser. Keep the window open while you work.
* The folder is remembered in `start_root.txt`. To use another one, drag it onto `start.bat`, or delete that file.
* **PC without internet access**: on a PC that has it (same Python version), run **`prepare_offline.bat`**: it fills
  a `wheels` folder with everything needed (packages and the French names model). Copy the whole project folder,
  `wheels` included, to the other PC: `start.bat` installs from it, offline.
* Port 5000 already taken: `set ANON_PORT=5001` before running `start.bat`.
* Copy your secret key too if the fake names must be the same on both PCs (see Privacy).

## Install (manual)

Use **Python 3.12 or 3.13** (newer versions may have no prebuilt wheels for spaCy and try to compile).

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate            # Git Bash: source .venv/Scripts/activate   macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m spacy download fr_core_news_md      # names model; use nl_core_news_md for Dutch, you can install both
```

If `spacy download` fails with `SSL: CERTIFICATE_VERIFY_FAILED` (company proxy or antivirus inspecting HTTPS), install
the model with pip directly (the model version must match spaCy's major.minor, see `python -m spacy info`):
```bash
python -m pip install https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl
```
For Dutch, replace `fr_core_news_md` with `nl_core_news_md` (both times) in the URL.

**PC without internet access to PyPI** (e.g. the production PC): download the packages on another PC with the same
Python version, copy the `wheels` folder, then install from it:
```bash
python -m pip download -r requirements.txt -d wheels
python -m pip install --no-index --find-links wheels -r requirements.txt
```

### OCR (scanned PDFs only)

Two engines are supported. The app uses Tesseract if it is installed, else Windows OCR (force one with
`--ocr-engine tesseract|windows`). The header of the app shows which one is active.

**Windows OCR – no admin rights needed** (recommended on a locked-down PC). It is built into Windows 10/11; the
`winrt-*` packages in `requirements.txt` are all it needs. It reads the OCR languages installed with Windows. Check
that French is there (PowerShell, no admin):
```powershell
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]; [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object LanguageTag
```
If `fr-FR` (or `nl-NL`) is missing, it has to be added in Windows language settings (usually needs admin / IT).

**Tesseract – needs an installation** (admin rights on Windows), with the language packs you use (fra, nld, eng):
* Windows: `winget install -e --id UB-Mannheim.TesseractOCR --interactive` and tick French / Dutch under *Additional
  language data*. Note: `pip install tesseract` / `pytesseract` do **not** install the program itself. It is found in
  `C:\Program Files\Tesseract-OCR` or `%LOCALAPPDATA%\Programs\Tesseract-OCR`; otherwise set `TESSERACT_CMD` to the
  full path of `tesseract.exe`. If languages are missing, set `TESSDATA_PREFIX` to the `tessdata` folder.
* macOS: `brew install tesseract tesseract-lang` · Linux: `apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-nld`

Tesseract reads difficult scans a bit better (isolated letters, stamps); Windows OCR is enough for clean scans.

## Run

```bash
python app.py --root "C:\path\to\folder\that\contains\the\client\folders"
# try it:   python app.py --root sample_data
```
Open http://127.0.0.1:5000. Options: `--port 5000`, `--ocr-lang fra+eng` (e.g. `nld+fra+eng`),
`--ocr-engine auto|tesseract|windows`, `--spacy-model nl_core_news_md` (repeatable).
At start the app prints the OCR engine in use and the location of the secret key.

**Speed.** The pages of a scanned document are processed by several worker processes at once (one per page, up to the
number of cores minus one, at most 6, fewer when the PC is short of free memory; about 0.5 GB each). The files are
byte for byte the ones a single process writes. `ANON_WORKERS=1` turns it off (`set ANON_WORKERS=1` before starting
the app), `ANON_WORKERS=3` caps it. The names model is loaded once, in the background, when the app starts.

Layout: `root/Client_A/*.pdf`, `root/Client_B/*.pdf`, … Each client folder gets its own config.

## Workflow

1. Click a client folder (or a file in it) in the tree: it becomes the **working folder**.
2. **1 · Scan & draft config** – every PDF of the folder is read (OCR for scans), personal data is detected, and
   `anon_config.yaml` is written in that folder with a **suggested replacement for each value**.
3. Review the **Config** tab, edit, **Save** (Ctrl+S). The *Table* view lists every value by type (names, IBAN,
   phones...): tick a line to replace it, untick to keep the original, click a replacement to edit it, ✕ (on hover)
   deletes a line, the last line adds a value; *Settings* (folded at the top) holds the options; the search field
   filters. The *YAML* view shows the file itself (both views edit the same file).
4. **2 · Anonymize folder** – the same config is applied to every PDF of the folder. Each `name.pdf` produces
   `name_anonymized.pdf` next to it (originals are never touched). **Each page is processed in its own mode:**
   * Digital pages (real visible text): the original text is really removed (redaction), the fake text is written
     in its place in the same font, size and colour; annotations, metadata, bookmarks and hidden data are removed.
     If the page contains images (a pasted screenshot, a scanned part), they are read by OCR too and the values
     found in the pixels are overwritten and rewritten (in the font and with the quality of the picture, as a new
     small picture over the old one).
   * Scanned pages: OCR, the values are painted over with the paper colour and rewritten (same size, baseline,
     colour), the page is rebuilt as a new image. This includes **searchable scans**
     (scan image + invisible text layer added by the scanner): what counts is what the reader sees.
     The fake text is written **in the font of the line** (recognised among the Windows fonts and the free fonts
     bundled in `anonymizer/fonts`: Arial, Times New Roman, Calibri, Cambria, Georgia, Verdana, Courier New, Consolas,
     DejaVu, Roboto, Open Sans, Lato...), regular or bold, and **with the quality of the scan**: same resolution,
     blur, ink colour and paper grain, so it does not stand out as crisp new text. Both can be switched off
     (`font_match`, `quality_match` in the config, or Config › Settings): Arial, sharp, on a flat patch.
     The ⚙ Settings slider **Fake text quality** corrects the detected quality: below 0 the fakes are a little
     softer and grainier than the page (-0.1 = slightly worse), above 0 a little cleaner (+1 = sharp); 0 = as
     detected (double-click the slider to reset). The report keeps the value used.
   * A digital file with a scanned annex is therefore handled page by page (Recap shows *mixed*).
   * Photos, signatures and QR codes are covered with black boxes (see below).
5. Compare **Original** and **Anonymized** side by side:
   * *Sync* ticked (default): both PDFs zoom together (−/+, *Fit*, Ctrl+wheel) and scroll together; **click and
     drag** to move around the page. Untick *Sync* to get the browser's own PDF viewer back (text can be selected).
   * The two PDF panes always keep the same width. Drag the bars beside the tree and the side panel to resize
     them (double-click a bar to reset). **☰** (top left) shows / hides the file tree.
6. The **Recap** tab lists, per file, every replaced value (type, original → replacement, count, pages), the black
   boxes (■ photos, QR codes, signatures), the MRZ, the time taken, and any original value still found in the
   output ("leftovers"). The **Log** tab shows the run (OCR engine, fonts used, bold, black boxes...).
   Values the anonymized file still shows, found by a second reading, are listed as warnings and offered in the
   **Config** as suggestions (commented out): tick them, then anonymize again.
7. Once you have checked an anonymized file by eye, press **Mark as reviewed** (top of the *Anonymized* pane). The tree
   shows `✓` on the file and `✓ 2/3` on the folder. If the file is generated again (or changed in any way), the flag
   goes back to *not reviewed* by itself. Flags are stored in `anon_review.json` in the folder.

### Many folders at once (batch)

Select a folder that contains client folders (e.g. the root): the **Recap** tab shows an overview of every client
folder below it (PDFs, config, anonymized, leftovers, reviewed); click a row to open that folder. The toolbar
buttons become **Scan all subfolders** / **Anonymize all subfolders**, and **Scan + anonymize all** does both in
one go (scan where there is no config yet, then anonymize what is to do).
* **Scan … folder(s) without config** drafts a config in every folder that has none (existing configs are never
  overwritten, so your reviewed edits are safe).
* **Anonymize … folder(s) to do** runs every folder whose outputs are missing or whose config changed since the last
  run. *redo all* also redoes the folders already done.
A folder that fails does not stop the others; the Log tab lists what was done, skipped or failed.

## What is detected

* **Names** – after a title (M., Mme, Dr...), `Prénom NOM`, `NOM Prénom`, the spaCy model, and values under a
  label on identity documents. Uncertain names are suggested but commented out.
* **Bank** – IBAN (checksum), BIC, French RIB / account numbers (also after "N° de compte", "Numéro client"), bank /
  branch codes ("Code banque / guichet"), masked card numbers (`6703 **** **** 4471`, `Carte x4412`), Belgian
  structured payment references, SEPA creditor identifiers.
* **Contact** – e-mail, phone (FR / BE / international), street addresses, postcode + town.
* **Identifiers** – Belgian national number and VAT number, passport / ID card numbers, insurance policy and
  contract numbers (after "police", "contrat", "dossier"...).
* **Dates** – off by default, except dates of birth (next to "né le", "naissance", "Date de naissance", "geboren"...).
  Other dates are listed commented out. Dates with `/ . -` or spaces (`14 05 1992`) are recognised.
* **Identity documents** (passport, ID card): values written under a label (`Nom / Surname`, `Prénoms`, `Date de
  naissance`, `Lieu de naissance`, `N° du document`, `Adresse`...) are found from their position on the page;
  the `Sexe` field keeps the gender of the fake names.

## How the suggestions work

* **Consistent**: the same value gets the same fake everywhere: in every file of the folder, in every folder on
  this PC, and at every later scan (as long as the secret key is the same, see Privacy). Names are replaced part by
  part, so `Marchand` alone and inside `Thomas Marchand` match; accents and case do not matter (`elodie` in an e-mail
  = `Élodie`); an initial follows its first name (`É. Vermeulen` → `S. Hendrickx`); a name slightly misread by OCR
  (`Vermeotlen`) gets the same replacement as the correct one.
* **Same shape**: same length where possible, first names keep their **gender**, case is kept (`MARCHAND` →
  `BEAUMONT`, `Vermeulen` → `Hendrickx`); IBANs keep the country and get a valid checksum; phone numbers keep their
  grouping; e-mails keep common domains; streets / towns come from lists (a French postcode gets a French town);
  masked card numbers keep their `****`; a Belgian national number gets the fake birth date of the same person;
  a number found inside a longer one (branch code inside an account number) reuses the matching digits.
* **Not reversible**: the fakes are derived with a secret key, so the code alone cannot be used to work back from
  a fake name to the real one (see Privacy).

## Black boxes

What cannot be replaced by fake text is covered with a black box, **and removed from the file**: on scans the page is
rebuilt as a new image; on digital PDFs the text, the image pixels and the vector drawings under the box are deleted
(not just hidden: removing the box in a PDF editor shows nothing). Configurable under `blackout:`:

* `photos` – the **faces of people** (ID photos, passports, ghost portraits, portraits in a document): the head is
  covered, found by a face detector that runs offline (YuNet model shipped in `anonymizer/models/`, ONNX Runtime).
  With `photos_only_on_id_documents: true` faces are searched only on pages that look like an identity document;
  the default is `false` (every page). Configs written before this change keep their own value.
* `signatures` – handwriting next to a label such as "Signature", "Signature du titulaire", "Date et signature",
  "Handtekening" (a label, not a sentence containing the word), a closing formula, or a line that says who signed
  ("Le notaire Maître X a signé", "Signé par ..."). The signature can be under the label or on its row, far to the
  right (the next cell of a grid, up to 13 cm). The whole signature is followed, even long strokes.
* `qr_codes` – QR codes and other 2D barcodes (Data Matrix, 2D-Doc), on scans and digital PDFs.

## Machine readable zone (MRZ)

Passports and ID cards repeat the name, number and date of birth in the MRZ (`P<FRA...<<...`), which OCR reads very
badly. It is therefore never read back but **rebuilt** in the same format as the original (passport 2×44, ID card
3×30, 2×36 documents, old French ID card) from the fake values, with valid check digits, so it matches the rest of
the page. What cannot be read on the page is filled with values of the right shape. `mrz: keep` leaves it untouched
(it then shows the real data).

## The config (`anon_config.yaml`)

```yaml
ocr_lang: "fra+eng"          # OCR languages for scans
case_sensitive: false
match_case: true             # keep UPPER / lower / Title case of the original
style:
  mode: replace              # replace = fake text | blackbox = black box over every replaced value
  digital_fill: none         # digital PDFs: none = keep the page background | white = white rectangle behind
  font_match: true           # scans and pictures: write in the font of the text around (false = Arial)
  quality_match: true        # scans and pictures: same blur, ink, paper grain as the scan (false = sharp)
blackout:
  photos: true
  photos_only_on_id_documents: true
  signatures: true
  qr_codes: true
mrz: regenerate              # regenerate | keep

replacements:
  - {find: "Thomas Marchand", replace: "Alexis Beaumont", type: PERSON}   # 20x in 3 file(s)
  - {find: "06 12 34 56 78",  replace: "06 26 43 15 49",  type: PHONE}
  # - {find: "30/09/2024", replace: "22/03/2025", type: DATE}             # commented = NOT replaced

regex:                       # optional, your own patterns
  - {pattern: '\bREF-\d{6}\b', replace: "REF-000000"}
```
* A line that starts with `#` is ignored. To replace something suggested but off by default, remove the `#`.
* To add something the detectors missed, add a line. `find` is matched ignoring case and extra spaces; on scans,
  common OCR confusions are tolerated too (0/O, 1/l/I, 5/S, missing accents).
* Edit `replace` freely; delete or comment out a line to keep that value.
* Full commented example: `docs/config.example.yaml`.

## Limits (be aware)

* **A scan is only as good as its OCR.** Handwriting, stamps and blurry text are not read, so they are not replaced
  (signatures are blacked out only when they have a label next to them). **Always look at the anonymized files.**
* Name detection is a heuristic. That is why uncertain names are commented out for you to decide: check the config.
* The "leftover" check only searches for the original values of the config. A second check reads the anonymized file
  again with the detectors: what it still finds (an IBAN, a name...) is a warning in the Recap and is added to the
  Config as a suggestion that is not active. Tick the ones that are right and anonymize again. Personal data that
  no detector recognises is still not found.
* Photo, QR code and MRZ detection are image heuristics, tested on generated documents: check them on your own
  documents. A logo shaped like a portrait could be blacked out (`photos_only_on_id_documents` limits this).
* Faces: the detector looks at the front of faces; a face seen in profile can be missed. Check the anonymized files.
* MRZ: exact only for the fields that could be read on the page; Windows OCR often misses isolated letters (the
  `F` of "Sexe"), the sex is then `<`. An MRZ inside the text layer of a digital PDF is not handled.
* Text inside images of digital pages is only replaced when OCR reads it; the image pixels are overwritten under
  each value found, the rest of the image is kept.
* Digital PDFs: the fake text uses the original font when the PDF or the PC has it, else the closest standard font
  (the Log says which). Scans: the font is recognised among the candidate fonts only (a font that is not on the PC
  nor bundled gets the closest look-alike, e.g. Helvetica -> Arial); on soft copies close fonts can be mixed up
  (Arial / Roboto, Courier New / Courier Prime). Italic fonts are candidates too, and slanted text takes the font
  measured once the line is turned straight. A page written entirely in bold can be read as regular text with ink
  spread. The Log says which fonts were used, the Trace view shows the font and quality of each replacement. On
  scans the fake names are chosen so that they take about the width of the name they replace in its font (digital
  PDFs: by number of letters, and a slightly wider name may be drawn a little smaller). The machine readable zone and
  the texts typed by hand are written with the blur and grain of the scan (and, for typed text, its font).
* On digital PDFs, an image only partly under a black box stays in the file, with the covered pixels overwritten.

Bundled fonts (`anonymizer/fonts`, used to recognise and rewrite scanned text): DejaVu Sans / Serif (Bitstream Vera
licence), Roboto, Open Sans, Lato, Source Sans 3, Barlow, Courier Prime (SIL Open Font License); the licences are in
`anonymizer/fonts/licenses`. They are only read by the app, never installed in Windows.

## Privacy

* `anon_config.yaml` and `anon_report.json` contain the **real** values (original → fake). `.gitignore` excludes
  them, the `*_anonymized.pdf` files and all PDFs outside `sample_data/`. Keep your real PDFs outside the repository.
* Do not send real documents to an AI assistant; the fake test documents are enough for development.

**Secret key.** Fake values are derived from the original with HMAC-SHA256 and a secret 256-bit key, so having
the code is not enough to work back from "Beaumont" to the real name by trying a list of names. The key is created
on the first run and kept **outside the repository**: `%APPDATA%\anonymizePDF\secret.key` (Windows) or
`~/.config/anonymizePDF/secret.key` (override with the env var `ANON_SECRET_FILE`). The app prints its location
at start.
* Keep it private and never commit or share it: with the key, the name list could be tried again.
* Back it up: with the same key, the same person gets the same fake name in every folder, now and later.
  Losing it only changes future suggestions; existing `anon_config.yaml` files still work.
* Copy it to another PC (e.g. the production PC) if fake names must match across machines.

## Test documents

`sample_data/` only contains **fake** documents (invented people, drawn portraits, "SPECIMEN" marks):

| Folder | Content |
|---|---|
| `Dossier Test 00` | simple French bank statement (digital + clean scan) |
| `Dossier Test 01` | Belgian bank statement, harder: several fonts, coloured boxes, two holders, signature, stamp (digital + tilted, yellowed scan) |
| `Dossier Test 03` | French passport, A4 scan: photo + ghost image, signature, MRZ |
| `Dossier Test 04` | French ID card front + back on one scan: photo + ghost, signature, address, QR code, 3-line MRZ |
| `Dossier Test 05` | mixed PDFs: searchable scan (invisible text layer), digital statement with a scanned annex, contract with a pasted image of bank details |
| `Dossier Test 06` | faces: digital page, scan (portraits of several sizes, ID card, sideways page), photo of a card on a table, a page without faces (`faces_truth.json` lists every face) |
| `Dossier Test 17` | fonts and scan qualities: lines in 16 known fonts (regular / bold) scanned at 300, 200 and 150 dpi (soft, faded, grainy), a digital page with a pasted picture of text, and a 3-page insurance contract scanned at a middle / low quality, turned by less than a degree, with the values inside sentences and names written 'Prénom **NOM**' (`fonts_truth.json`) |

They are generated by `tools/make_test_statement.py`, `make_test_passport.py`, `make_test_idcard.py` and
`make_test_mixed.py`, `make_test_faces.py` and `make_test_fonts.py` (Windows fonts needed), e.g. `python tools/make_test_idcard.py "sample_data/Dossier Test 04"`.
The portraits of `make_test_faces.py` are public-domain photographs from Wikimedia Commons (`tools/test_faces/`).

## Checking that everything still works

```bash
python tools/check_all.py            # all test folders, 2 random keys + config options (about 8 minutes, 6 processes)
python tools/check_all.py --quick    # 1 key, no option tests
python tools/check_all.py --fast     # also leaves out the long font / contract runs (about 6 min)
python tools/check_all.py --jobs 1   # one job after the other (slower, the output comes as it is made)
python tools/check_all.py --key "C:/path/to/secret.key"   # replay a failed run (its summary names the key)
```
It anonymizes copies of the test documents (never `sample_data/` itself, never with your real secret key) and checks
every output: no sensitive value in the text layer, in a new OCR of each page or in the metadata; the expected
black boxes, really black and with nothing left under them; the MRZ format and check digits; the processing mode of
each file; the secret key and fake-value rules; the config options. Exit code 0 = all passed. Run it after any change.
