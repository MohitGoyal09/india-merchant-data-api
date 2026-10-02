# Phase 2 plan: MCP layer for Agent Studio

Status: **Proposed** · Date: 2026-10-02 · Depends on: Phase 1 (REST API), done.

## 1. Goal

Let an AI agent, such as a Razorpay Agent Studio agent, answer merchant questions with the same data
and rules as the REST API. Example questions:

- "A customer paid USD 1,200 on 24 Dec 2025. What is that in INR, and when will it settle in Mumbai?"
- "Is 2 October a bank holiday in Chennai?"

The MCP server reuses the domain services in-process (`FxService`, `HolidayCalendar`,
`estimate_settlement`, `quote_invoice`). It does not call our own REST API over HTTP. That means
one source of truth, no extra network hop, and the same tests apply.

## 2. Design decisions

| # | Decision | Why |
|---|---|---|
| M1 | Official MCP Python SDK (`mcp` 2.x, FastMCP). Pin the version, and check the API against the installed package. | Standard, maintained, and what the agent hosts expect. |
| M2 | **Read-only by design.** No tool writes data, triggers ingest or manages webhooks. Every tool sets `readOnlyHint=true`, `idempotentHint=true`, `destructiveHint=false`, `openWorldHint=false`. | The agent cannot change state or hit RBI/FBIL. Admin stays on the REST API with a token. |
| M3 | Tool names follow Razorpay's MCP style: snake_case `fetch_*`, `fetch_all_*` and verb forms. Tools are grouped into **toolsets** (`calendar`, `fx`, `settlement`, `rates`, `health`), with a `--toolsets` flag. | It matches `razorpay/razorpay-mcp-server`, and a host can expose only what an agent needs. |
| M4 | Each tool returns **structured output** (a typed pydantic result, which gives an `outputSchema`) and a one-line text summary. Every result carries `provenance` and `warnings` (stale or degraded). | Agents can reason over fields. Humans can read the summary. Data trust travels with the answer. |
| M5 | **Errors are tool results** (`isError=true`) with a JSON body: `{code, message, hint}`. The codes are the same as the REST API (`CALENDAR_DATA_MISSING`, `RATE_NOT_FOUND`, `VALIDATION_ERROR`, ...). The server never raises to the host. | This matches Razorpay's `NewToolResultError` pattern. The model can read the hint and recover, for example by picking another date. |
| M6 | Transports: **stdio** (`imda mcp`) for Claude Code and Desktop, and **streamable HTTP** (`imda mcp --transport http`, path `/mcp`). HTTP needs `Authorization: Bearer <IMDA_MCP_TOKEN>` (at least 32 characters, timing-safe compare) and binds to 127.0.0.1 by default. | The same shape as Razorpay's remote MCP (a bearer merchant token over streamable HTTP). It gives a remote-ready path for Agent Studio. |
| M7 | The DB is opened **read-only** for each call, with the cached calendar snapshot (the same TTL as the API). | The same concurrency guarantees as the REST API. |
| M8 | Tool descriptions are written for the model: when to use the tool, what each parameter means (with examples), units (JPY per 100), and what the tool **cannot** answer. | Tool selection quality depends on descriptions more than on code. |

## 3. Tool surface (13 read-only tools)

| Toolset | Tool | Purpose |
|---|---|---|
| calendar | `fetch_all_offices` | The 34 RBI regional offices: slug, city, state |
| calendar | `fetch_holidays` | Bank holidays for an office and year (optional month) |
| calendar | `check_business_day` | Is a date a bank working day for an office? If not, why. |
| calendar | `fetch_next_business_days` | The next N working days after a date |
| settlement | `estimate_settlement_date` | The T+N settlement ETA, with the skipped days and their reasons |
| settlement | `quote_invoice` | A foreign-currency amount → INR at the reference rate, plus the settlement ETA |
| fx | `fetch_fx_rate` | The reference rate in force on a date, and why it may come from an earlier date |
| fx | `fetch_all_fx_rates` | A rate series for a range, paged with a cursor (max 1,000 rows) |
| fx | `convert_currency` | Convert between INR and USD/GBP/EUR/JPY/AED/IDR, including cross rates |
| fx | `fetch_fx_stats` | Weekly or monthly average, min, max and volatility |
| fx | `compare_fx_sources` | RBI against FBIL differences, in basis points |
| rates | `fetch_mibor` | FBIL overnight MIBOR (handles the Friday `3D` tenor) |
| health | `fetch_source_health` | Source status, freshness and drift, so the agent can say how fresh the data is |

