# Security

Threats, the control for each one, and the test that checks the control.
Operations steps are in [RUNBOOK.md](RUNBOOK.md). Known limits are in [LIMITATIONS.md](LIMITATIONS.md) section 5.

## Scope

| Item | Value |
|---|---|
| What runs | A local REST API, a read-only MCP server, a worker. One SQLite file. |
| Trust | RBI and FBIL pages are untrusted input. Merchants and agents are not trusted by default. |
| Not in scope | Hosted multi-tenant use. TLS termination. Per-client keys. See "Residual risks". |

## Threat, control, test

Test paths are relative to the repository root. Names after `::` are test functions.

| # | Threat | Control | Verifying tests |
|---|---|---|---|
| 1 | **SSRF through a webhook URL.** An admin (or a stolen admin token) points a webhook at an internal host. | The URL must be `https`, with no user info. The host name is resolved once. Every address must be public. The connection is pinned to the checked IP (original `Host` and TLS SNI kept). The check runs again at delivery. Redirects are not followed. `IMDA_ALLOW_PRIVATE_WEBHOOKS` is `false` by default. | `tests/unit/test_ssrf.py::test_rejects_bad_urls`, `::test_rejects_non_public_resolution`, `::test_mixed_public_and_private_resolution_rejected`; `tests/api/test_webhooks_api.py::test_unsafe_urls_are_rejected_422`; `tests/unit/test_webhooks.py::test_unsafe_at_delivery_time_is_skipped_recorded_and_not_retried`, `::test_redirect_is_a_failure_and_never_followed` |
| 2 | **DNS rebinding** of a webhook host between check and send. | One resolution for each delivery. The request goes to the validated IP, so a later DNS change has no effect. | `tests/unit/test_ssrf.py::test_dns_rebinding_second_check_catches_flip`, `::test_pin_uses_first_ip_original_host_header_and_sni`; `tests/unit/test_webhooks.py::test_one_resolution_per_delivery_so_rebinding_cannot_redirect`, `::test_request_goes_to_the_validated_ip_with_original_host_and_sni` |
| 3 | **Webhook forgery and replay.** An attacker posts a fake event to a receiver, or resends an old one. | HMAC-SHA256 over `"<timestamp>." + raw body`. Headers `X-IMDA-Signature`, `X-IMDA-Timestamp`, `X-IMDA-Event-Id`. The receiver rejects a timestamp more than 300 s from its clock and drops duplicate event ids. The timestamp is set at each send. The secret is shown once. | `tests/unit/test_signing.py::test_replay_of_old_delivery_rejected_even_with_valid_signature`, `::test_timestamp_tolerance_boundaries`, `::test_verify_uses_constant_time_compare`; `tests/unit/test_webhooks.py::test_timestamp_is_taken_at_each_post_not_at_pass_start`; `tests/api/test_webhooks_api.py::test_secret_is_shown_once_and_absent_from_list` |
| 4 | **Token guessing.** Someone guesses `IMDA_ADMIN_TOKEN` or `IMDA_MCP_TOKEN`. | Minimum 32 characters, checked at start. Surrounding white space is removed before the check. A short token stops the process. Admin routes return 503 when no token is set. Generate with `secrets.token_urlsafe(32)`. | `tests/unit/test_config_cli.py::test_settings_reject_short_admin_token`, `::test_padding_does_not_make_a_short_token_long_enough`; `tests/api/test_admin.py::test_unset_token_disables_admin_endpoints_with_503`, `::test_wrong_credentials_are_401`; `tests/mcp/test_http.py::test_middleware_refuses_a_short_token`; `tests/mcp/test_cli.py::test_http_refuses_to_start_without_a_token` |
| 5 | **Timing attack** on the token compare. | The token is hashed with SHA-256 and compared with `hmac.compare_digest`. | `tests/api/test_admin.py::test_digest_comparison_is_used`; `tests/mcp/test_http.py::test_token_matching_rules` |
| 6 | **Token leakage** in logs or responses. | Tokens are never logged or echoed. A failed MCP login is logged at WARNING with method, path and client address only. Webhook logs hold no secret, URL or body. | `tests/api/test_admin.py::test_token_never_reaches_logs_or_responses`; `tests/mcp/test_http.py::test_each_401_is_logged_without_the_header`; `tests/unit/test_webhooks.py::test_logs_contain_no_secret_url_or_body` |
| 7 | **Prompt injection** through scraped holiday or office names. | The server applies NFKC and removes control, zero-width, bidi, tag, private-use and filler characters. It caps each string at 300 characters. Server instructions and tool descriptions say tool text is data. The `settlement_answer` question sits in a marked block, capped at 1,000 characters. All tools are read-only, so an injected text cannot write data. | `tests/mcp/test_sanitize.py` (whole file); `tests/mcp/test_guards.py::test_control_characters_in_holiday_names_are_stripped`, `::test_instructions_and_descriptions_never_tell_the_model_to_obey_data`; `tests/mcp/test_limits.py::test_question_sits_in_a_delimited_block_marked_as_data`, `::test_question_is_capped`, `::test_question_cannot_close_the_block_early` |
| 8 | **Wrong Host or DNS rebinding** against the MCP HTTP port. | Binds to `127.0.0.1`. A non-loopback `--host` is refused without `--allowed-host`. Other `Host` values get 421 or 403. | `tests/mcp/test_cli.py::test_non_loopback_host_without_allowed_host_exits_2`, `::test_started_app_rejects_a_wrong_host_and_accepts_the_right_one` |
| 9 | **Resource exhaustion (DoS).** | Date range at most 3,660 days (`RANGE_TOO_LARGE`). Page size at most `IMDA_MAX_PAGE_SIZE` (1,000). Request body at most 64 KiB (413). Upstream response at most 25 MiB. MCP result at most 48 KB, and at most 8 tool calls at once. One refresh at a time, 300 s apart. | `tests/api/test_fx.py::test_rates_limit_above_the_configured_maximum_is_rejected`; `tests/api/test_hardening.py::test_oversized_content_length_is_413_before_the_body_is_read`, `::test_oversized_chunked_body_is_413`; `tests/unit/test_http_client.py::test_response_over_cap_is_rejected_without_retry`; `tests/mcp/test_limits.py::test_at_most_n_calls_run_at_once_and_none_are_rejected`; `tests/mcp/test_guards.py::test_no_tool_result_exceeds_about_fifty_kb`; `tests/api/test_admin.py::test_refresh_within_the_cooldown_is_429_with_retry_after`; `tests/mcp/test_errors.py::test_stats_period_cap_is_range_too_large` |
| 10 | **SQL injection.** | All values are bound with `?`. The only built SQL text is in `Store.holidays` (`src/imda/store/repo.py`). It joins fixed clause strings. Values stay bound. Office slugs must be one of the 34 known slugs. | `tests/api/test_sql_injection.py` sends classic payloads (`' OR 1=1 --`, `'; DROP TABLE fx_rates; --`, `mumbai' UNION SELECT ...`) in `office`, `year`, `month`, `currency`, `from`, `to`, `date`, `source` and `cursor` of `/v1/holidays`, `/v1/fx/rates` and `/v1/calendar/business-day`. Each gets 4xx only; every table keeps its row count; a valid holidays request returns the same data. Also `tests/api/test_calendar.py` (unknown office gives `OFFICE_NOT_FOUND`). Static check: bandit B608 (see below). |
| 11 | **CSV or ICS injection.** Hostile names give formulas or extra calendar lines. | CSV has only date, currency, rate, unit, source and time columns. It has no free text. ICS text is escaped. Control characters and Unicode line breaks are removed. Lines are folded at 75 octets. | `tests/api/test_fx.py::test_csv_via_format_param`; `tests/api/test_ics.py::test_escape_text_strips_control_and_unicode_line_breaks`, `::test_rendered_calendar_has_no_injected_lines_from_hostile_names` |
| 12 | **Upstream abuse.** We overload or anger RBI or FBIL, or get around their protection. | Honest `User-Agent` with a contact. One request every 2 s for each host. Backoff with jitter. Budget of 500 requests for each run. Circuit breaker (5 failures, 600 s). Kill switch `IMDA_UPSTREAM_ENABLED=false`. No redirects. No bot-protection, captcha or login bypass. Sources that need a bypass were rejected ([REVERSE_ENGINEERING.md](REVERSE_ENGINEERING.md)). | `tests/unit/test_http_client.py::test_sends_honest_user_agent_and_params_without_following_redirects`, `::test_waits_min_interval_between_requests_to_same_host`, `::test_budget_is_spent_per_attempt_and_then_exhausted`, `::test_circuit_opens_then_half_opens_then_closes`, `::test_kill_switch_raises_without_network` |
| 13 | **Information leak in errors.** | Errors have a fixed shape. No stack trace. Unknown exceptions become `INTERNAL_ERROR`, and the trace stays in the server log. MCP errors show field names, not values. Responses carry `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. | `tests/api/test_hardening.py::test_plain_value_error_is_a_generic_500_with_a_log`, `::test_every_response_carries_the_security_headers`; `tests/mcp/test_errors.py::test_unexpected_exception_is_a_generic_internal_error` |
| 14 | **Secrets in the repo.** | `.env`, `data/`, `*.sqlite3` and `*.db` are in `.gitignore`. `.dockerignore` keeps `.env` out of the image. `.env.example` has empty token values. `scripts/scan_secrets.py` scans every git-tracked file for Anthropic, AWS, GitHub and Slack keys, private key headers and quoted `api_key`, `secret`, `token` or `password` values. It prints `file:line: rule` and never the value. Deliberately fake test values are listed in `.secrets-allowlist`. | `make security` and the CI `security` job run the scan and fail on a finding. `tests/unit/test_scan_secrets.py` (each rule, the allowlist, no value printed, and a scan of this repository). |
| 15 | **Container weakness.** | The image runs as user `imda` (uid 10001), not root. Compose publishes ports on `127.0.0.1` only. Every service has `restart: "no"`. | No automated test. Check by reading `Dockerfile` and by `docker compose --profile mcp config \| grep -E "host_ip\|restart"`. |

## Run the security checks

| Check | Command |
|---|---|
| Security tests (the files above) | `uv run pytest -q --no-cov tests/unit/test_ssrf.py tests/unit/test_signing.py tests/unit/test_webhooks.py tests/unit/test_http_client.py tests/unit/test_config_cli.py tests/api/test_admin.py tests/api/test_webhooks_api.py tests/api/test_hardening.py tests/api/test_ics.py tests/mcp/test_http.py tests/mcp/test_guards.py tests/mcp/test_limits.py tests/mcp/test_sanitize.py tests/mcp/test_cli.py tests/mcp/test_errors.py tests/api/test_fx.py tests/api/test_mibor.py` |
| All offline tests | `make check` |
| Static analysis | `uv run bandit -r src -q` |
| Dependency audit | `uv run pip-audit` |
| Secret scan | `uv run python scripts/scan_secrets.py` (part of `make security`) |

`bandit` and `pip-audit` are dev dependencies in `pyproject.toml`.

## Results (run on 2026-10-02)

Security tests: 514 passed, 0 failed (the 17 files in the table above).

**pip-audit** (version 2.10.1): "No known vulnerabilities found". The project package itself is skipped, because it is not on PyPI.

**bandit** (version 1.9.4, src scanned): High 0, Medium 2, Low 10. Exit code 1. These are open findings. No `# nosec` hides them. Each one is judged below.

