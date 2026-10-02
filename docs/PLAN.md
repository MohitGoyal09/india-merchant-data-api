# India Merchant Data API: Build Plan

Status: **Proposed** · Owner: Mohit Goyal · Date: 2026-10-02
Context: Razorpay FDE (Agent Studio) take-home, Option 1, "reverse-engineer an API".

## 1. Problem

Indian merchants ask two questions every day:

1. "When will my settlement arrive?" The answer depends on bank holidays for each region.
2. "What is this foreign-currency payment worth in INR on that date?" The answer depends on the official reference rate.

The official sources are RBI and FBIL. Neither has a public API:

- RBI serves bank holidays and reference rates through ASP.NET WebForms pages.
- FBIL serves reference rates and MIBOR through an Angular single-page app (SPA).

This project reverse-engineers both into one typed, time-based, versioned API. Phase 2 adds an MCP layer, so an Agent Studio agent can use the same data as tools.

## 2. Goals and non-goals

**Goals**

- G1. Reverse-engineer 2 sites that use opposite web styles, behind one shared adapter framework.
- G2. Build an FX time-series dataset from 2000 to today. Backfill it once, refresh it daily, and record the source of every row.
- G3. Provide a bank-holiday calendar for 34 RBI regional offices, a business-day engine, and settlement ETA.
- G4. Provide a cross-border invoice helper: an amount, a currency and a date give the INR value plus the settlement date.
- G5. Production behaviour:
  - provenance on every response
  - degraded mode when a source fails
  - a drift canary
  - source failover
  - signed webhooks
  - an ICS feed
- G6. Provide a test-case script that runs in offline mode (fixtures) and in live mode.
- G7. Phase 2: an MCP server, an agent demo, and evals.

**Non-goals**

- No hosted public deployment. We do not publish a data mirror (see §9).
- No FX forecasting or ML.
- No multi-tenant auth or client SDK. The OpenAPI spec comes free from FastAPI.
- No NPCI, no RBI PSI XLSX files, no GST data. They are blocked by bot protection or captcha, or their terms ban scraping. See `LIMITATIONS.md`.

## 3. Verified source map (probed 2026-10-02)

| Source | Endpoint | Method | Key inputs | Output | Range |
|---|---|---|---|---|---|
| RBI holidays | `https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx` | GET the hidden fields, then POST the form | `drRegionalOffice` (0 = all, or 1 of 34 office ids), `drMonth` (0 = all, or 1–12), `drYear` (2001–2026), `btnGo=GO` | HTML in 2 layouts: a month matrix (all offices), or an office list for the year | 2001–2026 |
| RBI FX | `https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx` | GET the hidden fields, then POST the form | `chkUSD/GBP/EURO/YEN/AED/IDR=on`, `txtFromDate`/`txtToDate` in `DD/MM/YYYY`, `btnSubmit=" GO "` | HTML table, newest first | 2000-01-03 to 2018-07-24, then 2022-04-12 to today |
| FBIL FX | `https://www.fbil.org.in/wasdm/refrates/fetchfiltered?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&authenticated=false` | GET | ISO dates only. Other formats return HTTP 500 with a Java stack trace. | JSON `[{processRunDate, subProdName:"INR / 1 USD", displayTime, rate}]` | 2018-07-10 to today |
| FBIL MIBOR | `/wasdm/ovnmibor/fetchfiltered?...` | GET | same as FBIL FX | JSON `[{processRunDate, tenor:"O/N", displayTime, rate}]` | 2015-07-22 to today |

Shared facts:

- No captcha, token or cookie is needed on any data call.
- Both sites return 200 to an honest User-Agent: `india-merchant-data-api/0.1 (+contact)`.
- Units: JPY is quoted per 100, and IDR per 10,000 on RBI. The FBIL label carries the unit (`INR / 100 JPY`).
- On 2026-10-02 the latest FBIL row was dated 2026-09-24. The freshness check must handle a lag like this.

Settlement rule (Razorpay docs): T+2 working days. Sundays, the 2nd and 4th Saturdays, and bank holidays do not count as working days. A settlement that falls on a holiday moves to the next working day. In our engine the cycle and the rules can be configured.

## 4. Architecture

