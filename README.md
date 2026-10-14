# anonymizePDF

Local tool to anonymize PDFs **folder by folder** (one folder = one client = one shared config).
Everything runs on your machine. Nothing is uploaded anywhere.

```
app.py                   web app (Flask, 127.0.0.1 only)
anonymizer/
  engine.py              scan folder -> draft config -> anonymize -> verify -> report
  pii.py                 detectors (IBAN, phone, e-mail, addresses, names...), matching and text/box helpers
  ocr.py                 OCR of scanned pages: Tesseract or Windows built-in OCR
  fake.py                fake replacement values (same length, same format)
  render.py              drawing replacement text on digital pages and scanned images
  secret.py              secret key for the fake values (created once per PC, outside the repo)
  fields.py              identity documents: values under a label (Nom / Surname ...), MRZ
  regions.py             photos and signatures to black out
static/                  frontend (index.html, app.js, style.css)
docs/                    config.example.yaml, PROJECT_CONTEXT.md
sample_data/             fake PDFs to try it on (Dossier Test 0: simple FR statement, Dossier Test 1: harder BE one,
                         Dossier Test 3: French passport scan, Dossier Test 4: French ID card front + back, SPECIMEN)
tools/                   make_test_statement.py / make_test_passport.py / make_test_idcard.py: regenerate Test 1 / 3 / 4
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m spacy download fr_core_news_md      # names model; use nl_core_news_md for Dutch, you can install both
```

