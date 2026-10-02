# Limitations and the long-term fix

This service is an adapter layer over two public websites that offer no API. It is honest about
what that means.

## 1. Fragility: the sites can change at any time

- The parsers depend on HTML structure (RBI) and an undocumented JSON backend (FBIL). Either can
  change without notice.
- **Mitigation:**
  - A structural fingerprint check on every ingest, plus `imda canary` against
    `health/baselines.json`.
  - A parser for each known layout.
  - Parse errors stop the ingest for that dataset only. The data is not stored wrongly.
  - The API keeps serving the last good data with `meta.degraded = true`, and `source.degraded`
    webhooks fire.
- The full backfill already found three such changes in RBI's pages (2005, 2007 and 2018; see
  `REVERSE_ENGINEERING.md`). Each took minutes to fix, because the failure was loud and typed.
- **Residual risk:** a change that keeps the same shape but changes meaning (for example, a column
  swap) would not show up as drift. The RBI-vs-FBIL `compare` check catches this for FX, because
  the two agree to 0 bps today. Holidays have no second source.

## 2. Access and terms

- **RBI**
  - The [disclaimer](https://www.rbi.org.in/Scripts/Disclaimer.aspx) says RBI may block any IP
    and that "caching … [is] prohibited".
  - `robots.txt` cannot be read: it returns an "Unauthorised Access" page.
  - We keep the request rate low (1 every 2 s, a request budget, a circuit breaker and a kill
    switch), cite RBI as the source on every response, and **ship the backfill script, not the
    data**. Each user builds their own local copy.
- **FBIL**
  - The FAQ states the benchmarks are FBIL's property, and that display or redistribution needs
    FBIL's authorisation and a fee.
  - This repo is a local research demo: no hosted mirror, no redistribution.
  - **Production use needs an FBIL data licence.**
- We use an honest User-Agent with a contact URL, and never get around bot protection, captchas or
  logins. Sources that need any of that were excluded (NPCI, RBI PSI files, GST portal).

## 3. Data gaps and freshness

- RBI FX has no rates from 2018-07-25 to 2022-04-11, and FBIL starts on 2018-07-10. `source=auto`
  merges them into one continuous series from 2000.
- FBIL can lag. On 2026-10-02 its latest row was 2026-09-24, while RBI had 2026-10-01.
  `stale: true` shows on the provenance, and `auto` fails over to RBI.
- RBI's holiday dropdown covers 2001 to the current year. Next year's holidays appear when RBI
  publishes them (usually in December). The canary flags the new year (`holidays.year_available`,
  status stays `ok`) and `imda refresh` loads it. `imda backfill --datasets holidays --from
  2027-01-01 --to 2027-12-31` loads it by hand ([RUNBOOK.md](RUNBOOK.md) section 9).
- AED and IDR rates start on 2026-01-05.

## 4. Domain simplifications

- **Settlement ETA is an estimate, not Razorpay's settlement engine.** It applies the published
  rule: T+N working days, where Sundays, 2nd and 4th Saturdays and bank holidays are not working
  days.
  - Razorpay's docs text ("T+2 working days") and its worked example (captured Sat 2019-02-02,
    settled Mon 2019-02-04) disagree. So both modes exist: `working_days` (the default) and
    `calendar_then_roll`.
  - A real deployment would confirm the merchant's actual cycle and cut-off time.
- **Holiday names are RBI's combined names.** RBI publishes one name per date that joins the
  festivals of every region (for example "Gudhi Padwa/Ugadi Festival/Telugu New Year's Day/…" on
  2026-03-19). Its per-office list uses the same combined string. Dates and holiday kinds are
  per office, and they are correct. Only the display name is shared across regions.
- **Holidays are per RBI regional office (34 cities), not per bank branch or state.** A merchant's
  settlement depends on its bank's location. Mapping that location to the nearest RBI office is
  left to the caller.
- **RTGS-only holidays (`◆`) are treated as bank working days.** RTGS is closed on those days but
  banks are open, so NEFT and IMPS settlement still runs. A future `rtgs` holiday kind would let
  RTGS-sensitive flows treat them differently.
