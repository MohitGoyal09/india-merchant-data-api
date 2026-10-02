# FDE playbook: roll out to one strategic merchant

How a forward-deployed engineer (FDE) puts the IMDA MCP connector in front of one merchant's agents.
Every number marked **target** is a goal, not a result. No pilot has run. Each target says how to measure it.
Operations steps are in [RUNBOOK.md](RUNBOOK.md). Controls and tests are in [SECURITY.md](SECURITY.md).

## Phases

| Phase | Output | Section |
|---|---|---|
| 1. Discovery | Answers to the questions, merchant profile | a |
| 2. Baseline | Numbers before go-live | b |
| 3. Integration | Private connector, one token per agent | c |
| 4. Shadow pilot | ETA match rate, human edit rate | d |
| 5. Go-live | All criteria met | e |
| 6. Measure and hand off | Metrics, feedback, owner | f, g, h |

## a. Discovery questions

| # | Ask the merchant | Why | Where the answer goes |
|---|---|---|---|
| 1 | What share of support tickets ask "when will I be paid?" What share ask "what INR did I get?" | Sets the size of the problem. | Baseline table (b) |
| 2 | What share of invoices are in a foreign currency? Which currencies? | Only USD, GBP, EUR, JPY, AED and IDR are covered. | Scope check |
| 3 | In which city is the settlement bank account? | Holidays are per RBI regional office (34), not per bank branch. | `office` in the merchant profile |
| 4 | What is the settlement cycle (T+N)? Is it fixed or by payment method? | The default is T+2 working days (`IMDA_SETTLEMENT_CYCLE_DAYS`). | `cycle_days` |
| 5 | What are the cut-off times? Does a late capture move to the next day? | The engine uses the capture date in IST. It does not model a cut-off. | Known gap list |
| 6 | Do any payments settle on Saturdays or holidays? | Tests the `working_days` rule against real dates. | Pilot data (d) |
| 7 | Which agent surfaces will use it: support bot, finance ops, both? | Decides toolsets and tokens. | Table in (c) |
| 8 | Who reads the answers: customers, or staff only? | Customer-facing needs a stricter go-live bar. | Go-live (e) |
| 9 | Who owns the agent, and who is on call? | Needed for the hand-off. | Checklist (h) |

Map the bank city to an office:

```bash
curl -s http://127.0.0.1:8000/v1/offices | jq -r '.data[].slug'
```

If the city has no RBI office, pick the nearest one and record the choice with the merchant.

## b. Baseline metrics (capture before go-live)

Take two weeks of data from the merchant's help desk. Do not set success targets until you have the baseline.

| Metric | How to measure | Target |
|---|---|---|
| Tickets per week: "when will I be paid?" | Export tickets. Tag intent by keyword, then check 50 by hand. Count by week. | None. This is the baseline. |
| Tickets per week: "what INR did I get?" | Same. | None. This is the baseline. |
| Median handling time for these tickets | Help desk "first reply" and "resolved" times. Take the median. | None. This is the baseline. |
| Share answered correctly today | A finance reviewer checks a random sample of 50 closed tickets against the bank statement. Count correct answers. | None. This is the baseline. |
| Share of foreign-currency invoices | Payment export. Count by currency. | None. This is the baseline. |

## c. Integration plan

| Step | Action |
|---|---|
| 1 | Run one connector for this merchant. Data is public, so the connector controls who has access. See [MCP.md](MCP.md) section 9. |
| 2 | Load data and schedule the worker and the canary ([RUNBOOK.md](RUNBOOK.md) sections 2 and 3). |
| 3 | Make one bearer token for each agent: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Store it in the merchant's secret store. |
| 4 | Start one remote MCP server for each agent profile (below). The server speaks plain HTTP. Put TLS in a proxy. |
| 5 | In the agent host, register the URL and the header `Authorization: Bearer <token>`. Allow only `mcp__imda__*` tools and turn off built-in tools. |
| 6 | Check that a call without a token returns `401`. |

