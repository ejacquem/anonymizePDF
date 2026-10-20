# Real public documents: findings

Run of `python tools/check_public_docs.py` on branch `tests/public-real-documents` (2026-10-20, about 5 minutes):
**117 checks passed, 41 failed.** Each failure is a weakness of the app on a real document. The expectations are the
behaviour the app should have; they were not adjusted to the output. Nothing in `anonymizer/` was changed.

Baseline on the same code, `python tools/check_all.py --quick`: 719 passed, 1 failed
(`passeport_scan.pdf`: "LEFEVRE" is readable by OCR in the output, Dossier Test 03).

## The documents

Downloaded with `tools/fetch_public_samples.py` (checksums in `tools/public_samples.json`, never committed):

| File | Source | What it is | Mode |
|---|---|---|---|
| `bnc_guide_releve_bai.pdf` | Banque Nationale du Canada | user guide of a statement format, 18 pages, example records | mixed |
| `gwu_bank_statement_sample.pdf` | George Washington University | "sample bank statement": a bank certificate, scanned, placeholder sponsor | mixed |
| `dc_declaration_sample.pdf` | DC Department of Insurance | homeowners declarations page, John and Susy Doe (US formats) | digital |
| `ok_declarations_sample.pdf` | Oklahoma Insurance Department | explainer with an example declarations page (John E. Doe) | mixed |
| `ok_declarations_sample_bridgford.pdf` | a law firm's website | declarations page, searchable scan, English (John D and Jane A Sample) | mixed |
| `ca_assurance_habitation_fiche.pdf` | Crédit Agricole (Pacifica) | product sheet, no personal data | digital |
| `sg_mrh_conditions_generales.pdf` | Société Générale Assurances | home insurance general conditions, 64 pages, no personal data | mixed |

Scanned copies made by the test (200 dpi, one picture per page): the DC declarations page and page 10 of the guide.

## Weaknesses, by privacy impact

### 1. Personal data left readable

1. **US city line not found.** `Anywhere, USA 40000` and `Washington, DC 40000` (DC, digital and scanned),
   `Happyville, USA 12345` (Bridgford, OCR of the output). Alone, or after a street line, no detection.
   `pii.py` POSTAL_CITY only knows the French, Belgian, Swiss and similar postcodes.
2. **US policy number not found.** `Policy Number: 254H089SJ425` (DC): CONTRACT_REF takes digits only after the label.
3. **US phone number not found.** `1 202 111-2222` (DC agent): PHONE takes `+` / `00` or French forms only.
4. **Account numbers in CSV records with no label.** `03,0006123450999940,CAD,...` and
   `03,11110000558870001,EUR,...` (guide, page 10, digital and scanned): both stay readable. The generic rule
   (`pii.py`, `(?<![\w.,])\d{9,16}`) refuses a number after a comma, and 17 digits are out of its range. Only the
   amounts after a `+` are found. The second check reports the fragment `10000558870001` and nothing else.
5. **English name with a lower-case connector.** `John Doe and Susy Doe` stays readable in the scanned DC copy: the OCR
   reads it and the NER model finds it, but `engine.accept_person` requires every word to be Title Case, so `and`
   refuses the whole name. Nothing warns about it. In the digital folder run the same names are replaced only because
   `Doe` is an active name of another file of the folder (`John E. Doe`, OK declarations): the config says
   "shares a name word with an active name".
6. **The second check does not catch 1 to 5** in the scanned DC copy (no suspect at all), and only reports a fragment
   for 4 (`verify.leak_suspects`). The leftover check is silent for the same reason.

### 2. Wrong replacements (over-detection)

7. **A legal page becomes an identity page.** Page 56 of the general conditions says
   "carte nationale d'identité, passeport": `fields._ID_WORDS` matches, `is_id_document` is true, and every capitalised
   word of the page is taken as a name by identity-by-shape (`engine.py`, the `id_page` branch). Result: 97 PERSON
   replacements in the 64-page document (`Nous`, `Vous`, `Par`, `Dans`, `Cedex`, `Habitation`, `Médiateur`...).
8. **The folder config is shared by every file of the folder**, so those names reach the other documents:
   - product sheet with no personal data: 33 PERSON replacements (`Votre` -> `COLAS`, `Assurances` -> `Frédérique`,
     `Conditions` -> `BOUCHARDON`, `Paris` -> `AUBRY`);
   - OK declarations: `Page` -> `REMY` 7 times, the title becomes "... DECLARATIONS REMY", `Sample` -> `SAMIRA`;
   - guide: `Description`, the header of a table on 11 pages -> `CHARPENTIER`; on the scanned page 10 `BAI Standard`
     becomes a name;
   - DC declarations: the headings `Standard Time` and `SAMPLE FORM` become names.
