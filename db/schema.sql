-- ============================================================================
-- Bedrock Ops Lens — Customer-facing schema
--
-- Target: Postgres 15+ (= Aurora Postgres Serverless v2 wire-compatible).
-- Local dev: works against vanilla Homebrew/Docker postgres:15-alpine unchanged.
-- Idempotent: safe to re-run. Uses CREATE ... IF NOT EXISTS everywhere.
--
-- Two data paths feed this schema:
--   1. Volumetric — CloudWatch Metrics (AWS/Bedrock) → f_daily, f_hourly_peak,
--      f_hourly_errors, f_latency_daily, f_context_length. No per-request tags
--      possible (CW Metrics is bucketed by dimension only).
--   2. Tag-attributed — Bedrock model-invocation logs in S3 (the requestMetadata
--      field, see https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-request-metadata.html)
--      → f_daily_tagged. One log row with N tags fans out to N rows.
--
-- HARD RULE: never sum across the two paths. Volumetric totals come from f_daily.
-- Tag-grouped queries come from f_daily_tagged (always with WHERE tag_key=...).
-- Mixing them double-counts.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- f_daily — volumetric daily aggregates from CloudWatch Metrics.
--
-- One row per (event_date, accountId, modelId, region, operation, traffic_type,
-- service_tier, inference_profile_prefix). No tag dimension here — tags live
-- in f_daily_tagged.
--
-- Partitioned monthly on event_date so old months can be detached/dropped
-- cheaply. PRIMARY KEY includes event_date (Postgres requires partition key in PK).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_daily (
    -- event_date is the partition key (must be a plain column, not generated).
    -- year/month/day are derived from it for API-response convenience and
    -- backwards-compat with the reference's response shape.
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,

    -- Dimensions: NOT NULL with sentinel '__none__' so GROUP BY queries don't
    -- need COALESCE and the natural-key PRIMARY KEY works.
    accountId                 TEXT NOT NULL,
    modelId                   TEXT NOT NULL,
    region                    TEXT NOT NULL,
    operation                 TEXT NOT NULL DEFAULT '__none__',
    traffic_type              TEXT NOT NULL DEFAULT '__none__',
    service_tier              TEXT NOT NULL DEFAULT '__none__',
    inference_profile_prefix  TEXT NOT NULL DEFAULT '__none__',

    -- Measures. BIGINT (not INTEGER) — token counts can hit 10^12+ at scale.
    total_requests                  BIGINT NOT NULL,
    successful_requests             BIGINT,
    failed_requests                 BIGINT,
    total_input_tokens              BIGINT,
    total_output_tokens             BIGINT,
    total_cache_read_input_tokens   BIGINT,
    total_cache_write_input_tokens  BIGINT,
    status_400_count                BIGINT,
    status_403_count                BIGINT,
    status_429_count                BIGINT,
    status_500_count                BIGINT,
    status_503_count                BIGINT,

    PRIMARY KEY (event_date, accountId, modelId, region, operation,
                 traffic_type, service_tier, inference_profile_prefix)
) PARTITION BY RANGE (event_date);

-- Partitioned indexes — each cascades to every child partition.
-- BRIN is a few KB regardless of table size and is perfect for time-ordered data.
CREATE INDEX IF NOT EXISTS ix_f_daily_event_date_brin ON f_daily USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_daily_account         ON f_daily (event_date, accountId);
CREATE INDEX IF NOT EXISTS ix_f_daily_model           ON f_daily (event_date, modelId);
CREATE INDEX IF NOT EXISTS ix_f_daily_region          ON f_daily (event_date, region);
CREATE INDEX IF NOT EXISTS ix_f_daily_traffic         ON f_daily (event_date, traffic_type);

-- Default partition catches any date outside explicit monthly partitions.
-- Real partitions are created by the ingestion job; bootstrap with current ± 1 month.
CREATE TABLE IF NOT EXISTS f_daily_default PARTITION OF f_daily DEFAULT;