If `spacy download` fails with `SSL: CERTIFICATE_VERIFY_FAILED` (company proxy or antivirus inspecting HTTPS), install
the model with pip directly (the model version must match spaCy's major.minor, see `python -m spacy info`):
```bash
python -m pip install https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl
```
For Dutch, replace `fr_core_news_md` with `nl_core_news_md` (both times) in the URL.

Scanned PDFs need an OCR engine. Two are supported; the app uses Tesseract if it is installed, else Windows OCR
(force one with `--ocr-engine tesseract|windows`). The header of the app shows which one is active.

**Windows OCR – no admin rights needed** (recommended on a locked-down PC). It is built into Windows 10/11; the
`winrt-*` packages in `requirements.txt` are all it needs. It reads the OCR languages installed with Windows. Check
that French is there (PowerShell, no admin):
```powershell
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]; [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object LanguageTag
```
If `fr-FR` (or `nl-NL`) is missing, it has to be added in Windows language settings (usually needs admin / IT).

**Tesseract – needs an installation** (admin rights on Windows), with the language packs you use (fra, nld, eng):
* Windows: `winget install UB-Mannheim.TesseractOCR` (or the UB-Mannheim installer, tick the extra languages).
  Note: `pip install tesseract` / `pytesseract` do **not** install the program itself. It is found automatically in
  `C:\Program Files\Tesseract-OCR`; otherwise set the env var `TESSERACT_CMD` to the full path of `tesseract.exe`.
  If languages are missing, set `TESSDATA_PREFIX` to the `tessdata` folder.
* macOS: `brew install tesseract tesseract-lang` · Linux: `apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-nld`

## Run

```bash
python app.py --root "C:\path\to\folder\that\contains\the\client\folders"
# try it:   python app.py --root sample_data
```
Open http://127.0.0.1:5000. Options: `--port 5000`, `--ocr-lang nld+fra+eng`, `--spacy-model nl_core_news_md` (repeatable).

Layout: `root/Client_A/*.pdf`, `root/Client_B/*.pdf`, … Each client folder gets its own config.

## Workflow

1. Click a client folder (or a file in it) in the tree: it becomes the **working folder**.
2. **1 · Scan & draft config** – every PDF of the folder is read (OCR for scans), personal data is detected, and
   `anon_config.yaml` is written in that folder with a **suggested replacement for each value**.
3. Review the **Config** tab, edit, **Save** (Ctrl+S).
4. **2 · Anonymize folder** – the same config is applied to every PDF of the folder. Each `name.pdf` produces
   `name_anonymized.pdf` next to it (originals are never touched). Digital PDFs: the text is really removed.
   Scans: OCR, paint over, rebuild as images.
5. Compare **Original** and **Anonymized** side by side (zoom with −/+ or Ctrl+wheel, both scroll together; untick
   *Sync* to get the browser's own PDF viewer back, where text can be selected). Drag the bars between the panes to resize them. The **Recap** tab lists, per file, every replaced value
   (type, original → replacement, count, pages), the time taken, and any original value still found in the output.
   The **Log** tab shows the run.

## The config (`anon_config.yaml`)

```yaml
replacements:
  - {find: "Thomas Marchand", replace: "Alexis Beaumont", type: PERSON}   # 20x in 3 file(s)
  - {find: "06 12 34 56 78",  replace: "06 26 43 15 49",  type: PHONE}
  # - {find: "30/09/2024", replace: "22/03/2025", type: DATE}             # commented = NOT replaced
```
* A line that starts with `#` is ignored. To replace something suggested but off by default, remove the `#`.
* To add something the detectors missed, add a line. `find` is matched ignoring case and extra spaces.
* **Dates are off by default**, except dates that look like a birth date (next to "né le", "naissance", "geboren"…).
* Detected organisations / places / uncertain names are listed commented out.
* Optional `regex:` rules for your own patterns. `style.mode: blackbox` paints black boxes instead of text.
* Identity documents (passport, ID card): values written under a label (`Nom / Surname`, `Prénoms`, `Date de
  naissance`, `Lieu de naissance`, `N° du document`, `Adresse`) are found from their position; `Sexe` keeps the gender.
* `blackout:` what cannot be replaced by fake text is covered with a black box (all on by default):
  * `photos`: identity photos and the faded "ghost" copy; with `photos_only_on_id_documents: true` (default) only
    on pages that look like an identity document (MRZ, Nom / Prénoms / Date de naissance labels, "passeport"...);
  * `signatures`: handwriting next to a "Signature" / "Handtekening" label (a label, not a sentence);
  * `qr_codes`: QR codes and other 2D barcodes (scans and digital PDFs).
* `mrz: regenerate` (default) rebuilds the machine readable zone in the same format as the original (passport
  2 x 44, ID card 3 x 30, 2 x 36, old French ID card) from the fake values, so the names and numbers match the rest of
  the page; what cannot be read is filled with values of the right shape. `keep` leaves it (it then shows real data).
* `style.digital_fill: none` (default) writes the new text on the page background; `white` puts a white rectangle
  behind it (digital PDFs only; scans are always painted over with the paper colour).
* `match_case: true` keeps UPPER / lower case of the original (`MARCHAND` → `BEAUMONT`).

## How the suggestions work

Deterministic (same input → same suggestion, in every file and every later scan), same length where possible,
same format: phone numbers keep their grouping (first two digits kept), IBANs keep the country and get a valid
checksum, e-mails keep the common domains (gmail…) and change the rest, street/city names are swapped for
names of similar length, birth dates shift by a few days and years. Names are replaced token by token, so
`Marchand` alone gets the same replacement as inside `Thomas Marchand`.

## Limits (be aware)

* **A scan is only as good as its OCR.** Handwriting, stamps, signatures and blurry text are not found.
  Always look at the anonymized scans.
* Name detection is a heuristic (titles like `M.`, `Prénom NOM`, a first-name list, the spaCy model). That is why
  uncertain names are commented out for you to decide. Check the config before running.
* The "leftover" check only searches for the exact original values from the config. It cannot know about
  personal data that was never detected.
* A PDF is treated as *digital* or *scan* as a whole (a mixed document gets a warning).
* Digital PDFs: the replacement keeps the size, colour and baseline of the original, and its font when possible
  (the font embedded in the PDF if it has the needed letters, else the same font installed on this PC, else the
  closest standard font). The Log says which was used. Scans use Arial (bold when the original
  text is detected as bold), sized and coloured like the original.

## Privacy

`anon_config.yaml` and `anon_report.json` contain the **real** values. `.gitignore` excludes them, the
`*_anonymized.pdf` files and all other PDFs (except `sample_data/`). Keep your real PDFs outside the repository.

**Secret key.** Fake values are derived from the original with HMAC-SHA256 and a secret 256-bit key, so having
the code is not enough to work back from "Beaumont" to the real name by trying a list of names. The key is created
on the first run and kept **outside the repository**: `%APPDATA%\anonymizePDF\secret.key` (Windows) or
`~/.config/anonymizePDF/secret.key` (override with the env var `ANON_SECRET_FILE`). The app prints its location
at start.
* Keep it private and never commit or share it: with the key, the name list could be tried again.
* Back it up: with the same key, the same person gets the same fake name in every folder, now and later.
  Losing it only changes future suggestions; existing `anon_config.yaml` files still work.
* Copy it to another PC (e.g. the production PC) if fake names must match across machines.
