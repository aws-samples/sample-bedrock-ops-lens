-- 012: model-lifecycle policy regime + EOL retention.
--
-- Two problems this fixes.
--
-- (1) EOL models VANISH from the Bedrock API. Once a model passes its EOL date
--     AWS removes it from all Regions: ListFoundationModels stops returning it
--     and GetFoundationModel raises ResourceNotFoundException (verified against
--     anthropic.claude-3-haiku-20240307-v1:0, amazon.nova-premier-v1:0,
--     amazon.nova-sonic-v1:0, cohere.command-r-v1:0 and command-r-plus-v1:0,
--     all of which the docs table still lists). The ingester used to
--     DELETE the whole table and re-INSERT, so an EOL'd model's row was
--     destroyed on the next run — making the "past EOL" state the dashboard
--     wants to report unreachable, and leaving a 4xx spike in the errors panel
--     with no way to tell which model caused it. We now UPSERT and keep rows
--     the API has stopped returning, flagged via api_visible/last_seen_at.
--
-- (2) Two lifecycle policies now coexist. Models launched on Bedrock BEFORE
--     2026-09-07 follow the legacy policy (>=12 months on Bedrock, >=6 month
--     Legacy period, and a public-extended-access phase with provider-set
--     price rises for EOL dates after 2026-02-01). Models launched ON OR AFTER
--     2026-09-07 follow the current policy: no extended-access phase at all,
--     and a per-model Legacy period of either 6 months or 45 days.
--       legacy policy:  https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html
--       current policy: https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
--     The API exposes no policy field, but it does return startOfLifeTime for
--     every model, so the regime is derivable from the launch date. The notice
--     period is likewise derivable once a model is in Legacy, as
--     end_of_life_time - legacy_time.
--
-- Deliberately NOT stored: the current policy's "EOL no sooner than" date and
-- the declared Legacy period for models that are still ACTIVE. Neither is in
-- the Bedrock API (both are model-card-only), and this dashboard does not
-- hardcode lifecycle facts AWS doesn't serve.
--
-- `status` keeps mirroring the API enum (ACTIVE|LEGACY) — we never write a
-- synthetic 'EOL' into it. Past-EOL is derived from end_of_life_time <= today,
-- which works for retained rows too.
--
-- Idempotent: safe to re-run. Rollback:
--   ALTER TABLE dim_model_lifecycle DROP COLUMN IF EXISTS lifecycle_policy;
--   ALTER TABLE dim_model_lifecycle DROP COLUMN IF EXISTS notice_period_days;
--   ALTER TABLE dim_model_lifecycle DROP COLUMN IF EXISTS last_seen_at;
--   ALTER TABLE dim_model_lifecycle DROP COLUMN IF EXISTS api_visible;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'dim_model_lifecycle') THEN

        ALTER TABLE dim_model_lifecycle
            ADD COLUMN IF NOT EXISTS lifecycle_policy   TEXT,
            ADD COLUMN IF NOT EXISTS notice_period_days INTEGER,
            ADD COLUMN IF NOT EXISTS last_seen_at       TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS api_visible        BOOLEAN NOT NULL DEFAULT TRUE;

        -- Backfill for rows written by the pre-012 ingester. Those rows only
        -- exist because the API returned them on the last run, so
        -- last_seen_at = refreshed_at and api_visible stays TRUE.
        UPDATE dim_model_lifecycle
           SET last_seen_at = refreshed_at
         WHERE last_seen_at IS NULL;

        -- Backfill the regime from the launch date we already store. Rows with
        -- a NULL start_of_life_time stay NULL rather than being guessed at.
        UPDATE dim_model_lifecycle
           SET lifecycle_policy = CASE
                   WHEN start_of_life_time >= TIMESTAMPTZ '2026-09-07 00:00:00+00'
                       THEN 'current'
                   ELSE 'legacy'
               END
         WHERE lifecycle_policy IS NULL
           AND start_of_life_time IS NOT NULL;

        UPDATE dim_model_lifecycle
           SET notice_period_days =
                   EXTRACT(DAY FROM (end_of_life_time - legacy_time))::INTEGER
         WHERE notice_period_days IS NULL
           AND legacy_time      IS NOT NULL
           AND end_of_life_time IS NOT NULL;
    END IF;
END $$;

-- Past-EOL lookups now have to reach rows the API no longer returns, so the
-- partial index on status='LEGACY' is not enough on its own.
CREATE INDEX IF NOT EXISTS ix_dim_model_lifecycle_eol
  ON dim_model_lifecycle (end_of_life_time)
  WHERE end_of_life_time IS NOT NULL;

COMMENT ON COLUMN dim_model_lifecycle.lifecycle_policy IS
    '''legacy'' = launched before 2026-09-07 (>=6mo Legacy period, may have a '
    'public-extended-access phase). ''current'' = launched on/after that date '
    '(no extended-access phase, Legacy period is 6 months OR 45 days). Derived '
    'from start_of_life_time; the API has no policy field.';

COMMENT ON COLUMN dim_model_lifecycle.notice_period_days IS
    'end_of_life_time - legacy_time, in days: how much warning this model '
    'actually gave. NULL until the model enters Legacy. ~184 = the legacy '
    'policy''s 6 months; ~45 = the current policy''s short option.';

COMMENT ON COLUMN dim_model_lifecycle.api_visible IS
    'FALSE once ListFoundationModels stops returning this (modelId, region) — '
    'which is what AWS does after EOL. The row is RETAINED so the dashboard can '
    'still report "past EOL and you are still calling it".';

COMMENT ON COLUMN dim_model_lifecycle.last_seen_at IS
    'Last ingest run in which the Bedrock API returned this (modelId, region).';