```
            ┌────────────── sources (one adapter per dataset) ──────────────┐
 RBI HTML ──► rbi/aspnet.py (hidden-field postback) ─► rbi/holidays.py, rbi/fx.py
 FBIL JSON ─► fbil/fx.py, fbil/mibor.py                                       │
            └── all use http/client.py: PoliteClient ──────────────────────────┘
                     │  1 req / 2 s per host · backoff + jitter · circuit breaker
                     ▼
   ingest/ (backfill, refresh) ──► store/ (SQLite: rows + fetch_log + source_health)
                     │                         ▲
                     ▼                         │ reads only
   health/drift.py (canary, fingerprints)   domain/ (calendar, settlement, fx_service)
   events/webhooks.py (HMAC-signed)            │
                                               ▼
                               api/ (FastAPI, envelope + provenance)   mcp/ (Phase 2)
```

**Key decisions**

| # | Decision | Why |
|---|---|---|
| D1 | The API reads from local SQLite, never from the upstream site during a request. Ingest fills SQLite. | Requests stay fast, upstream load stays bounded, and degraded mode is natural: we serve the last good data. |
| D2 | Synchronous code throughout: httpx sync and stdlib `sqlite3`. FastAPI runs sync routes in a thread pool. | KISS. Ingest runs one call at a time anyway because of the politeness limit. |
| D3 | `SourceAdapter` protocol: `build_requests(params) → fetch(req) → parse(raw) → validate → normalize`, plus `fingerprint(raw)`. | Adding a source takes about 1 file. MIBOR proves it. |
| D4 | `source=auto` merge precedence: FBIL for dates from 2018-07-10 on, RBI for earlier dates. Every row keeps `source`. A `compare` endpoint shows where they differ. | FBIL has administered the benchmark since July 2018. |
| D5 | Money math uses `Decimal` only, and stores the rate with its unit (`per 1`, `per 100`, `per 10000`). | No float drift in conversions. |
| D6 | A business day is not a Sunday, not a 2nd or 4th Saturday (rule from 2015-09-01 on), and not an RBI-listed holiday for that office. | This matches the Razorpay settlement docs and RBI practice. |
| D7 | Write endpoints (webhooks, manual refresh) need an `IMDA_ADMIN_TOKEN` bearer token. Read endpoints are open on localhost. | The smallest auth that is still safe. |
| D8 | We ship the backfill script, not the data. Test fixtures are small recorded samples. | Respects the RBI "no caching" clause and the FBIL redistribution terms. |

## 5. Repository layout

```
india-merchant-data-api/
├── pyproject.toml  uv.lock  Makefile  Dockerfile  docker-compose.yml  .env.example
├── src/imda/
│   ├── config.py                 # pydantic-settings
│   ├── models.py                 # Office, Holiday, FxRate, Mibor, Provenance, SourceStatus
│   ├── http/client.py            # PoliteClient
│   ├── sources/base.py           # SourceAdapter protocol, RawPayload, ParseError
│   ├── sources/rbi/{aspnet,offices,holidays,fx}.py
│   ├── sources/fbil/{fx,mibor}.py
│   ├── store/{db,schema.sql,repo}.py
│   ├── ingest/{backfill,refresh}.py
│   ├── domain/{calendar,settlement,fx_service,invoice}.py
│   ├── health/{drift,baselines.json,freshness}.py
│   ├── events/webhooks.py
│   ├── api/{app,envelope,errors,deps}.py + api/routes/*.py
│   └── cli.py                    # typer: backfill · refresh · serve · canary
├── tests/{unit,contract,api}/ + tests/fixtures/{rbi,fbil}/
├── scripts/run_cases.py          # the assignment's test-case script
└── docs/{PLAN,ARCHITECTURE,REVERSE_ENGINEERING,LIMITATIONS,API}.md
```

## 6. API surface (v1)

Every success response uses this envelope:

```json
{ "data": ..., "meta": {"count": 0, "next_cursor": null, "degraded": false, "warnings": []},
  "provenance": [{"source": "fbil", "dataset": "fx_reference_rates", "source_url": "...",
                  "fetched_at": "...", "stale": false}] }
```

