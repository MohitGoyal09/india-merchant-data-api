#!/usr/bin/env bash
# Container smoke test: build the image, seed a fresh volume from the recorded fixtures, start
# `api` and `mcp` from docker-compose.yml and probe them over HTTP.
#
#   make smoke-compose
#   IMDA_API_PORT=18001 bash scripts/smoke_compose.sh
#
# It runs under its own compose project (imda-smoke-<pid>) with its own volume and image tag, so
# it never touches a running `docker compose up` stack or the `imda:dev` tag. The worker is not
# started (it would call RBI and FBIL). Everything is removed on exit, also after a failure.
# Needs: docker compose v2, curl, uv (to seed the DB on the host). The mcp service always
# publishes 127.0.0.1:8100 (see docker-compose.yml); the script fails early if that port is busy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT="imda-smoke-$$"
IMAGE="imda-smoke:$$"
API_PORT="${IMDA_API_PORT:-18000}"
MCP_PORT=8100
WAIT_SECONDS=90
API_URL="http://127.0.0.1:${API_PORT}"
MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/imda-smoke.XXXXXX")"
OVERRIDE="${WORK_DIR}/override.yml"
SEED_DIR="${WORK_DIR}/seed"
FAILURES=0

export IMDA_API_PORT="$API_PORT"
if command -v openssl >/dev/null 2>&1; then
  IMDA_MCP_TOKEN="$(openssl rand -hex 24)"
else
  IMDA_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
fi
export IMDA_MCP_TOKEN

dc() {
  docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" --profile mcp "$@"
}

pass() { printf 'PASS  %s\n' "$1"; }
skip() { printf 'SKIP  %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}
# fatal NAME: report a failed step that later steps depend on, then stop (the trap cleans up).
fatal() {
  printf 'FAIL  %s\n' "$1"
  exit 1
}

cleanup() {
  local status=$?
  set +e
  if [ -f "$OVERRIDE" ]; then
    if [ "$status" -ne 0 ]; then
      echo "--- container logs (last 40 lines) ---"
      dc logs --no-color --tail 40 api mcp 2>&1 || true
    fi
    dc down -v --remove-orphans >/dev/null 2>&1
  fi
  docker rmi "$IMAGE" >/dev/null 2>&1
  rm -rf "$WORK_DIR"
  if [ "$status" -eq 0 ] && [ "$FAILURES" -ne 0 ]; then
    status=1
  fi
  if [ "$status" -eq 0 ]; then
    echo "smoke-compose: all steps passed"
  else
    echo "smoke-compose: FAILED"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

port_busy() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

http_status() { # http_status [curl args...] -> prints the status code, 000 on connection error
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true
}

# ---------------------------------------------------------------- preflight
docker info >/dev/null 2>&1 || { echo "FAIL  docker is not running or not installed"; exit 2; }
for tool in curl uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL  $tool is required"; exit 2; }
done
for port in "$API_PORT" "$MCP_PORT"; do
  if port_busy "$port"; then
    echo "FAIL  port $port on 127.0.0.1 is already in use."
    if [ "$port" = "$MCP_PORT" ]; then
      echo "      docker-compose.yml publishes the mcp service on 127.0.0.1:$MCP_PORT; stop what uses it."
    else
      echo "      Set IMDA_API_PORT to a free port and run again."
    fi
    exit 2
  fi
done
pass "preflight: docker is up, ports $API_PORT and $MCP_PORT are free"

# One image tag per run, so the user's imda:dev tag is left alone. The short health interval
# keeps the wait quick; the probe command still comes from the Dockerfile.
cat >"$OVERRIDE" <<YAML
services:
  api:
    image: ${IMAGE}
    healthcheck:
      interval: 3s
      start_period: 5s
  mcp:
    image: ${IMAGE}
YAML

# ---------------------------------------------------------------- build + seed
if dc build api >"${WORK_DIR}/build.log" 2>&1; then
  pass "build image"
else
  tail -n 30 "${WORK_DIR}/build.log"
  fatal "build image"
fi

mkdir -p "$SEED_DIR"
if uv run --quiet python scripts/seed_fixtures.py --db "${SEED_DIR}/imda.sqlite3" >/dev/null; then
  # The container user (uid 10001) must be able to read the bind mount.
  chmod 755 "$WORK_DIR" "$SEED_DIR"
  chmod 644 "${SEED_DIR}"/imda.sqlite3*
  pass "seed fixture DB on the host"
else
  fatal "seed fixture DB on the host"
fi

if dc run --rm --no-deps --entrypoint "" -v "${SEED_DIR}:/src:ro" api \
  sh -c 'cp /src/imda.sqlite3 /data/' >"${WORK_DIR}/copy.log" 2>&1; then
  pass "copy seeded DB into the compose volume"
else
  cat "${WORK_DIR}/copy.log"
  fatal "copy seeded DB into the compose volume"
fi

# ---------------------------------------------------------------- start
if dc up -d --no-deps api mcp >"${WORK_DIR}/up.log" 2>&1; then
  pass "start api and mcp"
else
  cat "${WORK_DIR}/up.log"
  fatal "start api and mcp"
fi

api_container="$(dc ps -q api)"
health="starting"
for _ in $(seq 1 "$WAIT_SECONDS"); do
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$api_container" 2>/dev/null || echo gone)"
  [ "$health" = "healthy" ] && break
  [ "$health" = "unhealthy" ] || [ "$health" = "gone" ] && break
  sleep 1
done
if [ "$health" = "healthy" ]; then
  pass "api container health is healthy"
else
  fatal "api container health is '$health' after ${WAIT_SECONDS}s"
fi

# ---------------------------------------------------------------- probes
code="$(http_status "${API_URL}/healthz")"
if [ "$code" = "200" ]; then pass "GET /healthz is 200"; else fail "GET /healthz is $code (want 200)"; fi

code="$(http_status "${API_URL}/readyz")"
case "$code" in
  200) pass "GET /readyz is 200" ;;
  404) skip "GET /readyz is 404: this build has no readiness route" ;;
  *) fail "GET /readyz is $code (want 200)" ;;
