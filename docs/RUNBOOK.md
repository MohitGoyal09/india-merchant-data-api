# Runbook

How to run India Merchant Data API (IMDA) every day. Commands run from the repository root.
For design, see [ARCHITECTURE.md](ARCHITECTURE.md). For known limits, see [LIMITATIONS.md](LIMITATIONS.md).
Security tasks and test evidence are in [SECURITY.md](SECURITY.md).

## 1. Facts to know first

| Item | Value |
|---|---|
| Rule | Ingest fills one SQLite file. The API and the MCP server only read it. |
| REST API | `127.0.0.1:8000` (`IMDA_API_PORT` in Docker) |
| MCP over HTTP | `127.0.0.1:8100`, path `/mcp` |
| Database | `data/imda.sqlite3` (`/data/imda.sqlite3` in Docker, volume `imda-data`) |
| Settings | `IMDA_*` variables or `.env`. Names are in `src/imda/config.py`. |
| Processes | One `imda worker` only. Two workers can break the 1-request-per-2-s limit. |

## 2. Install and start

### Local

| Step | Command |
|---|---|
| 1. Install | `make install` |
| 2. Make settings | `cp .env.example .env` |
| 3. Edit `.env` | Put your contact in `IMDA_USER_AGENT`. Set `IMDA_ADMIN_TOKEN` (32+ characters). |
| 4. Load data | `uv run imda backfill --from 2024-01-01` (47 requests, about 92 s) |
| 5. Start API | `make serve` |
| 6. Check | `curl -s http://127.0.0.1:8000/healthz` |

Make a token: `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
For all history use `uv run imda backfill --from 2000-01-01`. It needs about 380 requests. At one request every 2 s, that takes at least 760 s.

### Docker

| Step | Command |
|---|---|
| 1. Build | `make docker-build` |
| 2. Start | `docker compose up -d api worker` |
| 3. Load data | `docker compose --profile tools run --rm backfill` |
| 4. Port 8000 busy | `IMDA_API_PORT=8080 docker compose up -d api worker` |
| 5. Start MCP | `IMDA_MCP_TOKEN=<32+ characters> docker compose --profile mcp up -d mcp` |
| 6. Check | `docker compose ps` and `curl -s http://127.0.0.1:8000/healthz` |
| 7. Logs | `docker compose logs -f worker` |

`api`, `worker` and `mcp` share volume `imda-data`. Containers do not restart by themselves.
After a host reboot, run step 2 again.

## 3. Daily refresh

| Option | Command | Note |
|---|---|---|
| Worker (default) | `uv run imda worker` | Refresh every 360 min. Webhook dispatch every 30 s. Stops on SIGTERM. |
| Cron | `0 7 * * * cd /ABS/PATH && uv run imda refresh` | A refresh needs 6 to 8 requests. |
| Cron with exit code | `uv run imda worker --once` | Runs one refresh and one dispatch. Exit code 1 if a job failed. |

The worker prints one JSON line per cycle (`"event": "worker.cycle"`). Check it with `docker compose logs --tail 20 worker`.

The worker does not run the canary. Schedule it once a day:

```bash
0 6 * * * cd /ABS/PATH && uv run imda canary    # exit code 1 when a source is broken
```

In Docker use `docker compose run --rm api imda canary`. The canary sends at most 6 requests.

Check state at any time: `uv run imda status`. It shows each source, its status, the last good time and the last error.

## 4. Register the MCP connector

Full host configs are in [MCP.md](MCP.md). Short version:

| Host | How |
|---|---|
| Claude Code, Desktop | Add an `mcpServers` entry with `uv run --directory /ABS/PATH/india-merchant-data-api imda mcp` (stdio, no token). |
| Claude Agent SDK | Same entry in `mcp_servers`. Add `allowed_tools=["mcp__imda__*"]`. |
| Remote HTTP | Start the server (below). Give the host the URL and `Authorization: Bearer <IMDA_MCP_TOKEN>`. |

Start a remote server for one agent profile:

```bash
IMDA_MCP_TOKEN=<32+ characters> uv run imda mcp --transport http --host 127.0.0.1 --port 8101 \
  --toolsets calendar,settlement --allowed-host imda.example.com
```

`imda.example.com` is a placeholder. Use the name the TLS proxy shows to clients. The server speaks plain HTTP. Put TLS in a proxy in front of it. Run one server per merchant and per toolset list, each with its own token and port.