-- ----------------------------------------------------------------------------
-- f_daily_tagged — daily aggregates fanned out per request-metadata tag.
--
-- A request with requestMetadata={"team":"orchestrator","environment":"prod"}
-- writes TWO rows here: one (tag_key='team', tag_value='orchestrator') and one
-- (tag_key='environment', tag_value='prod'). Customer queries always include
-- WHERE tag_key='team' (or whichever) so the per-tag math is correct.
--
-- Schema mirrors f_daily but drops dimensions that aren't useful at the per-tag
-- level (service_tier, inference_profile_prefix) to keep cardinality manageable.
-- Add them back later if a use case demands.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_daily_tagged (
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,

    accountId   TEXT NOT NULL,
    modelId     TEXT NOT NULL,
    region      TEXT NOT NULL,
    operation   TEXT NOT NULL DEFAULT '__none__',
    tag_key     TEXT NOT NULL,
    tag_value   TEXT NOT NULL,

    total_requests                  BIGINT NOT NULL,
    failed_requests                 BIGINT,
    total_input_tokens              BIGINT,
    total_output_tokens             BIGINT,
    total_cache_read_input_tokens   BIGINT,
    total_cache_write_input_tokens  BIGINT,

    PRIMARY KEY (event_date, accountId, modelId, region, operation, tag_key, tag_value)
) PARTITION BY RANGE (event_date);

CREATE INDEX IF NOT EXISTS ix_f_daily_tagged_brin   ON f_daily_tagged USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_daily_tagged_kv     ON f_daily_tagged (tag_key, tag_value, event_date);
CREATE INDEX IF NOT EXISTS ix_f_daily_tagged_account ON f_daily_tagged (event_date, accountId);
CREATE INDEX IF NOT EXISTS ix_f_daily_tagged_model  ON f_daily_tagged (event_date, modelId);

CREATE TABLE IF NOT EXISTS f_daily_tagged_default PARTITION OF f_daily_tagged DEFAULT;

-- ----------------------------------------------------------------------------
-- f_hourly_peak — hourly aggregate at (account, model, region) for peak RPM/TPM.
-- Used by Ops Insights for max-over-hour math. Smaller than full-dim hourly.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_hourly_peak (
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,
    hour        SMALLINT  NOT NULL,

    accountId   TEXT NOT NULL,
    modelId     TEXT NOT NULL,
    region      TEXT NOT NULL,

    total_requests      BIGINT NOT NULL,
    total_input_tokens  BIGINT,
    total_output_tokens BIGINT,
    status_429_count    BIGINT,

    PRIMARY KEY (event_date, hour, accountId, modelId, region)
) PARTITION BY RANGE (event_date);

CREATE INDEX IF NOT EXISTS ix_f_hourly_peak_brin    ON f_hourly_peak USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_hourly_peak_account ON f_hourly_peak (event_date, accountId);

CREATE TABLE IF NOT EXISTS f_hourly_peak_default PARTITION OF f_hourly_peak DEFAULT;

-- ----------------------------------------------------------------------------
-- f_hourly_errors — rolling 7-day window with per-status-code hourly breakdown.
-- Small enough (~85K rows) that full wipe + reload each refresh is simpler than
-- partitioning. NOT partitioned.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_hourly_errors (
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,
    hour        SMALLINT  NOT NULL,

    accountId   TEXT NOT NULL,
    modelId     TEXT NOT NULL,
    region      TEXT NOT NULL,

    total_requests      BIGINT NOT NULL,
    failed_requests     BIGINT,
    status_400_count    BIGINT,
    status_403_count    BIGINT,
    status_429_count    BIGINT,
    status_500_count    BIGINT,
    status_503_count    BIGINT,

    PRIMARY KEY (event_date, hour, accountId, modelId, region)
);

CREATE INDEX IF NOT EXISTS ix_f_hourly_errors_brin  ON f_hourly_errors USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_hourly_errors_model ON f_hourly_errors (modelId);

