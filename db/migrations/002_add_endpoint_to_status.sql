-- Migration 002: add `endpoint` column to f_hourly_status.
--
-- Why: the "Status Codes" chart (Health & Errors tab) reads f_hourly_status,
-- but the table had no `endpoint` column — so the runtime and mantle
-- sub-tabs rendered the SAME per-status-code series. Invocation logs (the
-- only source for per-code data) already classify each request as
-- runtime/mantle; this lets the chart slice them apart.
--
-- Default 'runtime' classifies every pre-migration row correctly: before this
-- change every f_hourly_status row came from the runtime-only per-code tally.
--
-- Idempotent: re-runnable. ADD COLUMN / DROP+ADD CONSTRAINT are state-gated.
-- ============================================================================

BEGIN;

ALTER TABLE f_hourly_status
  ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT 'runtime';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'f_hourly_status'::regclass
      AND contype  = 'p'
      AND NOT array['endpoint'::name] <@ (
        SELECT array_agg(attname::name)
        FROM unnest(conkey) AS k JOIN pg_attribute a
          ON a.attrelid = conrelid AND a.attnum = k
      )
  ) THEN
    ALTER TABLE f_hourly_status DROP CONSTRAINT f_hourly_status_pkey;
    ALTER TABLE f_hourly_status ADD CONSTRAINT f_hourly_status_pkey PRIMARY KEY
      (event_date, hour, accountId, modelId, region, endpoint);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_f_hourly_status_endpoint
  ON f_hourly_status (event_date, endpoint);

-- Bump cache_generation so the backend invalidates its Memcached entries.
INSERT INTO ingestion_meta (key, value, updated_at)
  VALUES ('cache_generation', extract(epoch from now())::text, now())
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

COMMIT;
