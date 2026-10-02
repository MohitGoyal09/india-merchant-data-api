# How the APIs were reverse-engineered

This file records how each source was mapped, what each request really looks like, and what changed
in the sites over 26 years of data. All of it was done on public pages, with an honest User-Agent,
at one request every 2 seconds or slower.

## Method

1. **Look first, fetch second.** Open the page in a normal browser, watch the Network tab, and
   read the page source. Note every form field, hidden input and XHR call.
2. **Reproduce with a plain HTTP client.** Repeat the call with `httpx` and an honest
   User-Agent (`india-merchant-data-api/0.1 (+repo URL)`). If a plain client is blocked
   (bot challenge, captcha, 403), the source is **out of scope**. We never get around blocks.
3. **Record fixtures.** Save one real response per layout under `tests/fixtures/`, plus the
   request that produced it (`*.meta.json`). Every parser is tested against real bytes.
4. **Fingerprint the shape.** Each adapter emits a structural fingerprint (form fields, dropdown
   sizes, table shape, JSON keys and types, no data values). `src/imda/health/baselines.json`
   stores the expected shape, and drift is checked on every ingest and by `imda canary`.
5. **Backfill, then let the data find the edge cases.** The full 2000–2026 backfill found three
   layout changes that no single sample showed (see "Drift found in the wild").

## Site 1: RBI (server-rendered ASP.NET WebForms)

### Bank holidays: `https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx`

| Step | Request | Notes |
|---|---|---|
| 1 | `GET` the page | Collect the hidden inputs `__VIEWSTATE` (~38 KB), `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`, `__EVENTTARGET`. |
| 2 | `POST` the same URL, form-encoded | Hidden inputs, plus `drRegionalOffice` (0 = all, or 1 of 34 office ids), `drMonth` (0 = all, or 1–12), `drYear` (2001–2026), and `btnGo=GO`. |

- `__EVENTVALIDATION` makes the server reject any dropdown value it did not offer. So a year that
  is not in the `drYear` list fails, and the client never sends one.
- "All offices" together with "all months" is rejected by the page's own script. We query either
  one month for all offices (a matrix) or one office for all months (a list).
- A POST response carries fresh hidden fields. `PostbackSession` (`sources/rbi/aspnet.py`) reuses
  them for the next POST and makes a new GET only when they are missing or rejected. This halves
  the request count.
- **Four response layouts**, each with its own parser and fingerprint baseline:
  - **Month matrix** (all offices, one month): only days that have a holiday appear as columns.
    A second table maps day to holiday name.
  - **Office list** (one office, all months): month header rows, then `day | name | marker` rows.
  - **No holidays**: the text "There are no holidays in June 2005". It returns `[]`, but only if
    the month and year in the text match the request.
  - **Truncated page** (no `</html>`): a parse error, never silent data loss.
- Marker meaning comes from the hidden legend text (`span.HideText`), **not** the glyph, because
  the glyphs change over the years (see below).
- RBI publishes holidays per **regional office** (34 cities), not per state. `OFFICE_STATE` maps
  each office to its state.

### FX reference rates: `https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx`

- Same GET-then-POST flow. Fields: `chkUSD/chkGBP/chkEURO/chkYEN/chkAED/chkIDR=on`, then
  `txtFromDate`/`txtToDate` in `DD/MM/YYYY`, then `btnSubmit=" GO "`. The date inputs are
  `readonly` in the browser (a date picker), but the server accepts posted values.
- The unit is read from the column header (`USD (INR / 1 USD)`, `YEN (INR / 100 YEN)`,
  `IDR (INR / 10000 IDR)`). It is never assumed.
- **Coverage gap:** RBI shows no rates from 2018-07-25 to 2022-04-11. Backfill skips this window
  to save requests, and FBIL fills it.
- **Finding:** over 176 overlapping days in 2026, RBI's rate equals FBIL's rate exactly
  (0.0000 bps difference). RBI republishes the FBIL benchmark. See `GET /v1/fx/compare`.

### Excluded RBI dataset: Payment System Indicators (UPI volumes)