-- ----------------------------------------------------------------------------
-- f_daily_cost — daily Bedrock spend, broken out by accountId × service.
-- Source: AWS Cost Explorer GetCostAndUsage from the central account, grouped
-- by LINKED_ACCOUNT + SERVICE. The CE API reports each Bedrock model as its
-- own "service" (e.g., "Claude Opus 4 (Amazon Bedrock Edition)"), so the
-- service column is what powers the Spend-by-Model stacked chart.
--
-- Refresh: once daily; CE data lags 24-48h so finer-grained refresh wastes
-- API budget for no signal. Idempotent UPSERT on (event_date, accountId,
-- service, region).
--
-- Currency note: Cost Explorer always reports in the payer's billing
-- currency (typically USD). We store the raw value + currency code so a
-- non-USD payer's chart still labels axes correctly.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_daily_cost (
    event_date      DATE     NOT NULL,
    year            SMALLINT GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month           SMALLINT GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day             SMALLINT GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,

    accountId       TEXT NOT NULL,
    service         TEXT NOT NULL,    -- e.g., "Claude Opus 4 (Amazon Bedrock Edition)"
    region          TEXT NOT NULL DEFAULT '__none__',

    total_cost      NUMERIC(18, 6) NOT NULL,    -- raw amount in `currency`
    currency        TEXT NOT NULL DEFAULT 'USD',

    PRIMARY KEY (event_date, accountId, service, region)
);

CREATE INDEX IF NOT EXISTS ix_f_daily_cost_brin    ON f_daily_cost USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_daily_cost_account ON f_daily_cost (accountId, event_date);
CREATE INDEX IF NOT EXISTS ix_f_daily_cost_service ON f_daily_cost (service);

-- ----------------------------------------------------------------------------
-- f_latency_daily — pre-computed percentiles per (model, traffic_type, region, day).
-- Percentiles cannot be re-aggregated, so they must be stored at the granularity
-- they're queried. CloudWatch returns p50/p90/p99 directly per dimension bucket.
-- Small table — not partitioned.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_latency_daily (
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,

    modelId       TEXT NOT NULL,
    traffic_type  TEXT NOT NULL DEFAULT '__none__',
    region        TEXT NOT NULL DEFAULT '__none__',

    sample_count  BIGINT,
    avg_e2e       DOUBLE PRECISION,
    p50_e2e       DOUBLE PRECISION,
    p90_e2e       DOUBLE PRECISION,
    p99_e2e       DOUBLE PRECISION,
    avg_ttft      DOUBLE PRECISION,
    p50_ttft      DOUBLE PRECISION,
    p90_ttft      DOUBLE PRECISION,
    p99_ttft      DOUBLE PRECISION,

    PRIMARY KEY (event_date, modelId, traffic_type, region)
);

CREATE INDEX IF NOT EXISTS ix_f_latency_brin  ON f_latency_daily USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_latency_model ON f_latency_daily (modelId);

-- ----------------------------------------------------------------------------
-- f_context_length — context-length routing variant (18k/51k/200k/1024k).
-- Source: invocation logs `routedModelId` (or equivalent CW dimension when
-- available). Small table — not partitioned.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_context_length (
    event_date  DATE      NOT NULL,
    year        SMALLINT  GENERATED ALWAYS AS (EXTRACT(YEAR  FROM event_date)::SMALLINT) STORED,
    month       SMALLINT  GENERATED ALWAYS AS (EXTRACT(MONTH FROM event_date)::SMALLINT) STORED,
    day         SMALLINT  GENERATED ALWAYS AS (EXTRACT(DAY   FROM event_date)::SMALLINT) STORED,

    accountId        TEXT NOT NULL,
    modelId          TEXT NOT NULL,
    routed_model_id  TEXT NOT NULL,
    region           TEXT NOT NULL,

    total_requests      BIGINT NOT NULL,
    total_input_tokens  BIGINT,

    PRIMARY KEY (event_date, accountId, modelId, routed_model_id, region)
);

CREATE INDEX IF NOT EXISTS ix_f_ctx_brin  ON f_context_length USING BRIN (event_date);
CREATE INDEX IF NOT EXISTS ix_f_ctx_model ON f_context_length (modelId);

