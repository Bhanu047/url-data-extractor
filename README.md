# URL Data Extractor

**Live app:** https://bhanu-url-data-extractor.onrender.com

**Source:** https://github.com/Bhanu047/url-data-extractor

The hosted app requires its private access code. The code is not stored in GitHub.

Paste a public URL, choose a numbered range such as **1 to 20**, and download tables, labelled fields, and structured data. Jobs persist on disk and can resume. Python standard library only; no API key or record-count cutoff.

## Run

The hosted version uses an access code with signed, HTTP-only cookies. Its code is held in the hosting environment, never in this repository. Free hosting uses temporary disk: download exports before a restart, spin-down, or redeploy. Long bulk jobs and durable recovery require running locally or adding persistent hosting storage.

On Windows, extract the ZIP and double-click **START_WINDOWS.bat** to open the app in your browser. Click **Fill sample URLs**, then **Start extraction**. Use **JSON Records CSV** for a row per fictional user. See START_HERE.md for a quick walkthrough.

Install Python 3.11+ and open a terminal in this repository's folder:

```powershell
py app.py
```

On macOS/Linux use `python3 app.py`. Open **http://127.0.0.1:8765** in your browser. Paste your link into **Source URL**. Keep the terminal running while extracting. Ctrl+C stops the app; `--port 9000` selects another local port.

## Start and end

The default **Start URL → End URL** mode accepts two complete links directly:

```text
Start URL: https://example.com/people/1
End URL:   https://example.com/people/100000
```

This plans all 100,000 URLs in order, including both endpoints. Use an end of 10, 20, 1000, or any supported larger ID. Only the final numeric path segment may differ; the remaining URL must match. Consistent leading zeros are preserved. One URL can contain many records. A 100,000-URL run is not limited to 100,000 rows. At the default one-second delay, 100,000 requests take at least about 28 hours, plus download/processing time and retries; pause and resume as needed.

Tests verify all 100,000 generated URLs without network requests, actual fixture collection for a direct 1–20 URL range, and the separate 50,020-row fixture extraction/export test. A real 100,000-page website has not been crawled as part of these checks.

Choose **Numbered pages / IDs** and replace the changing number with `{page}`:

| Source pattern | Start | End | Requests |
| --- | --- | --- | --- |
| `https://example.com/records?page={page}` | 1 | 20 | All 20 pages, inclusive |
| `https://example.com/profile/{page}` | 100 | 200 | All 101 profile IDs, inclusive |
| `https://example.com/results/{page}/list` | 1 | 20 | All 20 result URLs |

Other numbers in the URL stay unchanged. **One URL** extracts one HTML/JSON response. **Follow Next links** follows same-origin HTML `rel=next` or recognizable English Next anchors until none remains. Forms, cursor-based APIs, page offsets, localized controls, and JavaScript pagination may need a site adapter. Arbitrary linked detail pages are not automatically visited.

## Extracted data

HTML tables; explicit label/value pairs and definition lists (including Name, DOB, From, To when present); microdata; valid embedded JSON/JSON-LD; original JSON responses and flattened fields; title, metadata, headings, links, image URLs/alt text, and readable text. Source URLs and page numbers are retained. Missing names and dates are never guessed from prose.

The first all-`th` row becomes a table header; common row/column spans are expanded. Complex multirow headers or malformed HTML may need cleanup. Two-column tables are additionally exposed as label/value fields; inspect their meaning before treating them as person records. Different extracted views can overlap; nothing is silently deduplicated.

## 50,000+ records

There is **no 50,000-record cutoff**. The regression suite extracts **50,020 table rows across 20 fixture pages**, checks the full CSV row count, and verifies the last record. Results commit to SQLite after each page. Exports stream page by page; the interface preview limit never truncates exports.

Capacity depends on disk, memory, and the website. Each HTML/JSON response has a **100 MB** default local download limit (10 MB on the free hosted instance) and is parsed in memory; extremely large or deeply nested individual responses can fail. Use pagination for large datasets. That limit applies per response, not to the entire multi-page job. Table rows, fields, and JSON array objects are distinct counts; the UI does not claim its table-row count measures every JSON object.

## Downloads

Pause or finish a job before downloading a consistent snapshot:

- **Full JSON**: job summary, every saved page and its original structured JSON, text, tables, fields, and failed-page errors.
- **Full JSONL**: one summary line, then one line per page.
- **Tables CSV**: all table rows, unioned column names, source URL, requested page, table number, caption, row number.
- **Fields CSV**: every labelled/flattened field with its source.
- **JSON Records CSV**: a row per top-level JSON object or array item, including flattened nested fields. Embedded JSON blocks are handled separately. Nested arrays are flattened by index, not expanded into separate records; full JSON retains original nesting.

A JSON response might have no HTML tables; use Full JSON or Fields CSV for it. Formula-like CSV cells receive an apostrophe prefix to prevent spreadsheet formula execution; JSON preserves original values. Failed pages appear in JSON, not as rows in data CSVs. CSV alone does not prove completeness.

## Resume and failures

Jobs are stored in `data/jobs.sqlite3` on your computer. After a process restart, interrupted jobs appear as paused. **Resume / retry failures** keeps successful pages and retries failed or unvisited pages. One job runs at a time, with three attempts per page and a one-second delay between pages.

Failed range pages produce `completed_with_errors`. Next-link jobs stop at a failure because the next URL is unknown; resuming retries it. Pagination loops stop with a visible error. JSON exports retain every failed-page error; the UI lists the first 100 failures. Never treat a partial export as a complete collection.

Completion means the specified URLs were processed, not that the website returned distinct or exhaustive records. A site can return repeated pages, challenges, or empty results with HTTP 200. Verify a sample against the source.

## Access and limitations

This personal deployment excludes Instagram. It is intended for public test data and sources the operator is authorized to export.

Public HTTP(S) HTML and JSON on ports 80/443 only. No JavaScript execution, login/cookies, CAPTCHA bypass, PDFs/images, or arbitrary crawling. JavaScript tables, infinite scroll, API cursor pagination, and authenticated sites need adapters. No generic extractor can guarantee every field from every website.

The fetcher has a 30-second download budget and a 100 MB per-response cap; OS DNS resolution can add resolver time. It blocks private/local addresses, revalidates each redirect, pins resolved IPs, and validates HTTPS hostnames. Local mode binds only to loopback. Hosted mode requires a strong APP_ACCESS_CODE and an exact RENDER_EXTERNAL_HOSTNAME, validates Host/Origin, and protects API routes and downloads using signed session cookies. The hosted version is a personal preview, not a general multi-tenant service.

Source records remain in the app database and are not logged. Local mode stores them on your computer; hosted mode stores them on the hosting instance. `data/` and common export files are ignored by Git. Do not commit extracted personal data to the repository. Use only data you are authorized to access and respect the site's access rules.

## Tests

```sh
python -m unittest discover -s tests -v
```

Covers 50,020-row collection and export, inclusive ranges, durable resume/retry, Next links and loops, structured fields, JSON, table spans, Unicode, CSV formula protection, private-address/redirect blocking, and local HTTP endpoints. Tests use fixtures and do not establish compatibility with an untested live website. GitHub Actions runs this suite on Python 3.11–3.13.
