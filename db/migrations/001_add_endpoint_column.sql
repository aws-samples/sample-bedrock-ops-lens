-- Migration 001: add `endpoint` column to volumetric tables.
--
-- Why: AWS Bedrock now exposes two distinct API endpoints —
--   * bedrock-runtime  (Converse / InvokeModel; AWS/Bedrock CW namespace)
--   * bedrock-mantle   (Responses, Chat Completions, Anthropic Messages APIs;
--                       AWS/BedrockMantle CW namespace, launched 2026-06-01)
--
-- They publish to *different* CloudWatch namespaces, with different metric
-- names, and the same model can run under either endpoint. The dashboard
-- needs to show both side-by-side per tab. Adding `endpoint` to the
-- primary key lets the same (date, account, model, region) tuple have
-- separate rows for each endpoint without colliding.
--
-- Default value 'runtime' classifies every existing row correctly: the
-- only ingest path that has run before this migration is cw_metrics.py,
-- which reads AWS/Bedrock (= the runtime endpoint).
--
-- Idempotent: re-runnable without side effects. Each ADD COLUMN / DROP
-- CONSTRAINT / ADD CONSTRAINT is gated on the current state.
-- ============================================================================

BEGIN;

-- f_daily ---------------------------------------------------------------------
ALTER TABLE f_daily
  ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT 'runtime';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'f_daily'::regclass
      AND contype  = 'p'
      AND NOT array['endpoint'::name] <@ (
        SELECT array_agg(attname::name)
        FROM unnest(conkey) AS k JOIN pg_attribute a
          ON a.attrelid = conrelid AND a.attnum = k
      )
  ) THEN
    ALTER TABLE f_daily DROP CONSTRAINT f_daily_pkey;
    ALTER TABLE f_daily ADD CONSTRAINT f_daily_pkey PRIMARY KEY
      (event_date, accountId, modelId, region, operation,
       traffic_type, service_tier, inference_profile_prefix, endpoint);
  END IF;
END $$;

-- f_hourly_peak ---------------------------------------------------------------
ALTER TABLE f_hourly_peak
  ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT 'runtime';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'f_hourly_peak'::regclass
      AND contype  = 'p'
      AND NOT array['endpoint'::name] <@ (
        SELECT array_agg(attname::name)
        FROM unnest(conkey) AS k JOIN pg_attribute a
          ON a.attrelid = conrelid AND a.attnum = k
      )
  ) THEN
    ALTER TABLE f_hourly_peak DROP CONSTRAINT f_hourly_peak_pkey;
    ALTER TABLE f_hourly_peak ADD CONSTRAINT f_hourly_peak_pkey PRIMARY KEY
      (event_date, hour, accountId, modelId, region, endpoint);
  END IF;
END $$;

-- f_hourly_errors -------------------------------------------------------------
ALTER TABLE f_hourly_errors
  ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT 'runtime';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'f_hourly_errors'::regclass
      AND contype  = 'p'
      AND NOT array['endpoint'::name] <@ (
        SELECT array_agg(attname::name)
        FROM unnest(conkey) AS k JOIN pg_attribute a
          ON a.attrelid = conrelid AND a.attnum = k
      )
  ) THEN
    ALTER TABLE f_hourly_errors DROP CONSTRAINT f_hourly_errors_pkey;
    ALTER TABLE f_hourly_errors ADD CONSTRAINT f_hourly_errors_pkey PRIMARY KEY
      (event_date, hour, accountId, modelId, region, endpoint);
  END IF;
END $$;

-- f_latency_daily -------------------------------------------------------------
-- Mantle does not publish CW latency, so this table will only ever have
-- rows with endpoint='runtime' from CW, plus 'mantle' rows derived from
-- invocation logs (when the customer enables them).
ALTER TABLE f_latency_daily
  ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT 'runtime';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'f_latency_daily'::regclass
      AND contype  = 'p'
      AND NOT array['endpoint'::name] <@ (
        SELECT array_agg(attname::name)
        FROM unnest(conkey) AS k JOIN pg_attribute a
          ON a.attrelid = conrelid AND a.attnum = k
      )
  ) THEN
    ALTER TABLE f_latency_daily DROP CONSTRAINT f_latency_daily_pkey;
    ALTER TABLE f_latency_daily ADD CONSTRAINT f_latency_daily_pkey PRIMARY KEY
      (event_date, modelId, traffic_type, region, endpoint);
  END IF;
END $$;

-- Index helps endpoint-filtered queries on every table. Cheap to add.
CREATE INDEX IF NOT EXISTS ix_f_daily_endpoint        ON f_daily        (event_date, endpoint);
CREATE INDEX IF NOT EXISTS ix_f_hourly_peak_endpoint  ON f_hourly_peak  (event_date, endpoint);
CREATE INDEX IF NOT EXISTS ix_f_hourly_errors_endpoint ON f_hourly_errors (event_date, endpoint);

-- Bump cache_generation so the backend invalidates its Memcached entries.
INSERT INTO ingestion_meta (key, value, updated_at)
  VALUES ('cache_generation', extract(epoch from now())::text, now())
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

COMMIT;
