-- India Merchant Data API: SQLite schema (version 1).
-- Conventions: dates are ISO-8601 TEXT (YYYY-MM-DD), timestamps are ISO-8601 TEXT with offset,
-- money and rates are Decimal stored as TEXT (never REAL). Every data row links to fetch_log.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- One row per ingest invocation (backfill, refresh, canary).
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('backfill', 'refresh', 'canary')),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    summary_json TEXT NOT NULL DEFAULT '{}'
);

-- One row per upstream HTTP attempt (audit + provenance).
CREATE TABLE IF NOT EXISTS fetch_log (
    fetch_id     TEXT PRIMARY KEY,
    run_id       TEXT REFERENCES ingest_runs (run_id),
    source       TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    method       TEXT NOT NULL,
    url          TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    status_code  INTEGER,
    bytes        INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    fetched_at   TEXT NOT NULL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_fetch_log_source ON fetch_log (source, dataset, fetched_at);

CREATE TABLE IF NOT EXISTS offices (
    slug      TEXT PRIMARY KEY,
    rbi_id    INTEGER NOT NULL UNIQUE,
    name      TEXT NOT NULL,
    state     TEXT,
    fetch_id  TEXT REFERENCES fetch_log (fetch_id)
);

CREATE TABLE IF NOT EXISTS holidays (
    office_slug  TEXT NOT NULL REFERENCES offices (slug),
    date         TEXT NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('ni_act', 'closing_of_accounts')),
    fetch_id     TEXT REFERENCES fetch_log (fetch_id),
    PRIMARY KEY (office_slug, date, kind)
);

-- Which (office, year) pairs have been loaded. A loaded year may legitimately have no holidays.
CREATE TABLE IF NOT EXISTS holiday_years (
    office_slug  TEXT NOT NULL REFERENCES offices (slug),
    year         INTEGER NOT NULL,
    loaded_at    TEXT NOT NULL,
    fetch_id     TEXT REFERENCES fetch_log (fetch_id),
    PRIMARY KEY (office_slug, year)
);

-- Raw per-source rows. The `auto` merge (FBIL from 2018-07-10, RBI before) happens at read time.
CREATE TABLE IF NOT EXISTS fx_rates (
    currency      TEXT NOT NULL,
    date          TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('rbi', 'fbil')),
    rate          TEXT NOT NULL,
    unit          INTEGER NOT NULL CHECK (unit >= 1),
    published_at  TEXT,
    fetch_id      TEXT REFERENCES fetch_log (fetch_id),
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (currency, date, source)
);
CREATE INDEX IF NOT EXISTS ix_fx_rates_date ON fx_rates (currency, date);

CREATE TABLE IF NOT EXISTS mibor_rates (
    date          TEXT NOT NULL,
    tenor         TEXT NOT NULL,
    rate          TEXT NOT NULL,
    published_at  TEXT,
    fetch_id      TEXT REFERENCES fetch_log (fetch_id),
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (date, tenor)
);

-- Latest health per (source, dataset), written by refresh and canary.
CREATE TABLE IF NOT EXISTS source_health (
    source            TEXT NOT NULL,
    dataset           TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'broken', 'unknown')),
    checked_at        TEXT NOT NULL,
    last_success_at   TEXT,
    last_error_at     TEXT,
    last_error        TEXT,
    fingerprint_json  TEXT,
    drift_json        TEXT,
    PRIMARY KEY (source, dataset)
);

-- Domain events (fx.rates.published, holidays.updated, source.degraded, source.recovered).
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    event         TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    subscription_id  TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    events_json      TEXT NOT NULL,
    secret           TEXT NOT NULL,  -- needed to sign; returned to the caller only once, at creation
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id      TEXT PRIMARY KEY,
    subscription_id  TEXT NOT NULL REFERENCES webhook_subscriptions (subscription_id),
    event_id         TEXT NOT NULL REFERENCES events (event_id),
    attempt          INTEGER NOT NULL,
    status_code      INTEGER,
    error            TEXT,
    attempted_at     TEXT NOT NULL,
    succeeded        INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0, 1))
);
CREATE INDEX IF NOT EXISTS ix_deliveries_sub ON webhook_deliveries (subscription_id, attempted_at);