Also:
- **Resources:** `imda://offices` and `imda://sources/health`, for hosts that read context.
- **Prompt:** `settlement_answer`. It is a template that tells the agent to state the rate date, the
  holiday reasons and the fact that the ETA is an estimate.

## 4. Agent demo

`scripts/agent_demo.py` is a small agent built on the **Anthropic Python SDK (Messages API)**. It
connects to our MCP server over stdio with the MCP client SDK, lists its tools, and runs a
tool-use loop:

- Model `claude-opus-5-5`, with adaptive thinking and explicit `effort: "medium"`.
- Server-side refusal `fallbacks: "default"`.
- No forced `tool_choice`, because that returns a 400 on this model.

It prints each tool call and the final answer, and saves a transcript to `docs/demo/*.md`.

Why not the Claude Agent SDK: Agent Studio is built on the Claude Agent SDK, but that SDK is the
full Claude Code harness, with file and shell tools. For a merchant-data agent we want **only** our
MCP tools, and full visibility of each tool call for scoring evals. A Messages API loop gives both.
`docs/MCP.md` shows how to register the same server in the Agent SDK, Claude Code and Claude
Desktop (`mcpServers` config), so the connector is host-agnostic.

## 5. Evals (the bar is set before building)

| Layer | What | How | Bar |
|---|---|---|---|
| 1. Tool contract (no LLM, runs in CI) | About 30 cases: every tool's happy path, errors (`isError` plus code), annotations (all read-only), output schema validity, provenance present, toolset filtering | In-memory MCP client session against a DB seeded from fixtures | **100%** |
| 2. Agent (Claude, opt-in) | 16 merchant questions in 9 categories (fx, settlement, calendar, invoice, rates, comparison, error_recovery, out_of_scope, safety) with known answers on the fixture DB | Score is deterministic, with no LLM judge. Each question must (a) call the required tool(s) and (b) state the expected facts (dates, INR amounts, holiday names) in the final answer, matched by regex or number with a tolerance. | **≥ 15/16 (92%)**, plus reported tool-selection accuracy, average tool calls, tokens and cost |

The agent eval results are saved to `evals/results/<timestamp>.json`, and a summary table is
printed.

**Cost:** the plan first guessed about USD 1.5 per run. Measured on the 16-case file (run 4,
2026-10-02): 16,838 input tokens, 8,010 output tokens and 261,792 cache-read tokens, so **about
USD 0.28 per run** on Claude Opus 5.5. Layer 2 only runs when you ask for it and have set up
credentials.

**Result:** run 4 passed 16/16 with tool selection 100%. Run 3 scored 14/16 (below the bar) because
two correct answers failed regex checks that were too narrow. The checks were widened and
regression tests were added. History: [evals/README.md](../evals/README.md#live-run-history).

## 6. Deliverables

- `src/imda/mcp/` contains: `server.py` (FastMCP app), `tools/*.py` (one file per toolset),
  `schemas.py` (result models), `errors.py`, `auth.py` (HTTP bearer), `context.py` (read-only store
  and cached calendar).
- CLI: `imda mcp [--transport stdio|http] [--host] [--port] [--toolsets ...]`
- `docs/MCP.md` has the tool reference, a **"What the agent can and cannot do"** section, host
  configs (Claude Code `.mcp.json`, Claude Desktop, Agent SDK `mcp_servers`, remote HTTP), and the
  error codes.
- `docs/mcp_tool_spec.json` is generated from the live server's `list_tools` (names, descriptions,
  input and output schemas, annotations).
- `scripts/agent_demo.py`, `evals/agent_cases.json`, `evals/run_agent_evals.py`, and tests for
  everything above. The demo and agent-eval loop are unit-tested with a fake Anthropic client, so
  CI needs no API key.

## 7. Milestones

| M | Scope | Gate |
|---|---|---|
| P1 | MCP server, 13 tools, schemas, errors, toolsets, stdio and HTTP with bearer auth, CLI, layer-1 contract evals | Contract evals 100%. `make check` green. MCP Inspector-style smoke test (list tools, call 3) over stdio and HTTP. |
| P2 | Agent demo, agent eval harness with fake-client tests, `docs/MCP.md`, `mcp_tool_spec.json` | Tests green. Demo runs end-to-end against the fake client. |
| P3 (needs your credentials) | A live agent demo run and a layer-2 eval run on Claude Opus 5.5 | ≥ 15/16, with the transcript saved to `docs/demo/`. Met: run 4 scored 16/16. |
| P4 | Review: MCP security (auth, tool output size limits, no prompt injection carried through holiday names), docs refresh | No HIGH findings |

P1 and P2 run in parallel (different files). P3 waits for your credentials.