| Agent | Toolsets | Tools it gets | Example |
|---|---|---|---|
| Support agent | `calendar,settlement` | `fetch_all_offices`, `fetch_holidays`, `check_business_day`, `fetch_next_business_days`, `estimate_settlement_date`, `quote_invoice` | `IMDA_MCP_TOKEN=<support token> uv run imda mcp --transport http --port 8101 --toolsets calendar,settlement --allowed-host imda-support.example.com` |
| Finance agent | `fx,settlement` | `fetch_fx_rate`, `fetch_all_fx_rates`, `convert_currency`, `fetch_fx_stats`, `compare_fx_sources`, `estimate_settlement_date`, `quote_invoice` | `IMDA_MCP_TOKEN=<finance token> uv run imda mcp --transport http --port 8102 --toolsets fx,settlement --allowed-host imda-finance.example.com` |

`example.com` names are placeholders. Add `health` to a list if the agent must state data freshness. One process has one token, so each agent profile needs its own process.

## d. Shadow-mode pilot

In shadow mode the agent drafts an answer. A human approves or edits it. The customer sees only the human answer.

| Step | Action |
|---|---|
| 1 | Run for N weeks. **Target:** N = 4, and at least 100 settled payments. Adjust with the merchant. |
| 2 | For each ticket, store the agent draft, the tool calls and the human edit. Tag each edit with a reason. |
| 3 | Export settled payments from the merchant's Razorpay settlement report: `captured_at` (with UTC offset) and the real settlement date. Map the column names by hand. |
| 4 | Compute the ETA match rate (below) once a week. |
| 5 | For each mismatch, find the cause with [RUNBOOK.md](RUNBOOK.md) section 8. Fix data or settings. |
| 6 | Poll `GET /v1/sources/health` every hour and store `freshness.stale` for each source (for the freshness SLO in (e)). |

Compute the match rate. `pairs.csv` has rows `captured_at,settled_date`, for example `2026-09-14T11:00:00+05:30,2026-09-16`:

```bash
while IFS=, read -r captured settled; do
  eta=$(curl -s -G http://127.0.0.1:8000/v1/settlement/eta \
    --data-urlencode "captured_at=$captured" --data-urlencode "office=mumbai" \
    | jq -r '.data.eta_date')
  echo "$captured,$settled,$eta"
done < pairs.csv | awk -F, '{n++; if ($2==$3) m++; else print "MISMATCH " $0}
  END {printf "%d/%d exact (%.1f%%)\n", m, n, 100*m/n}'
```

Add `--data-urlencode "cycle_days=N"` and `--data-urlencode "mode=calendar_then_roll"` if the merchant needs them.

| Pilot metric | Formula | Target |
|---|---|---|
| ETA exact-match rate | payments where our ETA equals the real date, divided by all payments | At least 90%. Set the final bar with the merchant after week 1. |
| ETA within 1 working day | payments where the difference is 1 working day or less, divided by all | At least 98% |
| Draft approved without edit | approved drafts divided by all drafts | At least 80% |
| Wrong-fact drafts | drafts with a wrong date or amount, counted by reviewer | 0 sent to customers |

These targets are starting points. If the match rate is low, the cause is often the cycle, the office or the cut-off, not the data.

## e. Go-live criteria

All rows must hold. Record the date and the evidence.