esac

body_file="${WORK_DIR}/eta.json"
code="$(curl -s -o "$body_file" -w '%{http_code}' --max-time 10 -G "${API_URL}/v1/settlement/eta" \
  --data-urlencode 'captured_at=2026-03-27T11:00:00+05:30' --data-urlencode 'office=mumbai' || true)"
if [ "$code" = "200" ] && grep -Eq '"eta_date": ?"2026-04-02"' "$body_file"; then
  pass "GET /v1/settlement/eta returns 2026-04-02 from the seeded DB"
else
  fail "GET /v1/settlement/eta is $code or lacks eta_date 2026-04-02"
fi

# Wait for the MCP server to answer (401 without a token).
code="000"
for _ in $(seq 1 30); do
  code="$(http_status -X POST "$MCP_URL" -H 'Content-Type: application/json' -d '{}')"
  [ "$code" = "401" ] && break
  sleep 1
done
if [ "$code" = "401" ]; then
  pass "POST /mcp without a token is 401"
else
  fail "POST /mcp without a token is $code (want 401)"
fi

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-compose","version":"0"}}}'
init_file="${WORK_DIR}/init.out"
code="$(curl -s -o "$init_file" -w '%{http_code}' --max-time 10 -X POST "$MCP_URL" \
  -H "Authorization: Bearer ${IMDA_MCP_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "$INIT" || true)"
if [ "$code" = "200" ] && grep -q 'india-merchant-data' "$init_file"; then
  pass "POST /mcp initialize with the token is 200"
else
  fail "POST /mcp initialize with the token is $code (want 200 naming india-merchant-data)"
fi

[ "$FAILURES" -eq 0 ]