- **Banks' closing of accounts (1 April) counts as a non-working day by default**
  (`IMDA_CLOSING_OF_ACCOUNTS_IS_HOLIDAY`).
- **FX as-of** returns the last published rate within 10 days. This is a convenience view, not
  legal advice on which rate applies to a given invoice (GST and customs rules name their own
  sources).

## 5. Engineering limits of this demo

- **Single node, SQLite, sync code.** That is fine for this data volume (about 50k rows in total),
  but not for multi-tenant scale.
- **Webhook SSRF guard:** the target's DNS is resolved once per delivery. Every address must be
  public, and the connection is pinned to the validated IP (`Host` and TLS SNI keep the original
  name), so DNS rebinding cannot redirect a delivery. Residual risk: an HTTP proxy configured
  around the dispatcher would bypass the pin. Egress firewall rules are the defence in depth.
- **Webhooks are at-least-once.** Each delivery is claimed in the database before it is sent. If
  a dispatcher crashes after sending but before recording the result, the claim expires after
  10 minutes and the event is sent again. Receivers should de-duplicate on `X-IMDA-Event-Id`.
- **Refresh and dispatch locks are per process.** Run one worker process. Several API or worker
  processes could refresh at the same time, which would break the 1-request-per-2-s upstream
  limit. A multi-node setup needs a shared lock (for example, a database lease row).
- **The admin token is a single shared bearer token.** Read endpoints are open and meant for
  localhost. Production would add per-client keys, rate limits on our own API, and TLS
  termination.
- **The MCP HTTP token is also one shared bearer token.** `imda mcp --transport http` has no
  per-client identity, no per-client rate limit and no rotation beyond restarting with a new
  `IMDA_MCP_TOKEN`. It speaks plain HTTP, so a remote deployment needs TLS terminated by a
  reverse proxy in front of it. A non-loopback `--host` is refused unless each public name is
  listed with `--allowed-host HOST[:PORT]` (repeatable), which turns on Host and Origin checks
  against DNS rebinding. Failed logins are logged at WARNING (method, path, client address; never
  the header). At most 8 tool calls run at once; the rest wait.
- **The MCP server opens the database with `mode=ro`, but SQLite still writes next to it.** In
  WAL mode a reader creates and updates the `-wal` and `-shm` files beside the database file. So
  the directory must be writable by the server user even though the database itself is never
  modified. A read-only mount for the whole directory can make calls fail with
  `STORE_UNAVAILABLE`.
- **Prompt injection in scraped names relies on the model.** Holiday and office names (at most 300
  characters each) come from scraped pages. The server removes control, zero-width, bidi,
  tag, private-use and filler characters (after NFKC normalisation) and caps the length, which
  stops hidden or fake structure. It cannot stop a plain-language sentence such as "ignore your
  instructions" inside a name. Defence there depends on the model treating tool text as data, as
  the server instructions and tool descriptions tell it to. Zero-width joiners are removed too,
  so a few Indic conjunct forms in names lose their joiner.
- **Webhook secrets are stored in plain text** in SQLite, because they are needed for signing.
  Production would use a KMS-backed secret store.

## The long-term fix

Scraping is a bridge, not a destination. The durable fix is an official, machine-readable feed for
each dataset:

| Dataset | Durable source |
|---|---|
| Bank holidays | RBI publishes the holiday calendar as ICS/JSON, ideally with an RTGS/NEFT flag |
| FX reference rates | A licensed FBIL data feed (FBIL offers direct and vendor distribution) |
| Payment statistics (UPI) | DBIE API access, or NPCI's official data products |

Every source sits behind the `SourceAdapter` interface (`fetch → parse → fingerprint`). Switching
a dataset to an official feed means replacing one adapter file. The REST API, the MCP tools, the
store, the webhooks and the tests stay the same.

The forward-deployed-engineer step is to raise this with the data owners, while this adapter keeps
merchants unblocked in the meantime.