| ID | Severity | Location | Assessment |
|---|---|---|---|
| B608 | Medium | `src/imda/store/repo.py:406` | False positive. The f-string adds `where`. `where` is made only from the fixed strings `office_slug = ?` and `date BETWEEN ? AND ?`. Values go in as bound arguments. The line has `# noqa: S608` for ruff, but bandit ignores `noqa`. |
| B608 | Medium | `src/imda/observability.py:55` | False positive. The table name comes from fixed literals in the same module, never from a request (the code comment says so). The file is new and not yet committed at the time of this scan. Prefer a fixed query for each table, or add `# nosec B608` with the same reason. |
| B101 x7 | Low | `src/imda/api/errors.py:119,126,137,142,153,164,176` | `assert isinstance(exc, X)` to narrow the type in an exception handler. Each handler is registered for that class only. With `python -O` the check disappears and nothing else changes. |
| B101 | Low | `src/imda/http/client.py:151` | `assert last is not None` after a retry loop that runs at least once. Type narrowing only. |
| B101 | Low | `src/imda/ingest/loaders.py:162` | `assert raw is not None` after a loop over 12 months. Type narrowing only. |
| B311 | Low | `src/imda/http/client.py:105` | `random.Random()` makes backoff jitter. It is not a security use. |

`make security` runs `bandit -r src -q -ll` and `pip-audit`. The `-ll` flag fails on Medium findings, so it exits 1 until the two B608 lines are fixed or suppressed with a reason. This is a CI decision for the owner.