Every error response uses this shape: `{"error": {"code": "INVALID_DATE", "message": "...", "details": {}}}`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/offices` | The 34 RBI regional offices: slug, name, state, RBI id |
| `GET /v1/holidays?office=&year=&month=` | Holidays: date, name, type (NI Act or closing of accounts) |
| `GET /v1/calendar/business-day?date=&office=` | Is it a working day? If not, why (Sunday, 2nd Saturday, the holiday name) |
| `GET /v1/calendar/next-business-days?date=&office=&n=` | The next N working days |
| `GET /v1/calendar/{office}.ics?year=` | Subscribable calendar feed |
| `GET /v1/settlement/eta?captured_at=&office=&cycle_days=2` | The settlement date, plus each skipped day and the reason |
| `GET /v1/fx/rates?currency=&from=&to=&source=auto\|rbi\|fbil&cursor=` | History with pages (JSON, or CSV with `Accept: text/csv`) |
| `GET /v1/fx/rates/as-of?currency=&date=` | The rate in force on a date: `effective_date` plus the reason it differs (weekend, a holiday name, not yet published, source gap) |
| `GET /v1/fx/convert?amount=&from=&to=&date=` | INR to or from a foreign currency, as a `Decimal` string |
| `GET /v1/fx/stats?currency=&from=&to=&period=week\|month` | Average, minimum, maximum, and volatility of daily log returns |
| `GET /v1/fx/compare?currency=&from=&to=` | RBI against FBIL on overlapping dates, with the difference and a flag |
| `POST /v1/invoice/quote` | Body: amount, currency, invoice_date, captured_at, office. Returns the INR value, the rate used, and the settlement ETA. |
| `GET /v1/rates/mibor?from=&to=` | Overnight MIBOR (FBIL) |
| `GET /v1/sources/health` | For each source: status ok, degraded or broken; last success; last error; fingerprint drift; freshness |
| `POST /v1/webhooks` · `GET /v1/webhooks` · `DELETE /v1/webhooks/{id}` · `GET /v1/webhooks/{id}/deliveries` | Subscriptions (admin token) |
| `POST /v1/admin/refresh` | Trigger an incremental refresh (admin token) |

Validation:

- ISO dates only.
- `from` must not be later than `to`.
- At most 1,000 rows per page.
- Currency enum: USD, GBP, EUR, JPY, AED, IDR.
- The office slug must exist.
- The amount must be a positive decimal with 2 decimal places or fewer.

## 7. Production behaviour

- **Provenance:** every stored row carries `source`, `source_url`, `fetched_at` and `fetch_id`, which links to `fetch_log` (request params, HTTP status, bytes, sha256, duration).
- **Degraded mode:** if a refresh fails (network error, parse error or drift), we keep the last good rows and mark `source_health = degraded`. Responses then set `meta.degraded = true` and add a warning. The API never returns 500 because an upstream source failed.
- **Freshness:** the expected latest publish date is the last Mumbai working day that is already past 13:30 IST (FBIL publish time). A source is `stale` if it is behind that date. The holiday calendar drives this check.
- **Drift canary:** `imda canary` fetches 1 small sample from each source. It then:
  - fingerprints the sample. For RBI that is the form field names, the dropdown sizes and the table header shape. For FBIL it is the JSON keys and their types.
  - compares the fingerprint to `baselines.json`, and checks that the sample still parses.
  - writes the result to `source_health`.
  - emits `source.degraded` or `source.recovered` when the status changes.
- **Failover:** if the FBIL refresh fails, refresh RBI for the same window (RBI covers 2022 onward), and the reverse. `auto` merge uses whatever is present.
- **Politeness:**
  - 1 request per 2 s per host.
  - An honest User-Agent with contact details.
  - Exponential backoff with jitter on 429, 5xx and timeouts, at most 4 attempts.
  - A circuit breaker opens after 5 failures and stays open for 10 minutes.
  - A hard per-run request budget, and a kill switch `IMDA_UPSTREAM_ENABLED=false`.
- **Webhooks:**
  - Events: `fx.rates.published`, `holidays.updated`, `source.degraded`, `source.recovered`.
  - Body: JSON with `id`, `event`, `created_at` and `payload`.
  - Header `X-IMDA-Signature`: hex HMAC-SHA256 of the raw body, signed with the subscription secret. This is the same scheme as Razorpay's `X-Razorpay-Signature`.
  - 3 retries with backoff, and a delivery log.
  - SSRF guard: reject private, loopback and link-local targets unless `IMDA_ALLOW_PRIVATE_WEBHOOKS=true` (dev only).
- **Logs:** JSON lines with a request id. No secrets in logs.

## 8. Milestones (commit at every green gate)

| M | Scope | Owner | Exit gate |
|---|---|---|---|
| M0 | Scaffold: uv, ruff, mypy, pytest-cov, Makefile, CI, `config.py`, `models.py`, `sources/base.py` contracts | Orchestrator | `make check` green, contracts reviewed |
| M1 | `PoliteClient` and `rbi/aspnet.py` hidden-field helper (TDD with respx) | Agent | Rate-limit, retry, breaker and budget tests pass |
| M2a | RBI offices, holidays adapter, both layouts, recorded fixtures | Agent A | Parses fixtures. 1 live smoke test passes. |
| M2b | RBI FX adapter plus FBIL FX and MIBOR adapters, recorded fixtures | Agent B | Units normalized, ISO validation, fixtures pass |
| M2c | `domain/calendar.py` and `domain/settlement.py` (pure logic, TDD) | Agent C | Table-driven tests: 2nd and 4th Saturdays, the 2015 rule change, holiday chains, year boundaries |
| M3 | SQLite store, `fetch_log`, backfill and refresh CLI | Agent | Backfill 2024–2026 runs live. Re-running it is idempotent. |
| M4 | `fx_service` (as-of, convert, stats, compare, auto merge) and `invoice.py` | Agent | Decimal tests, holiday-reason tests |
| M5 | FastAPI routes, envelope, errors, CSV, ICS | Agent | API tests for every endpoint and error code |
| M6a | Health: drift canary, freshness, degraded mode, failover | Agent D | Fault-injection tests: a broken fixture leads to degraded and no 500 |
| M6b | Webhooks: HMAC, retries, SSRF guard, admin token | Agent E | Signature verification test, SSRF rejection tests |
| M7 | `scripts/run_cases.py`, Docker, docs (README, REVERSE_ENGINEERING, LIMITATIONS, ARCHITECTURE) | Agent | `make cases` all PASS offline. Live run is recorded. |
| M8 | Review: code-reviewer, python-reviewer and security-reviewer, then fixes | Agents | No CRITICAL or HIGH findings left |
| P2 | MCP server (FastMCP, stdio and streamable HTTP, `readOnlyHint`), agent demo, evals | Agents | Eval pass rate meets the bar set before building |

Parallel waves (no 2 agents edit the same file):

1. Wave 1: M0, then M1.
2. Wave 2: M2a, M2b and M2c.
3. Wave 3: M3, then M4.
4. Wave 4: M5.
5. Wave 5: M6a and M6b.
6. Wave 6: M7, then M8.

## 9. Test plan

- **Unit:** parsers against recorded fixtures, the calendar and settlement tables, Decimal FX math, HMAC, the SSRF guard, the breaker and the backoff.
- **Contract:** each adapter's parse output is checked against its pydantic model, and its fingerprint against `baselines.json`.
- **Fault injection:**
  - truncated HTML
  - renamed JSON keys
  - 500 with a stack trace
  - 429
  - timeout

  Expected result: degraded responses, never a 500.
- **API:** every endpoint, on both the success path and each error code.
- **Live (opt-in, `make test-live`):** at most about 10 upstream calls, rate-limited.
- **`scripts/run_cases.py`:** about 25 named cases. It prints a PASS/FAIL table and exits non-zero on any failure. Examples:
  - The USD rate as-of 2026-01-26 returns the rate from 2026-01-23, with the reason "Republic Day".
  - Settlement for a payment captured on Friday 2026-03-27 in Mumbai skips 28 Mar (4th Saturday), 29 Mar (Sunday) and Tue 31 Mar (a Mumbai holiday per the RBI probe). Whether 1 Apr (banks' closing of accounts) also counts as a non-working day is a rule we decide and test in M2c.
  - An invalid date format returns `INVALID_DATE`.
  - JPY conversion respects the per-100 unit.
  - A webhook signature verifies.

  These are examples only. The final expected values come from fixtures, and the calendar rules (including 1 Apr) are checked against RBI data during M2c.
- **Coverage:** at least 80% on `src/imda`.

## 10. Risks

| Risk | Mitigation |
|---|---|
| RBI blocks our IP | Low request rate, budget, kill switch. Offline fixtures keep the demo and tests working. |
| The site layout changes | Drift canary, degraded mode, a parser per layout, fingerprint baselines |
| FBIL data lags | The freshness flag, plus RBI failover for 2022 onward |
| Licence and terms | No redistribution, no hosted mirror, a source citation on every response. `LIMITATIONS.md` states that production use needs an FBIL licence. |
| Ambiguity in the Razorpay rule | The engine can be configured. The docs say it is an estimate, not Razorpay's real engine. |

## 11. Long-term fix (goes in LIMITATIONS.md)

The real fix is official machine-readable feeds:

- an RBI holiday calendar in ICS or JSON
- an FBIL licensed data feed, or vendor distribution
- DBIE API access

Until then this service is the adapter layer. Each source sits behind the adapter interface, so switching a source to an official feed means replacing 1 adapter. The API contract does not change.