9. **A street keyword followed by ordinary words.** `mise en place du service` -> street `place du service` (guide);
   `place d'actions` (general conditions, p. 9), `cours d'eau` (p. 15, first run).
10. **US street numbers taken for French postcodes.** `2690 Pennsylvania` -> postcode town `2400 Périgueux`,
    `1212 First Street` -> `2400 Périgueux`, `3055 Tulip` -> `5900 Lille`, and in the scanned DC copy
    `1427 Sample` -> `0640 Cannes` on top of `1427 Sample Drive` -> `2071 Renard Drive` (two fakes for the same text).
    POSTAL_CITY (`pii.py`, `[1-9]\d{3,4}` followed by a capitalised word) takes any 4-digit number.
11. **Fakes of US addresses are not plausible.** `Insured Town, USA 12345` -> `Ykmehoh Mudj, APO 77026`,
    `Company Town, USA 54321` -> `Jizgyvo Risd, OJI 20640`: scrambled letters (`fake.py`, the `scramble` fallback).
12. **A false leftover.** `Drive`, a kind word kept in the fake `3963 Perrin Drive`, is reported as a leftover of the
    config value `Drive` (part of `Sample Drive`): WARNING "1 original value still found" on the declarations page.
13. **Judgement call.** `banker's personal checks` -> `moreau personal checks` (certificate): the signatory's surname
    replaces a common word everywhere. Over-redaction is accepted by design, a changed sentence may not be.
14. **Cover photo.** Three face boxes on pages 1, 11 and 43 (the same picture, same hash). The check "no drawing left
    under the box" flags one vector drawing under each box. Not analysed.

### 3. The text of digital PDFs is damaged around a replaced word

15. Oklahoma declarations (product explainer): after a replaced word, the neighbouring words lose their text layer.
    `following` -> `followi` + `ample`, `example` -> `ample`, `coverage` -> `verag`, `policy` -> `polic`,
    `your` -> `you` / `our`, `how much it` -> `wmuchit`. On the rendered page the words are repainted in another weight,
    and the replacement `Remy` is smaller than the text around it. Pointers: `render.py` (`glyphs`,
    `restore_collateral`, `neighbour_glyphs`, `redraw_missing`). Copy, search and reading of the output suffer.

### 4. Scans

16. The scanned DC copy: names and addresses as in 1, 5 and 10, and the headings as in 8.
17. English text read by the French recognizer: `ocr._windows_tag` uses the first installed language of `fra+eng`
    (fr-FR). On the English searchable scan `Named insured` is read `Named 'nsured` with `fra+eng`; `eng` (en-GB)
    reads it correctly. One page, small effect, but every English scan goes through it.

### 5. Speed

18. The 64-page general conditions: about 200 s of anonymization (about 3 s a page), the folder of five files about 4
    minutes. The test run takes about 5 minutes; `--quick` leaves the 64-page document out.

## Points that held

- The names of the declarations pages (John Doe, Susy Doe, Frank Stallings, John E. Doe, John D Sample, Jane A Sample),
  the property street (`1427 Sample Drive`) and the agent's street (`2690 Pennsylvania`): replaced in the digital outputs
  (the scanned DC copy keeps its names, see 5).
- The certificate: sponsor and signatory replaced in the text layer and in the picture.
- The product sheet and the general conditions: no personal value to leak (there is none in them).
- Detector controls: a heading of two capitalised words, a sentence of the conditions and an e-mail label give no name
  when they are read alone.

## Environment notes

- Python's TLS store refuses `www.oid.ok.gov` (an expired certificate in the chain); curl accepts it. The fetch script
  falls back to curl. The sha256 check is what makes the file trusted, whichever tool fetched it.
- `tests/real-public-documents` (the first branch name used here) now also holds a commit by another agent, with the
  same content as `fa25ce3` on `main`. It was left alone; this branch starts from `main`.
- `tools/check_all.py`: the `KEEP` dictionary repeats the key `Dossier Test 15` (harmless).

## Where the expectations are

`tools/check_public_docs.py`: `SPEC` (one entry per document: secrets, keep, mode, black boxes, no_person),
`SCANS` (the scanned copies), `DETECTOR_CASES`, `DETECTOR_ABSENT`, `DETECTOR_CONTROLS` (the strings of the documents,
no PDF). The script is not wired into `check_all.py`; run it on its own.
