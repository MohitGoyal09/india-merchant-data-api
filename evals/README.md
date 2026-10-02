# Agent evals (layer 2)

Twelve merchant questions, answered by Claude through our MCP server. The score is deterministic:
no LLM judge. This is layer 2 of the plan in `docs/PLAN_MCP.md` section 5. Layer 1 (tool contract
evals, no LLM) lives elsewhere and runs in CI.

## Run

```bash
make agent-evals ARGS=--dry-run     # no Claude call, no credentials: checks the case file
make agent-evals                    # real run: needs ANTHROPIC_API_KEY or `ant auth login`

uv run python evals/run_agent_evals.py --filter holiday --model claude-opus-5-5 --json out.json
uv run python evals/run_agent_evals.py --no-fallback --effort high
```

Exit codes: `0` bar met, `1` below the bar or an invalid case file, `2` no credentials.

A real run costs about USD 1.5 on Claude Opus 5.5 (the summary prints the exact estimate at
$4 / $20 per million input / output tokens, with cache tokens at their own rates). It writes
`evals/results/<UTC timestamp>.json`. Those files are git-ignored.

## How a run works

1. Seed a temp SQLite DB from the recorded fixtures (`scripts/seed_fixtures.py`).
2. Start ONE `uv run imda mcp` server on it, with `IMDA_MCP_FIXED_NOW` set to the case file's
   `fixed_now` (2026-09-30 15:00 IST) and `IMDA_UPSTREAM_ENABLED=false`.
3. For each case, run the agent loop (`src/imda/agent/loop.py`), then score the result.

A case **passes** when:

- every tool in `required_tools` was called, and no tool in `forbidden_tools` was; and
- every check in `expect` holds for the final answer.

The bar is **11 of 12**. The summary also reports tool-selection accuracy (cases whose tool
rules held), average tool calls per case, tokens, and estimated cost.

## Case file: `agent_cases.json`

```json
{
  "id": "jpy_to_inr_per_100_unit",
  "question": "How many rupees is JPY 1,000 worth on 24 September 2026, ...?",
  "required_tools": ["convert_currency"],
  "forbidden_tools": [],
  "expect": [
    {"type": "number", "value": 606.2, "tolerance": 0.05, "truth": "0:data.result"},
    {"type": "regex", "value": "60,?620", "negate": true}
  ],
  "notes": "why this case exists",
  "ground_truth": [{"request": {"method": "GET", "path": "/v1/fx/convert", "params": {}},
                    "checks": [{"path": "data.result", "op": "eq", "value": "606.20"}]}],
  "golden_answer": "a model answer that must pass its own checks"
}
```

| Check type | Passes when the answer |
|---|---|
| `contains` | contains `value` (case-insensitive) |
| `regex` | matches `value` anywhere (case-insensitive) |
| `number` | has any number within `tolerance` (default 0.01) of `value`; reads `1,15,091.88` |

Add `"negate": true` to require that a check does NOT hold (for example, no invented holidays).

`ground_truth`, `truth` and `golden_answer` are what make the expected values trustworthy:

- `ground_truth` runs the REST API (the domain layer) on the same fixture DB. Its checks must hold.
- `truth` (on a `number` check) says which ground-truth value the literal came from. The literal
  must be within `tolerance` of it.
- `golden_answer` is run through the scorer. It must pass its own checks.

`--dry-run` verifies all three, so a wrong expected value fails before any money is spent.

## The 12 cases

| Case | Tool | Expected |
|---|---|---|
| `usd_invoice_inr_and_settlement` | `quote_invoice` | INR 115,091.88; ETA 28 Sep 2026; says it is an estimate |
| `mumbai_holiday_mar31` | `check_business_day` | not a working day; Mahavir Jayanti |
| `t2_settlement_skipped_days` | `estimate_settlement_date` | 2 Apr 2026; skips 28, 29, 31 Mar and 1 Apr (names them) |
| `usd_rate_holiday_fallback` | `fetch_fx_rate` | 95.7245 from 11 Sep; Ganesh Chaturthi |
| `jpy_to_inr_per_100_unit` | `convert_currency` | INR 606.20 (not 60,620) |
| `next_3_business_days_mumbai` | `fetch_next_business_days` | 30 Mar, 2 Apr, 4 Apr 2026 |
| `rbi_vs_fbil_usd_sept` | `compare_fx_sources` | 0 bps difference |
| `usd_inr_september_stats` | `fetch_fx_stats` | mean 95.46, min 94.45, max 96.03 |
| `mibor_friday_3d_tenor` | `fetch_mibor` | 4.91 on 4 Sep 2026, tenor 3D |
| `mumbai_holidays_april_2026` | `fetch_holidays` | 1 Apr (closing of accounts), 3 Apr Good Friday, 14 Apr Ambedkar Jayanti |
| `holidays_2010_not_loaded` | `fetch_holidays` | says the data is not available; invents no holiday |
| `gst_rate_out_of_scope` | none | says it cannot answer; gives no rate |

## Notes

- Regex and phrase checks match plain-English answers. If a correct answer fails, read the saved
  `answer` in the results file first. Widen a regex only when the answer was right.
- The server clock is fixed, so FX staleness warnings (data ends 24 Sep) can appear in answers.
  That is expected and does not fail a case.
- With the refusal fallback on (default), a declined request re-runs on another model. The results
  file records `model_served` and `fallback_used`. Cost for a fallback call uses Opus 5.5 rates.

## Latest live run (2026-10-02, `claude-opus-5-5`, fixture DB)

| Metric | Result |
|---|---|
| Pass rate | **12/12 (100%)** in two consecutive runs, bar 11/12 |
| Tool selection | 100% |
| Average tool calls | 1.2 |
| Tokens | 14,289 input, 5,723 output, 8,053 cache write, 193,272 cache read |
| Cost | about USD 0.25 |

Full results: [run 1](../docs/demo/agent-eval-2026-10-02.json), [run 2](../docs/demo/agent-eval-2026-10-02-run2.json).
Live demo transcripts (real data): [USD invoice + Mumbai settlement](../docs/demo/usd-invoice-settlement.md), [Chennai holiday + JPY invoice](../docs/demo/chennai-holiday-and-jpy.md).
