-- Busiest-MINUTE capacity observations, alongside the existing hourly rows.
--
-- WHY. f_hourly_peak stores Period=3600 Sum, so the API can only report an
-- hourly average per minute. AWS enforces token quotas per MINUTE, so a bursty
-- workload throttles while an hourly average still looks safe. Measured on
-- 2026-10-06 for us.anthropic.claude-sonnet-5: busiest-hour average 2,838 TPM
-- versus a true busiest minute of 90,888 TPM.
--
-- WHY THESE ROWS ARE PRE-RESOLVED. Unlike every other fact table, application
-- inference profile identifiers are resolved to their foundation model BY THE
-- INGESTER, before the peak is taken, and modelId here is already the effective
-- model. A peak cannot be recombined after the fact:
--
--     direct [100, 0] + profile [0, 100]  -> true peak 100, but maxima sum to 200
--     direct [100, 0] + profile [100, 0]  -> true peak 200, but max of maxima is 100
--
-- Once only per-identifier maxima survive, neither SUM nor MAX recovers the
-- right answer. So this table is deliberately NOT added to the lens_read
-- projection; source_ids records which raw identifiers were combined, and
-- resolution_stale marks rows whose mapping changed after the minute data
-- aged out of CloudWatch (1-minute retention is 15 days).
CREATE TABLE IF NOT EXISTS public.f_minute_peak (
    event_date              DATE    NOT NULL,
    accountId               TEXT    NOT NULL,
    modelId                 TEXT    NOT NULL,   -- effective model, already resolved
    region                  TEXT    NOT NULL,
    endpoint                TEXT    NOT NULL DEFAULT 'runtime',

    -- Each peak is the maximum over single UTC minutes within event_date, taken
    -- after summing every contributing source series minute by minute.
    peak_rpm                BIGINT,
    peak_rpm_at             TIMESTAMPTZ,
    peak_input_tpm          BIGINT,             -- InputTokenCount + CacheWriteInputTokens
    peak_output_tpm         BIGINT,             -- raw output tokens, unweighted
    peak_quota_tpm          BIGINT,             -- estimated quota burn; see source column
    peak_quota_tpm_at       TIMESTAMPTZ,        -- RPM / input / output can peak in other minutes

    -- aws_estimate = every contributing minute had EstimatedTPMQuotaUsage.
    -- reconstructed = none did, so input + cache_write + output*rate was used.
    -- mixed = some did. unavailable = nothing observed.
    quota_tpm_source        TEXT    NOT NULL DEFAULT 'unavailable',
    burndown_rate           INTEGER,
    burndown_rate_source    TEXT,

    source_ids              TEXT[]  NOT NULL DEFAULT '{}',
    has_application_profile BOOLEAN NOT NULL DEFAULT FALSE,

    -- Reported samples, NOT collection coverage. A quiet application is sparse
    -- by nature; coverage lives in f_minute_collection.
    active_minutes          INTEGER NOT NULL DEFAULT 0,
    resolution_stale        BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_date, accountId, modelId, region, endpoint)
);

CREATE INDEX IF NOT EXISTS ix_minute_peak_date  ON public.f_minute_peak(event_date);
CREATE INDEX IF NOT EXISTS ix_minute_peak_model ON public.f_minute_peak(modelId);

-- Collection coverage, tracked separately from active datapoints. A failed or
-- partial CloudWatch response must never be presented as measured zero usage.
CREATE TABLE IF NOT EXISTS public.f_minute_collection (
    event_date       DATE NOT NULL,
    accountId        TEXT NOT NULL,
    region           TEXT NOT NULL,
    endpoint         TEXT NOT NULL DEFAULT 'runtime',
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL,             -- complete | partial | failed
    detail           TEXT,
    series_requested INTEGER NOT NULL DEFAULT 0,
    series_complete  INTEGER NOT NULL DEFAULT 0,
    partial_day      BOOLEAN NOT NULL DEFAULT FALSE,  -- today, still accumulating
    last_success_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_date, accountId, region, endpoint)
);

COMMENT ON COLUMN public.f_minute_peak.modelId IS
    'Effective foundation model. Application inference profile identifiers are '
    'resolved by the ingester BEFORE the peak is taken, because per-identifier '
    'maxima cannot be recombined into a simultaneous peak afterwards.';
COMMENT ON COLUMN public.f_minute_peak.active_minutes IS
    'Minutes that reported at least one datapoint. NOT collection coverage - '
    'see f_minute_collection.status.';
COMMENT ON COLUMN public.f_minute_peak.peak_quota_tpm IS
    'Peak ESTIMATED quota TPM over one minute. EstimatedTPMQuotaUsage is an AWS '
    'approximation that excludes max_tokens reservation, so this is not the '
    'enforcement counter and must not be labelled as one.';
