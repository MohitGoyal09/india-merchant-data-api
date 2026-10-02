# MCP connector

## 1. What it is

This is a read-only [MCP](https://modelcontextprotocol.io) server. It gives an AI agent 13 tools
over the same data and rules as the REST API: RBI bank holidays, FBIL and RBI FX reference rates,
MIBOR, settlement dates and source health. It is built for a Razorpay Agent Studio agent. It uses
the same in-process code as the REST API, so both give the same answers.

Merchant questions it answers:

- "A customer paid USD 1,250.50 on 30 Sep 2026. What is that in INR, and when will it settle in Mumbai?"
- "Is 2 October a bank holiday in Chennai?"
- "What was the USD/INR reference rate on Republic Day?"
- "How fresh is your data?"

The design is in [PLAN_MCP.md](PLAN_MCP.md). Limits of the data are in [LIMITATIONS.md](LIMITATIONS.md).

## 2. Quickstart

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
make install                                   # install dependencies
uv run python scripts/seed_fixtures.py --db data/imda.sqlite3   # offline demo data (Sep 2026)
# or load real data (live, polite, about 92 s): uv run imda backfill --from 2024-01-01
uv run imda mcp                                # stdio transport
IMDA_MCP_TOKEN=<32+ characters> uv run imda mcp --transport http --port 8100
```

The HTTP transport is streamable HTTP at `http://127.0.0.1:8100/mcp`. Each request needs
`Authorization: Bearer <IMDA_MCP_TOKEN>`. With no token, the command exits with code 2.

Options of `imda mcp`:

| Option | Default | Meaning |
|---|---|---|
| `--transport stdio\|http` | `stdio` | stdio for Claude Code and Desktop. http for remote hosts. |
| `--host`, `--port` | `127.0.0.1`, `8100` | Bind address (http only). |
| `--allowed-host` | none | `HOST[:PORT]` allowed in the `Host` header (http only, repeatable). Required when `--host` is not a loopback address. |
| `--toolsets` | all | Comma list of `calendar,settlement,fx,rates,health`. Only those tools are registered. |

The server reads `IMDA_DB_PATH` (default `data/imda.sqlite3`). It never calls RBI or FBIL.
Run `imda refresh` (or the Docker `worker`) to keep the data fresh.

### Host configuration

Replace `/ABS/PATH` with the path of this repository. `--directory` makes `uv` run from the
repository, so the default database path works.

**Claude Code** (`.mcp.json` in your project):

```json
{
  "mcpServers": {
    "imda": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/india-merchant-data-api", "imda", "mcp"]
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`): use the same `mcpServers` block. Restart the app.

