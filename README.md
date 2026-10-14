# anonymizePDF

Local tool to anonymize PDFs **folder by folder** (one folder = one client = one shared config).
Everything runs on your machine. Nothing is uploaded anywhere.

```
app.py                   web app (Flask, 127.0.0.1 only)
anonymizer/
  engine.py              scan folder -> draft config -> anonymize -> verify -> report
  pii.py                 detectors (IBAN, phone, e-mail, addresses, names...), OCR and text/box helpers
  fake.py                fake replacement values (same length, same format)
  render.py              drawing replacement text on digital pages and scanned images
static/                  frontend (index.html, app.js, style.css)
docs/                    config.example.yaml, PROJECT_CONTEXT.md
sample_data/             fake PDFs to try it on
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m spacy download fr_core_news_md      # names model; use nl_core_news_md for Dutch, you can install both
```

Scanned PDFs need **Tesseract OCR** with the language packs you use (fra, nld, eng):
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
   *Sync* to scroll them separately). Drag the bars between the panes to resize them. The **Recap** tab lists, per file, every replaced value
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
* Replacement text is drawn in a standard font, so it looks slightly different from the original.

## Privacy

`anon_config.yaml` and `anon_report.json` contain the **real** values. `.gitignore` excludes them, the
`*_anonymized.pdf` files and all other PDFs (except `sample_data/`). Keep your real PDFs outside the repository.