**Secret scan** (2026-10-02): `scripts/scan_secrets.py` found two module-level `TOKEN` constants, in `tests/api/test_admin.py` and `tests/api/test_webhooks_api.py`. Both are admin tokens passed to an in-process test app on a temporary database. They are listed in `.secrets-allowlist`. `git log --all` shows no `.env`, `.sqlite3` or `.db` file ever added.

## Residual risks

Full text: [LIMITATIONS.md](LIMITATIONS.md) section 5.

| Risk | Detail | Mitigation |
|---|---|---|
| Plain-language prompt injection | A scraped name such as "ignore your instructions" passes the sanitizer. | Read-only tools. Server text tells the model to treat tool output as data. Human review in shadow mode ([FDE_PLAYBOOK.md](FDE_PLAYBOOK.md)). |
| One shared bearer token | No per-client identity. No guess limit on failed logins (they are only logged). No rotation except restart. | 32+ random characters. One server and one token for each merchant. Rotate by [RUNBOOK.md](RUNBOOK.md) section 5. Add a rate limit at the proxy. |
| Open read endpoints | REST reads need no token. No per-client rate limit or concurrency cap. | Bind to `127.0.0.1`. Put an authenticating proxy in front for any shared host. |
| Plain HTTP | The MCP server and API speak HTTP. | Terminate TLS in a reverse proxy. |
| Proxy bypasses the IP pin | An HTTP proxy placed around the dispatcher would defeat the pin. | Egress firewall rules. |
| Webhook secrets in plain text | Stored in SQLite, because signing needs them. | Keep the database file and backups private. Use a secret store in production. |
| At-least-once webhooks | A crash can send an event twice. | Receivers drop duplicates by `X-IMDA-Event-Id`. |
| Per-process locks | Two workers could break the upstream rate limit. | Run one worker. |
| Legal | RBI may block IPs. FBIL data needs a licence for production. | See [LIMITATIONS.md](LIMITATIONS.md) section 2. |

## How to report and rotate

| Task | Action |
|---|---|
| Report a vulnerability | Do not open a public issue. Send the details to the repository owner (Mohit Goyal) by private message. Include steps to reproduce and the version (`uv run imda version`). |
| A token may have leaked | Rotate it now with [RUNBOOK.md](RUNBOOK.md) section 5. Check that the old token returns 401. Read the logs for `MCP auth rejected` lines. |
| A webhook secret may have leaked | `DELETE /v1/webhooks/{id}`, then create the subscription again. Give the new secret to the receiver. |
| A secret was committed | Rotate it first. Then remove it from history. Do not rely on deleting the file. |
| Upstream asks us to stop | Set `IMDA_UPSTREAM_ENABLED=false`. Stop the worker. Reads continue from stored data. |