The index page `PSIUserView.aspx` is public. The XLSX files sit on `rbidocs.rbi.org.in`, which sends
non-browser clients an F5 bot-defence JavaScript challenge. Passing it would mean getting around
bot protection, so this dataset is out of scope.

## Site 2: FBIL (Angular single-page app over a Spring REST backend)

The HTML at `https://www.fbil.org.in/` is an empty `<app-root>`, and all data comes from XHR calls.

1. **Read the bundle.** `main.<hash>.js` (~5 MB) holds the API base URL as a constant
   (`https://www.fbil.org.in/wasdm`) and every route (`/refrates/fetchfiltered`,
   `/ovnmibor/fetchfiltered`, `/tbill/...`, and others).
2. **Check for gates.** reCAPTCHA is loaded, but in the bundle it is used only in the complaint,
   whistle-blower and careers forms. The Angular interceptor adds a Bearer token only for admin
   users. Data calls need no token, cookie or captcha, and the response sends
   `access-control-allow-origin: *`.
3. **Map parameters by trial.** `fromDate`/`toDate` must be `YYYY-MM-DD`. Any other format
   returns **HTTP 500 with a full Java stack trace**, so our code validates dates before every
   request. `authenticated=false` is required.

| Dataset | Endpoint | Data from |
|---|---|---|
| FX reference rates | `GET /wasdm/refrates/fetchfiltered?fromDate=&toDate=&authenticated=false` | 2018-07-10 |
| Overnight MIBOR | `GET /wasdm/ovnmibor/fetchfiltered?...` | 2015-07-22 |

Quirks handled:
- `subProdName` labels carry the unit (`INR / 100 JPY`). One variant has no spaces (`INR/1 USD`,
  12 rows in 2021).
- On Fridays, MIBOR has tenor `3D` (over the weekend), not `O/N`.
- RUB rows exist but are outside our currency list. They are counted and skipped.
- There is no pagination: a 2010-to-today query returns 8,296 rows (1.1 MB) in one response.
  We still split requests into 1-year chunks.
- **Lag:** on 2026-10-02 FBIL's latest FX row was 2026-09-24, while RBI had 2026-10-01. The
  freshness check flags it, and `source=auto` fails over to RBI by itself.

## Drift found in the wild (by the full backfill)

| Year | What changed | Fix | Regression fixture |
|---|---|---|---|
| 2005 | Empty months show the text "There are no holidays in June 2005", not a table | New `no_holidays` layout that checks month and year against the request | `holidays_all_2005_06_empty` |
| 2007 | New marker `▲` = "Holiday under NI Act **and** RTGS holiday" | Classify by hidden legend text, not by glyph | `holidays_all_2007_01_rtgs` |
| 2018 | New marker `◆` = "RTGS holiday" only (banks open) | Not a bank holiday for business days or settlement | `holidays_all_2018_04_rtgs_only` |

Each one stopped the ingest with a clear `ParseError`. Data was never stored wrongly. Each became a
fixture and a test in a few minutes. This is the drift-detection design working as intended.

## Cost of the full history

The whole dataset needed **380 upstream requests** (41 MB, about 330 ms per request on average):

- 2000–2026 FX (31k rows)
- 2015–2026 MIBOR
- 2001–2026 holidays for 34 offices (13k holidays)

A daily `imda refresh` needs about 6–8 requests.

## Sources checked and rejected

| Source | Why rejected |
|---|---|
| NPCI (`npci.org.in`, UPI statistics) | Akamai returns 403 "Access Denied" to non-browser clients. That is bot protection. |
| RBI PSI XLSX (`rbidocs.rbi.org.in`) | F5 bot-defence challenge on file downloads |
| GST portal HSN/SAC search | The search needs an image captcha. The typeahead is behind F5 bot defence. The terms name "web scraping" as unauthorised. |
| DBIE (`data.rbi.org.in`) | An undocumented internal `/api/services/queryInterface`. Not called. |
| `data.gov.in` | Has an official API, so it is not a "no public API" target |