| Criterion | Threshold | How to check |
|---|---|---|
| Agent eval on the merchant's own questions | At least 92% of cases pass (the default `--bar 0.92` in [evals/README.md](../evals/README.md); 15 of 16 for the current suite) | Write the merchant's cases (include `safety` and `error_recovery` categories) in a new file with the same format as `evals/agent_cases.json`. Run `uv run python evals/run_agent_evals.py --cases <file> --dry-run`, then run it without `--dry-run`. |
| Drift canary | Green for 7 days in a row | `uv run imda canary` once a day. Exit code 0 each day. |
| Freshness SLO | **Target:** `rbi/fx_reference_rates` has `stale: false` in at least 95% of the hourly polls over 7 days | Polls from pilot step 6. FBIL lags often; the SLO uses RBI because `source=auto` fails over to it. |
| ETA match rate | At or above the bar agreed in (d) | The script above. |
| Runbook tested | One drill done: restore a backup, rotate a token, read a `degraded` answer | [RUNBOOK.md](RUNBOOK.md) sections 5, 6 and 7. |
| Security | Security tests pass. `pip-audit` clean. Bandit findings reviewed. | [SECURITY.md](SECURITY.md), "Run the security checks". |
| Handoff sheet | Complete | Section h. |

Known gap: `evals/run_agent_evals.py` always seeds its test database from the recorded fixtures (`scripts/seed_fixtures.py`). It has no option to use the live database. Merchant questions must therefore rely on facts in the fixtures (Mumbai 2026 holidays, September 2026 FX, the office list), or you must add fixtures. Closing this gap is a platform task (see g).

## f. Success metrics after launch

Measure monthly. Compare with the baseline from (b). All values are **targets** to agree with the merchant.

| Metric | How to measure | Target |
|---|---|---|
| Tickets per week for the two intents | Same export and tags as the baseline | Down against baseline. Set the number after baseline. |
| Median handling time | Help desk times | Down against baseline. Set the number after baseline. |
| Share answered correctly | Same 50-ticket sample | At or above baseline, with 0 known wrong settlement dates sent |
| ETA match rate | Weekly script on new payments | At or above the pilot bar |
| Share of answers with a data warning | Count answers that contain a `WARNING:` line | Report only. Watch for a rise. |
| Open incidents | [RUNBOOK.md](RUNBOOK.md) section 8 | Each has an eval case added |

## g. Feedback to the platform team

| Item | Detail |
|---|---|
| Parts that generalise | The `SourceAdapter` interface (`fetch`, `parse`, `fingerprint`) in `src/imda/sources/base.py`. The drift canary (`imda canary`, `src/imda/health/baselines.json`). The provenance envelope (`provenance`, `warnings`, `meta.degraded`) on every answer. The toolset flag for per-agent scope. |
| Parts that are specific | The RBI and FBIL parsers. The settlement rules. |
| Gaps to close | One shared token for each server. No per-client rate limit. No live-database option in the agent eval runner. Next-year holidays cannot load before 1 January ([RUNBOOK.md](RUNBOOK.md) section 9). Cut-off times are not modelled. |
| Long-term fix | Official feeds: an RBI holiday calendar in ICS or JSON, and a licensed FBIL data feed. Only the adapter file changes. See [LIMITATIONS.md](LIMITATIONS.md). |
| Ask | Open a request with RBI and FBIL for official feeds. Ask Razorpay for the settlement-cycle and cut-off rules per merchant. |

## h. Hand-off checklist

| Item | Value | Done |
|---|---|---|
| Owner (name, team) | | [ ] |
| On-call (name, channel) | | [ ] |
| Merchant profile: office, `cycle_days`, `mode` | | [ ] |
| One token for each agent, stored in the merchant's secret store | | [ ] |
| Next token rotation date | | [ ] |
| Database backup job and last restore test date | | [ ] |
| Worker and daily canary scheduled | | [ ] |
| Dashboards or logs: `/v1/sources/health`, worker JSON lines, `imda.api.access` log, `MCP auth rejected` lines | | [ ] |
| Webhook receivers drop duplicates by `X-IMDA-Event-Id` | | [ ] |
| Eval file for this merchant, with the last run date | | [ ] |
| Known limits shared with the merchant: [LIMITATIONS.md](LIMITATIONS.md) and [MCP.md](MCP.md) section 6 | | [ ] |
| FBIL data licence status for production use | | [ ] |