**Claude Agent SDK** (Python). The dict shape and the `allowed_tools` wildcard come from the
[Agent SDK MCP guide](https://code.claude.com/docs/en/agent-sdk/mcp).

```python
options = ClaudeAgentOptions(
    mcp_servers={
        "imda": {
            "command": "uv",
            "args": ["run", "--directory", "/ABS/PATH/india-merchant-data-api", "imda", "mcp"],
        }
    },
    allowed_tools=["mcp__imda__*"],  # or explicit: "mcp__imda__quote_invoice", ...
)
```

Tool names have the form `mcp__<server>__<tool>`. `allowed_tools` auto-approves those tools. The
guide does not say that it hides other tools. To keep an agent to these tools only, also
disable the built-in tools in your SDK options (see the Agent SDK permissions guide).
To expose fewer tools, add `"--toolsets", "calendar,settlement"` to `args`.

**Remote HTTP** (for example, a hosted connector):

```python
mcp_servers={
    "imda": {
        "type": "http",
        "url": "https://imda.example.com/mcp",
        "headers": {"Authorization": f"Bearer {os.environ['IMDA_MCP_TOKEN']}"},
    }
}
```

The same shape works in `.mcp.json` with `"type": "http"`. Put TLS in front of the server. The
server itself speaks plain HTTP.

## 3. Tool reference

All tools are read-only. Dates are `YYYY-MM-DD` (2000-01-01 to 2100-12-31). `office` is an RBI
office slug, such as `mumbai` or `new-delhi`. Currencies: USD, GBP, EUR, JPY, AED, IDR, INR.
The machine-readable spec is [mcp_tool_spec.json](mcp_tool_spec.json). Examples below were run
on a database backfilled on 2026-10-02. The line shown is the first line of the result text.

| Toolset | Tool | What it answers | Key params | Example call and result |
|---|---|---|---|---|
| calendar | `fetch_all_offices` | Which RBI offices exist, and their slugs? | none | `{}` gives "34 RBI regional offices. Office slugs: agartala, ahmedabad, ..." |
| calendar | `fetch_holidays` | Which bank holidays has an office in a year or month? | `office`, `year`, `month?` | `chennai, 2026, 10` gives "3 RBI bank holidays for chennai in 2026-10." |
| calendar | `check_business_day` | Is a date a bank working day? If not, why? | `date`, `office` | `2026-10-02, chennai` gives "2026-10-02 (Friday) is NOT a bank working day for chennai: Mahatma Gandhi Jayanti." |
| calendar | `fetch_next_business_days` | What are the next N working days? | `date`, `office`, `count?` (5) | `2026-10-01, mumbai, 3` gives "... 2026-10-03, 2026-10-05, 2026-10-06." |
| settlement | `estimate_settlement_date` | When will a payment settle (T+N)? Which days were skipped? | `captured_at` (with offset), `office`, `cycle_days?`, `mode?` | `2026-03-27T11:00:00+05:30, mumbai` gives "estimated settlement 2026-04-02 (Thursday); skipped 4 non-working days ..." |
| settlement | `quote_invoice` | INR value of a foreign amount, plus settlement ETA | `amount`, `currency`, `invoice_date`, `office`, `captured_at?` | `1250.50 USD, 2026-09-30, mumbai` gives "1250.50 USD = 120026.99 INR at the rbi reference rate of 2026-09-30. ... estimated settlement 2026-10-03 (Saturday); skipped 1 ... 2026-10-02 Mahatma Gandhi Jayanti." |
| fx | `fetch_fx_rate` | Which rate was in force on a date, and why is it from an earlier day? | `currency`, `date`, `source?` (auto) | `USD, 2026-01-26` gives "1 USD = 91.6195 INR (fbil reference rate of 2026-01-23). No rate was published on 2026-01-26 (Republic Day) ..." |
| fx | `fetch_all_fx_rates` | A rate series for a range, paged | `currency`, `from_date`, `to_date`, `source?`, `limit?` (100, max 1000), `cursor?` | `USD, 2026-09-01 to 09-30, limit 5` gives "5 USD reference rates ... More rows follow: pass next_cursor as `cursor`." |
| fx | `convert_currency` | Convert between INR and another currency, or cross rates | `amount`, `from_currency`, `to_currency`, `date`, `source?` | `1000 JPY to INR, 2026-09-30` gives "1000 JPY = 611.60 INR (exact 611.6000) using JPY rbi rate of 2026-09-30." |
| fx | `fetch_fx_stats` | Weekly or monthly mean, min, max, change, volatility | `currency`, `from_date`, `to_date`, `period?` (`week` or `month`) | `USD, 2026-07-01 to 09-30, month` gives "3 month periods ... Latest 2026-09-01 to 2026-09-30: mean 95.4641238095, min 94.4467, max 96.0321, change 1.173715%." |
| fx | `compare_fx_sources` | Do RBI and FBIL agree? Differences in basis points | `currency`, `from_date`, `to_date` | `USD, 2026-09-01 to 09-30` gives "USD: 17 day(s) where RBI and FBIL both published, 0 differ by more than 1 bp, largest difference 0.0000 bp." |
| rates | `fetch_mibor` | FBIL overnight MIBOR (`3D` tenor on Fridays) | `from_date`, `to_date` (max 366 days) | `2026-09-22 to 09-30` gives "3 overnight MIBOR rows ... Latest: 2026-09-24 O/N 5.13% p.a." |
| health | `fetch_source_health` | How fresh is each source? Is there drift? | none | `{}` gives "Overall data status: ok. All 5 source datasets are ok." and two stale warnings |

Also available:

| Kind | Name | Use |
|---|---|---|
| Resource | `imda://offices`, `imda://sources/health` | The same JSON as `fetch_all_offices` and `fetch_source_health`, for hosts that read context. |
| Prompt | `settlement_answer(question)` | A template: which tools to call, and what the final answer must state (rate date, skipped days, estimate caveat, warnings). |

## 4. Result format

A successful result has two parts.

1. **Text content.** A one-line summary, then one `WARNING: ...` line for each warning, then the
   result as compact JSON. Real example (`fetch_fx_rate`, 2026-01-26):

   ```text
   1 USD = 91.6195 INR (fbil reference rate of 2026-01-23). No rate was published on 2026-01-26 (Republic Day), so this is the latest earlier rate, 3 days before.
   WARNING: fbil/fx_reference_rates is stale: latest data is 2026-09-24, expected 2026-10-01

   {"provenance":[...],"warnings":[...],"currency":"USD","requested_date":"2026-01-26","effective_date":"2026-01-23","lag_days":3,"reason":"Republic Day","rate":{...}}
   ```

2. **`structuredContent`.** The same JSON as an object. It matches the tool's `outputSchema`.

Every result has two fields:

| Field | Content |
|---|---|
| `provenance` | A list of `{source, dataset, source_url, fetched_at, stale}`: where the data came from, and when it was stored. |
| `warnings` | Texts such as stale or degraded data. The agent must pass these on. |

Size limits keep results small for the model:

| Limit | Value |
|---|---|
| Text of any result | At most 48,000 bytes. If the JSON is larger, the text keeps the summary and a note. Read `structuredContent`. |
| `fetch_all_fx_rates` page | Cut at about 36 KB of rows, at most 1,000 rows. A warning says so. Continue with `next_cursor`. |
| `fetch_fx_stats` | At most 120 periods and 3,660 days. More gives `RANGE_TOO_LARGE`. |
| `compare_fx_sources` | More than 100 overlapping days: only flagged days are listed (`rows_scope` is `flagged_only`). The `summary` still covers all days. |
| `fetch_mibor` | At most 366 days. |
| Any text field | At most 300 characters. |

## 5. Errors

An error is a tool result with `isError=true`. The text is JSON: `{"code", "message", "hint"}`.
The server never raises a protocol error for a tool call.

| Code | When | Hint tells the agent to |
|---|---|---|
| `VALIDATION_ERROR` | A value is not allowed (for example a rule of the domain). | Correct the value and retry. |
| `INVALID_REQUEST` | The arguments do not match the tool schema (bad date format, wrong enum, date out of range), or the tool name is unknown. | Fix the named argument. |
| `OFFICE_NOT_FOUND` | The office slug is unknown. | Call `fetch_all_offices`. It suggests close names. |
| `RATE_NOT_FOUND` | No rate was published recently before that date. | Try an earlier date, or list rates with `fetch_all_fx_rates`. |
| `CALENDAR_DATA_MISSING` | No holiday data is loaded for that office and year. | Tell the user the year is unavailable. Never assume no holidays. |
| `RANGE_TOO_LARGE` | The date range is over the limit of the tool. | Use a shorter range, or split it. |
| `STORE_UNAVAILABLE` | The database is missing or not set up. | Tell the user the service is unavailable. Do not retry. |
| `INTERNAL_ERROR` | Any other failure. The cause is logged, not shown. | Retry once, then tell the user. |

Real example: `fetch_holidays` with `office=mumbai, year=2031`:

```json
{"code": "CALENDAR_DATA_MISSING", "message": "No holiday data loaded for office 'mumbai', year 2031", "hint": "Holiday data for 2031 is not loaded for mumbai. Tell the user that year is unavailable. Do not assume there are no holidays."}
```

## 6. What the agent can and cannot do

### What the agent can do

- Say whether a date is a bank working day for one of the 34 RBI offices, and name the reason.
- List bank holidays for an office, by year or month, for the years that are loaded (2001 to 2026).
- Estimate the settlement date of a payment (T+N working days). It names every skipped day and
  its reason (Sunday, 2nd or 4th Saturday, holiday).
- Give the RBI or FBIL reference rate for a date. If the date has no rate, it gives the latest
  earlier rate and says why (weekend, holiday).
- Convert amounts between INR and USD, GBP, EUR, JPY, AED and IDR, including cross rates, with the
  unit handled (JPY per 100, IDR per 10,000).
- Quote an invoice: INR value plus settlement ETA, in one call.
- Give weekly or monthly FX statistics, and compare RBI with FBIL in basis points.
- Give FBIL overnight MIBOR.
- Say how fresh each source is, and show stale or degraded data in `warnings`.

### What the agent cannot do

- **It cannot write.** No tool changes data, starts an ingest, or manages webhooks. Those stay on
  the REST API, behind an admin token.
- **It cannot reach live data.** It never calls RBI or FBIL. Answers come from the local dataset.
  Freshness is shown in `provenance` and `warnings`. Data is only as new as the last `imda refresh`.
- **It has no data for years that are not loaded.** It returns `CALENDAR_DATA_MISSING`. FX starts on
  2000-01-03 (AED and IDR on 2026-01-05). There are no FX rates from 2018-07-25 to 2022-04-11 from RBI,
  but FBIL covers that gap.
- **Holidays are per RBI office, not per bank branch.** The caller must map a bank to the nearest
  office. Holiday names are RBI's combined names, shared across regions (dates and kinds are per office).
- **The settlement date is an estimate.** It follows the published T+N rule. It is not Razorpay's
  settlement engine, and it does not know the merchant's cut-off time or bank-specific cycles.
- **No tax or bank data.** No GST or customs rules, no bank-specific cut-offs, and no UPI volumes
  (NPCI was excluded because it needs a bot-protection bypass).
- **Only six currencies** besides INR: USD, GBP, EUR, JPY, AED, IDR.
- **FX rates are reference rates, not tradeable quotes.** They are not the rate that a bank or card
  network charged. FBIL's benchmarks need an FBIL data licence for production use.

## 7. Security

| Control | Detail |
|---|---|
| Read-only tools | Every tool has `readOnlyHint=true`, `idempotentHint=true`, `destructiveHint=false`, `openWorldHint=false`. |
| Read-only database | The SQLite file is opened read-only for each call. |
| stdio | No token. The host starts the process, so it runs with the permissions of that user. |
| HTTP bearer token | `IMDA_MCP_TOKEN`, at least 32 characters. Compared in constant time (`hmac.compare_digest` on SHA-256 digests). A bad token gives `401`. The token is never logged or echoed. |
| HTTP bind | `127.0.0.1` by default. The SDK turns on DNS-rebinding protection for loopback hosts (`127.0.0.1`, `localhost`, `::1`). The server speaks plain HTTP: terminate TLS at a proxy. A non-loopback `--host` is refused unless you pass `--allowed-host HOST[:PORT]` (repeatable); only those `Host` values (and their `Origin`s) are served, others get `421` or `403`. |
| Token handling | One shared token, no per-client identity. Surrounding whitespace is trimmed, and the server refuses a token shorter than 32 characters. Each `401` is logged at WARNING with method, path and client address only. |
| Concurrency | At most 8 tool calls run at once. Extra calls wait; they do not fail. Structured results above 48 KB are returned with trailing list items removed and a warning. |
| Read-only mode and WAL | The database is opened with `mode=ro` and never modified, but SQLite creates `-wal` and `-shm` files next to it. The directory must be writable. |
| Untrusted text | Holiday names and error texts come from scraped pages. The server applies NFKC, removes control, format (zero-width, bidi, tag), private-use, unassigned and filler characters, and caps each string at 300 characters. This stops hidden structure, not a plain-language "ignore your instructions" sentence: that relies on the model treating tool text as data. The `settlement_answer` prompt caps the question at 1,000 characters and puts it in a `<merchant_question>` block marked as data. The server `instructions` and the tool descriptions tell the model to treat such text as data, never as instructions. |
| Audit log | One JSON line per tool call on the logger `imda.mcp.audit` (stderr). It holds the tool, outcome, timing, size and argument names. It never holds argument values. See [RUNBOOK.md](RUNBOOK.md#10-observability). |
| No secrets in output | Errors show field names and rules, not the rejected values. Unknown exceptions are logged and replaced by `INTERNAL_ERROR`. |

## 8. Testing

| Layer | What | Command | Bar |
|---|---|---|---|
| 1. Contract evals (no LLM, no network) | Every tool, errors, annotations, output schemas, provenance, toolsets, stdio and HTTP with bearer auth. Each result is also compared with the REST API. | `make mcp-evals` | 238 tests passed in the last run (2026-10-02). Must be 100%. |
| 2. Agent evals (Claude, opt-in) | Sixteen merchant questions in nine categories, scored by rules and not by a judge. Bar: 15 of 16 (at least 92%). | See [evals/README.md](../evals/README.md) | See [PLAN_MCP.md](PLAN_MCP.md) section 5 |
| 3. Spec freshness | `docs/mcp_tool_spec.json` matches the live server. | `uv run python scripts/export_tool_spec.py --check` | Exit code 0 |

After you change a tool, run `uv run python scripts/export_tool_spec.py` to refresh the spec.


**Latest live agent eval (2026-10-02, run 4, `claude-opus-5-5`): 16/16 passed in 9 categories, tool selection 100%, about USD 0.28.** The bar is 15/16. Run 3 scored 14/16 because two correct answers failed regex checks that were too narrow; the checks were widened and regression tests were added. See the run history in [`evals/README.md`](../evals/README.md#live-run-history). More: [`evals/README.md`](../evals/README.md) and the demo transcript [`demo/usd-invoice-settlement.md`](demo/usd-invoice-settlement.md).

## 9. How this maps to Agent Studio

- **One private connector for each merchant org.** Run one server for each merchant org and
  register it in Agent Studio as a private MCP connector. The data is public, so the connector
  controls who has access, not which data they see.
- **Remote HTTP with a token for each merchant.** The streamable HTTP transport at `/mcp` uses a
  bearer token, the same shape as Razorpay's remote MCP server. Give each merchant its own token.
  (This demo has one token for each server process. A shared server with many tokens needs a
  token store, which is not built.)
- **Toolsets scope an agent.** A support agent can get `--toolsets calendar,settlement`. A
  finance agent can get `fx,rates`.
- **Long term: official feeds.** Each source sits behind an adapter. When RBI or FBIL offer an
  official feed, only the adapter changes. The tool names, schemas and this document stay the
  same. See [LIMITATIONS.md](LIMITATIONS.md).
