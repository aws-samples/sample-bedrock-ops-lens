-- 004_proxy_dimensions.sql
-- Generalize proxy per-workload attribution to ARBITRARY custom dimensions.
--
-- The original model hardcoded a single `workload TEXT` column on
-- f_proxy_usage_hourly / f_request_events / dim_workloads. Real customers want
-- to slice by any attribute their proxy emits — workload, env (prod/dev),
-- business_unit, cost_center, team, etc. — all at once.
--
-- New model mirrors the proven f_daily_tagged fan-out: the proxy emits a
-- `dimensions` map per request, and we store ONE rollup row per
-- (dim_key, dim_value, model, endpoint, region, hour). Summing across a single
-- dim_key is correct (each key covers 100% of the request's tokens); summing
-- across DIFFERENT keys would multiply-count, so queries always pin one key —
-- exactly the f_daily_tagged discipline.
--
-- `workload` is no longer special: it's just the conventional default key.
--
-- These tables never held real customer data (proxy ingestion is opt-in and
-- was validated only with disposable test events, since purged), so we drop &
-- recreate rather than do a lossy column migration.

-- ---------------------------------------------------------------------------
-- f_proxy_dim_hourly — hourly rollup, one row per (dim_key, dim_value, model,
-- endpoint, region). Powers per-dimension tokens / throttle / latency / quota.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS f_proxy_usage_hourly CASCADE;
CREATE TABLE IF NOT EXISTS f_proxy_dim_hourly (
    event_date     DATE NOT NULL,
    hour           SMALLINT NOT NULL,
    dim_key        TEXT NOT NULL,          -- e.g. 'workload', 'env', 'bu'
    dim_value      TEXT NOT NULL,          -- e.g. 'search-service', 'prod'
    modelId        TEXT NOT NULL,
    endpoint       TEXT NOT NULL DEFAULT 'runtime',
    region         TEXT NOT NULL,
    accountId      TEXT NOT NULL DEFAULT '__none__',
    total_requests BIGINT NOT NULL DEFAULT 0,
    input_tokens   BIGINT NOT NULL DEFAULT 0,
    output_tokens  BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    throttled_count BIGINT NOT NULL DEFAULT 0,
    error_count    BIGINT NOT NULL DEFAULT 0,
    p50_latency_ms DOUBLE PRECISION,
    p90_latency_ms DOUBLE PRECISION,
    p99_latency_ms DOUBLE PRECISION,
    PRIMARY KEY (event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId)
);
CREATE INDEX IF NOT EXISTS ix_f_proxy_dim_brin ON f_proxy_dim_hourly USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_proxy_dim_kv   ON f_proxy_dim_hourly (dim_key, dim_value, event_date);
CREATE INDEX IF NOT EXISTS ix_f_proxy_dim_model ON f_proxy_dim_hourly (event_date, modelId, region);

-- ---------------------------------------------------------------------------
-- dim_proxy_dimensions — distinct (dim_key, dim_value) pairs seen recently,
-- for the top-bar dimension:value picker. Mirrors dim_tags / dim_workloads.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_workloads CASCADE;
CREATE TABLE IF NOT EXISTS dim_proxy_dimensions (
    dim_key             TEXT NOT NULL,
    dim_value           TEXT NOT NULL,
    first_seen          DATE NOT NULL,
    last_seen           DATE NOT NULL,
    total_requests_30d  BIGINT NOT NULL DEFAULT 0,
    endpoints           TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (dim_key, dim_value)
);
CREATE INDEX IF NOT EXISTS ix_dim_proxy_key    ON dim_proxy_dimensions (dim_key, total_requests_30d DESC);

-- ---------------------------------------------------------------------------
-- f_request_events — raw per-request rows. Replace the single `workload`
-- column with a JSONB `dimensions` map so the full attribute set is retained
-- for ad-hoc drill-down. Recreated (was empty).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS f_request_events CASCADE;
CREATE TABLE IF NOT EXISTS f_request_events (
    ts             TIMESTAMPTZ NOT NULL,
    event_date     DATE NOT NULL,
    dimensions     JSONB NOT NULL DEFAULT '{}'::jsonb,
    modelId        TEXT NOT NULL,
    endpoint       TEXT NOT NULL DEFAULT 'runtime',
    region         TEXT NOT NULL,
    accountId      TEXT NOT NULL DEFAULT '__none__',
    input_tokens   BIGINT NOT NULL DEFAULT 0,
    output_tokens  BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    status         INTEGER NOT NULL DEFAULT 200,
    throttled      BOOLEAN NOT NULL DEFAULT false,
    latency_ms     DOUBLE PRECISION,
    request_id     TEXT NOT NULL,
    PRIMARY KEY (event_date, request_id, ts)
) PARTITION BY RANGE (event_date);
CREATE TABLE IF NOT EXISTS f_request_events_default PARTITION OF f_request_events DEFAULT;
CREATE INDEX IF NOT EXISTS ix_f_request_events_brin ON f_request_events USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_request_events_dims ON f_request_events USING GIN (dimensions);