Verify: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8101/mcp` must print `401` (no token).

## 5. Rotate tokens

| Step | `IMDA_ADMIN_TOKEN` | `IMDA_MCP_TOKEN` |
|---|---|---|
| 1. Make a new value | `python -c "import secrets;print(secrets.token_urlsafe(32))"` | Same command. |
| 2. Store it | Edit `.env` or your secret store. | Edit `.env`, or the shell that starts the server. |
| 3. Restart | `docker compose up -d --force-recreate api` (local: stop and start `make serve`) | `docker compose --profile mcp up -d --force-recreate mcp` (local: restart `imda mcp`) |
| 4. Update clients | Scripts that call `/v1/webhooks` or `/v1/admin/*`. | The `Authorization` header in each agent host. |
| 5. Check the old value | `curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $OLD" http://127.0.0.1:8000/v1/webhooks` must print `401`. | Same check on `/mcp`. It must print `401`. |
| 6. Record the date | Write the next rotation date in the hand-off sheet. | Same. |

There is no grace period. The old token stops working at the restart. A token shorter than 32 characters makes the process exit at start.
Webhook signing secrets are separate. To rotate one, delete the subscription (`DELETE /v1/webhooks/{id}`) and create it again.

## 6. Back up and restore

Back up (works while the API runs):

```bash
mkdir -p backups
sqlite3 data/imda.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE);"
cp data/imda.sqlite3 backups/imda-$(date +%F).sqlite3
sqlite3 backups/imda-$(date +%F).sqlite3 "PRAGMA integrity_check;"    # must print: ok
```

Option: `sqlite3 data/imda.sqlite3 ".backup backups/imda-$(date +%F).sqlite3"` makes a consistent copy without the checkpoint.

Docker (not run in this review):

```bash
docker compose exec api python -c "import sqlite3; s=sqlite3.connect('/data/imda.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close()"
docker compose cp api:/data/backup.sqlite3 backups/imda-$(date +%F).sqlite3
```

Restore:

| Step | Command |
|---|---|
| 1. Stop writers and readers | `docker compose --profile mcp stop` (local: stop the processes) |
| 2. Move the old files away | `mv data/imda.sqlite3 data/imda.sqlite3.bad; rm -f data/imda.sqlite3-wal data/imda.sqlite3-shm` |
| 3. Copy the backup in | `cp backups/imda-YYYY-MM-DD.sqlite3 data/imda.sqlite3` |
| 4. Start | `docker compose up -d api worker` |
| 5. Check | `uv run imda status` |
| 6. Catch up | `uv run imda refresh` (it re-reads the last 7 days) |

No backup? Run the backfill again. The repo ships the script, not the data.
Webhook secrets are in the database in plain text. Keep backups private.

## 7. Symptom, cause, check, action

Set `H=http://127.0.0.1:8000`. Query the database with `sqlite3 data/imda.sqlite3`.

| Symptom | Likely cause | Check | Action |
|---|---|---|---|
| `meta.degraded: true` | A source this answer used has status `degraded` or `broken`. The API serves the last good data. | `uv run imda status`; `curl -s $H/v1/sources/health \| jq '.data.sources[] \| select(.status!="ok")'` | Find the row in this table (broken, circuit, budget, RBI block). Fix the cause. Run `uv run imda refresh`. Status returns to `ok` and a `source.recovered` event fires. |
| `stale: true` on FBIL | FBIL published late. Staleness is computed on each request. It is not stored. | `curl -s $H/v1/sources/health \| jq '.data.sources[] \| select(.freshness.stale==true) \| {source,dataset,freshness}'` | `source=auto` uses RBI for dates FBIL lacks. Pass the warning on. Run `uv run imda refresh` once. If RBI is also stale, check `fetch_log` (below). |
| `source_health = broken` (parse error, drift) | RBI or FBIL changed the page or JSON. Ingest stored nothing from that payload. | `uv run imda canary`; `sqlite3 data/imda.sqlite3 "SELECT fetched_at,source,dataset,status_code,error FROM fetch_log ORDER BY fetched_at DESC LIMIT 10;"`; the drift report in `/v1/sources/health` (`added_keys`, `removed_keys`, `changed_keys`) | 1. Re-record the failing fixture: `uv run python scripts/record_rbi_fixtures.py holidays_page` (names are in `record_all`) or `uv run python scripts/record_fbil_fixtures.py`. 2. Update the parser in `src/imda/sources/`. 3. Run `uv run python scripts/update_baselines.py`. 4. Run `make check`. 5. Run `uv run imda refresh`. |
| `CALENDAR_DATA_MISSING` (REST 409, MCP tool error) | No holiday data for that office and year. | `sqlite3 data/imda.sqlite3 "SELECT year,COUNT(*) FROM holiday_years GROUP BY year;"` (a full year has 34 rows) | Load the year: `uv run imda backfill --datasets holidays --from YYYY-01-01`. For a year not yet started, see section 9. Never tell a merchant "no holidays". |
| `STORE_UNAVAILABLE` (REST 503, MCP tool error) | The database file is missing, not migrated, or its directory is read-only. | `ls -l $IMDA_DB_PATH`; `docker compose exec api ls -l /data` | Run the backfill, or restore (section 6). The MCP server opens the file with `mode=ro`, but SQLite writes `-wal` and `-shm` files next to it. The directory must be writable by the server user. |
| `circuit open` in a refresh error | 5 failures in a row on one host. The breaker stays open for 600 s. | `uv run imda status` (LAST ERROR column); `fetch_log` query above | Wait 10 minutes. Run `uv run imda refresh`. If it opens again, check the host in a browser. |
| `request budget exhausted` | The run used `IMDA_REQUEST_BUDGET` requests (default 500). | `uv run imda status`; the run output shows `requests` | Run `uv run imda refresh` again. A full history backfill needs about 380 requests. Raise `IMDA_REQUEST_BUDGET` only on purpose. |
| RBI blocks the IP (many 403, 429, timeouts from `rbi.org.in`) | RBI may block any IP (see [LIMITATIONS.md](LIMITATIONS.md) section 2). | `fetch_log` rows with `source = 'rbi'` and an `error` value | 1. Stop the worker: `docker compose stop worker`. 2. Set `IMDA_UPSTREAM_ENABLED=false` in `.env` and restart `api`. 3. The API and MCP keep serving the last good data. 4. Do not change IP or headers to get around the block. Contact RBI. Plan the move to an official feed. |
| Webhook deliveries fail | The receiver is down, slow or returns non-2xx, or the URL is unsafe. | `curl -s -H "Authorization: Bearer $IMDA_ADMIN_TOKEN" $H/v1/webhooks/<id>/deliveries \| jq '.data[] \| {attempt,status_code,error}'` | See the error classes below. Run `uv run imda webhooks dispatch` to retry now. |
| `429 REFRESH_COOLDOWN` | A refresh was accepted less than `IMDA_REFRESH_COOLDOWN_SECONDS` ago (300 s). | The `Retry-After` header | Wait. Or run `uv run imda refresh` on the host. `409 REFRESH_IN_PROGRESS` means one is running. |
| MCP `401` | The token is missing or wrong. | Server log line `MCP auth rejected: 401 ...` (method, path, client address; never the header) | Send `Authorization: Bearer <IMDA_MCP_TOKEN>`. Check for a stale token after a rotation. |
| MCP `421` or `403` | The `Host` or `Origin` header is not in `--allowed-host`. | `curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $IMDA_MCP_TOKEN" -H "Host: bad.example" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{}" http://127.0.0.1:8101/mcp` (prints 421; the token check runs first) | Add the public name: `--allowed-host HOST[:PORT]` (repeat for each name). Restart. |
| `imda mcp` exits with code 2 | No `IMDA_MCP_TOKEN`, or a non-loopback `--host` without `--allowed-host`. | The error line on stderr | Set the token. Add `--allowed-host`. |
| `503 ADMIN_DISABLED` | `IMDA_ADMIN_TOKEN` is not set. | `echo ${IMDA_ADMIN_TOKEN:+set}` | Set a token of 32+ characters. Restart `api`. |

Webhook error classes (the delivery log shows only these, never the URL or body):

| Class | Meaning | Action |
|---|---|---|
| `timeout` | No answer within `IMDA_WEBHOOK_TIMEOUT_SECONDS` (10 s). | Fix or speed up the receiver. |
| `connect_error` | The TCP or TLS connection failed. | Check DNS, firewall, certificate. |
| `http_status` | The receiver returned a non-2xx code. See `status_code`. | Fix the receiver. Redirects are never followed. |
| `unsafe_url` | The URL is not `https`, or it resolves to a private, loopback or link-local address. Never retried. | Fix the URL. Delete the subscription and create it again. |
| `in_flight` | A pass holds the claim. A claim older than 10 minutes counts as a failed try. | Wait. Do not run two workers. |

A failed delivery is tried 3 times, with waits of 30 s, 2 min and 10 min. Receivers must drop duplicates by `X-IMDA-Event-Id`.

## 8. Incident: the agent gave a wrong settlement date

| Step | Action |
|---|---|
| 1. Collect | The question, the agent answer, the transcript of tool calls, the merchant's actual settlement date, the office, `captured_at` (with offset), the cycle and the mode. |
| 2. Reproduce with REST | `curl -s -G $H/v1/settlement/eta --data-urlencode "captured_at=2026-03-27T11:00:00+05:30" --data-urlencode "office=mumbai" --data-urlencode "cycle_days=2" --data-urlencode "mode=working_days" \| jq` |
| 3. Reproduce with MCP | Call `estimate_settlement_date` with the same arguments. The two answers must match. If not, stop and report a bug. |
| 4. Read `skipped` | Each skipped day has a reason (Sunday, 2nd or 4th Saturday, a holiday name). Compare with the merchant's bank calendar. |
| 5. Check holidays | `curl -s "$H/v1/holidays?year=2026&office=mumbai&month=3" \| jq`. Compare by hand with the RBI holiday page. |
| 6. Check provenance | Read `provenance[].fetched_at` and `meta.warnings` in the REST answer. Read `/v1/sources/health` and the `fetch_log` query in section 7. |
| 7. Name the cause | See the table below. |
| 8. Fix | Fix the data, the settings, or the prompt. Run `uv run imda refresh` if data changed. |
| 9. Add a test | Add an eval case to `evals/agent_cases.json` (with `ground_truth`). Check it with `make agent-evals ARGS=--dry-run`. Add a unit case to `tests/unit/test_settlement.py` or `tests/api/test_settlement.py`. |
| 10. Tell the merchant | Say what was wrong and the correct date. The ETA is an estimate ([LIMITATIONS.md](LIMITATIONS.md) section 4). |

| Cause | Sign | Fix |
|---|---|---|
| Wrong office | The bank city maps to another RBI office. | Correct the office in the merchant profile. `curl -s $H/v1/offices \| jq -r '.data[].slug'` |
| Wrong cycle or mode | The merchant is not on T+2, or Razorpay rolls by calendar days. | Set `cycle_days` and `mode` (`working_days` or `calendar_then_roll`). Or set `IMDA_SETTLEMENT_CYCLE_DAYS`. |
| Cut-off time | The payment was captured late in the day. The engine uses the capture date in IST. | Record the cut-off. The engine does not model it. |
| Holiday data wrong or old | A holiday is missing, or the row is old. | Compare with RBI. Run `uv run imda backfill --datasets holidays --from YYYY-01-01 --force`. |
| RTGS-only or 1 April rule | `◆` days count as working days. 1 April is a holiday by default. | Check `IMDA_CLOSING_OF_ACCOUNTS_IS_HOLIDAY`. |
| Agent misquote | REST and MCP agree, but the answer differs. | Fix the prompt. Add the eval case. |

## 9. Add next year's holidays

RBI usually publishes the next year in December ([LIMITATIONS.md](LIMITATIONS.md) section 3).

1. Know how the code sees it. `refresh` and `backfill` read the `drYear` dropdown on RBI's page on every run. The newest option is the limit for loading. The canary does not flag a new year: the year list is left out of the fingerprint on purpose (`src/imda/sources/rbi/holidays.py`).
2. In December, `imda refresh` loads this month and next month. When the new year is in the dropdown, January of that year loads too. Month loads do not mark the year as loaded.
3. `imda backfill` caps `--to` at today. So a full load of a year that has not started fails: `--from` after today is rejected. Load the full year on or after 1 January:

   ```bash
   uv run imda backfill --datasets holidays --from 2027-01-01
   ```

   The worker's refresh also loads the current year when it is missing.
4. Verify:

   ```bash
   sqlite3 data/imda.sqlite3 "SELECT year,COUNT(*) FROM holiday_years GROUP BY year;"   # 34 rows for the new year
   curl -s "$H/v1/holidays?year=2027&office=mumbai" | jq '.meta.count'
   uv run imda canary
   ```

5. Known gap: until the full year loads, a settlement ETA that reaches the new year returns `CALENDAR_DATA_MISSING`. Tell the agent owner before late December.
6. The error `details.hint` shows `imda backfill --datasets holidays --from YYYY-01-01`. Before 1 January that command fails as in step 3.
