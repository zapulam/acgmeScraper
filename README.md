# ACGME Program Contact Scraper

Scrapes public ACGME accreditation program search results by state, opens each program detail page with a reusable Playwright browser session, and saves program leadership/contact rows to a resumable JSON checkpoint.

The scraper collects ACGME public data only. It leaves missing email or phone values blank and stores raw contact rows in the JSON checkpoint. Dedupe is applied only when converting the checkpoint to CSV.

## Setup

```powershell
cd acgmeScraper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

`requests` and `BeautifulSoup` fetch and parse state search result lists. Playwright opens program detail pages using browser-managed session behavior.

## Run

Full scrape:

```powershell
python python scrape_acgme_contacts.py --checkpoint data/acgme_checkpoint.json --delay 1.0
```

Small smoke test:

```powershell
python python scrape_acgme_contacts.py --states CA --max-programs 3 --delay 0.2 --checkpoint data/acgme_checkpoint_smoke.json
```

Resume an interrupted run by rerunning the same command. Completed program details are skipped unless `--force` is provided.

During scraping, the CLI uses Rich formatting with a compact banner, run configuration, per-state progress bars, and live counters for contacts, skipped programs, and errors. The JSON checkpoint is saved after each completed program and keeps the raw rows collected from program pages.

## Export CSV

Convert a completed or partial checkpoint to CSV:

```powershell
python checkpoint_to_csv.py --checkpoint data/acgme_checkpoint.json --output data/acgme_contacts.csv
```

CSV export dedupes by normalized email globally. Rows with the same email are merged, and distinct programs, roles, names, phone numbers, and source URLs are combined with `; ` in first-seen order. Contacts without an email stay separate by `Program Code + Role + Name`.

The export command also uses Rich formatting and reports raw checkpoint rows plus final deduped CSV rows.

Smoke-test checkpoint export:

```powershell
python checkpoint_to_csv.py --checkpoint data/acgme_checkpoint_smoke.json --output data/acgme_contacts_smoke.csv
```

## CLI Options

Scraper options:

```text
--checkpoint PATH   JSON checkpoint path for resume/retry.
--states LIST       Optional state names, abbreviations, or ACGME state IDs.
--delay SECONDS     Base delay between live ACGME requests. Default: 1.0.
--force             Re-scrape programs already marked complete.
--max-programs N    Stop after N programs for smoke tests.
```

CSV export options:

```text
--checkpoint PATH   JSON checkpoint path to read.
--output PATH       CSV output path to write.
```

## CSV Columns

```text
Program Code, Program Name, Specialty, State, City, Role, Name, Email, Phone, Source URL
```

## Tests

```powershell
python -m unittest discover -s tests
```

The tests use synthetic ACGME-like HTML and do not hit the live ACGME site.