-- ----------------------------------------------------------------------------
-- f_quotas — Service Quotas: default + applied per (account, region, quota_code).
--
-- `default_value`  — from list_aws_default_service_quotas (global default).
-- `applied_value`  — from get_service_quota (account-specific, post-increase).
--
-- The dashboard's burndown math should use applied_value when present (real
-- headroom) but the UI surfaces both so customers see how much quota they
-- already gained vs the default.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS f_quotas (
    accountId        TEXT NOT NULL,
    region           TEXT NOT NULL,
    quota_code       TEXT NOT NULL,
    quota_name       TEXT NOT NULL,
    model_name       TEXT NOT NULL,
    -- 'On-demand' | 'Cross-region' | 'Global cross-region'
    traffic_type     TEXT NOT NULL,
    -- 'RPM' | 'TPM'
    metric           TEXT NOT NULL,
    default_value    DOUBLE PRECISION,
    applied_value    DOUBLE PRECISION,
    adjustable       BOOLEAN NOT NULL DEFAULT TRUE,
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (accountId, region, quota_code)
);

CREATE INDEX IF NOT EXISTS ix_f_quotas_model ON f_quotas (model_name, region);

-- ----------------------------------------------------------------------------
-- dim_tags — distinct (tag_key, tag_value) seen recently, with rollup counts.
-- Refreshed daily from f_daily_tagged. Backs the top-bar tag-picker dropdowns —
-- never query f_daily_tagged for dropdown population (full table scan).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_tags (
    tag_key             TEXT NOT NULL,
    tag_value           TEXT NOT NULL,
    first_seen          DATE NOT NULL,
    last_seen           DATE NOT NULL,
    total_requests_30d  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tag_key, tag_value)
);

CREATE INDEX IF NOT EXISTS ix_dim_tags_key_volume ON dim_tags (tag_key, total_requests_30d DESC);

-- ----------------------------------------------------------------------------
-- dim_model_lifecycle — Bedrock foundation model lifecycle status.
-- Refreshed by ingestion/model_lifecycle.py via bedrock:ListFoundationModels
-- (no scraping, no JSON files — pure live AWS API). One row per model per
-- region because the same modelId can have different lifecycle dates in
-- different regions (per the AWS docs page).
--
--   status                'ACTIVE' | 'LEGACY'  (string, mirrors the API enum)
--   start_of_life_time    when the model was first published on Bedrock
--   legacy_time           when the model entered the Legacy state
--   public_extended_access_time   start of the (post-2026-02-01) extended-access
--                                 phase when pricing may rise
--   end_of_life_time      hard EOL — requests fail after this date
--   model_name            human-readable, from API (e.g. "Claude 3 Haiku")
--   provider              from API (e.g. "Anthropic")
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_model_lifecycle (
    modelId                       TEXT NOT NULL,
    region                        TEXT NOT NULL,
    status                        TEXT NOT NULL,
    model_name                    TEXT,
    provider                      TEXT,
    start_of_life_time            TIMESTAMPTZ,
    legacy_time                   TIMESTAMPTZ,
    public_extended_access_time   TIMESTAMPTZ,
    end_of_life_time              TIMESTAMPTZ,
    refreshed_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (modelId, region)
);

CREATE INDEX IF NOT EXISTS ix_dim_model_lifecycle_status
  ON dim_model_lifecycle (status) WHERE status = 'LEGACY';

-- ----------------------------------------------------------------------------
-- user_preferences — per-user pinned tag keys for the top-bar.
-- user_id is the Cognito `sub` claim (or 'default' if auth disabled in dev).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id             TEXT PRIMARY KEY,
    pinned_tag_keys     TEXT[] NOT NULL DEFAULT '{}',
    default_time_range  TEXT,
    default_provider    TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- ingestion_meta — last-refresh timestamp + arbitrary key/value freshness facts.
-- Read by /api/mirror-status and the freshness pill in the UI.
-- (Renamed from `mirror_meta` in the reference: no SQLite mirror in this build.)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- ingestion_days — per-day checkpoint coverage so the ingestion job is resumable.
-- A (table_name, event_date) row exists if that chunk completed successfully.
-- "Reload-recent" pattern: the job always re-pulls today and yesterday even
-- when a row exists (CW Metrics data lags by minutes-to-hours).
-- (Renamed from `mirror_days` in the reference.)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_days (
    table_name  TEXT     NOT NULL,
    event_date  DATE     NOT NULL,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count   BIGINT   NOT NULL,
    PRIMARY KEY (table_name, event_date)
);

CREATE INDEX IF NOT EXISTS ix_ingestion_days_loaded ON ingestion_days (loaded_at DESC);
