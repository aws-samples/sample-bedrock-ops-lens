-- Migration 003: metric-coverage gaps from the AWS docs + internal wiki.
--
-- Adds columns/tables for:
--   F  Multimodal token breakdown  — f_daily text/speech input+output token cols
--   E  Legacy model invocations    — f_daily legacy_invocations col
--   A  Mantle token percentiles     — f_mantle_token_pctl table (p50/p90/p99 of
--                                      per-inference InputTokens/OutputTokens)
--   B  Mantle per-project usage      — f_mantle_project table (native Project dim)
--   G  Per-principal attribution     — f_identity_usage table (identity.arn from
--                                      invocation logs)
--
-- Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.
-- ============================================================================

BEGIN;

-- F: multimodal token breakdown (AWS/Bedrock InputTextTokenCount etc.) ---------
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS total_input_text_tokens   BIGINT;
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS total_input_speech_tokens BIGINT;
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS total_output_text_tokens  BIGINT;
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS total_output_speech_tokens BIGINT;
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS total_output_image_count  BIGINT;

-- E: legacy-model invocations (AWS/Bedrock LegacyModelInvocations) -------------
ALTER TABLE f_daily ADD COLUMN IF NOT EXISTS legacy_invocations BIGINT;

-- A: Mantle per-inference token percentiles (AWS/BedrockMantle InputTokens /
--    OutputTokens, Project+Model level → p50/p90/p99). Mantle-only.
CREATE TABLE IF NOT EXISTS f_mantle_token_pctl (
    event_date  DATE NOT NULL,
    accountId   TEXT NOT NULL,
    modelId     TEXT NOT NULL,
    region      TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT '__none__',
    sample_count BIGINT,
    p50_input_tokens  DOUBLE PRECISION,
    p90_input_tokens  DOUBLE PRECISION,
    p99_input_tokens  DOUBLE PRECISION,
    p50_output_tokens DOUBLE PRECISION,
    p90_output_tokens DOUBLE PRECISION,
    p99_output_tokens DOUBLE PRECISION,
    PRIMARY KEY (event_date, accountId, modelId, region, project)
);
CREATE INDEX IF NOT EXISTS ix_f_mantle_token_pctl_brin ON f_mantle_token_pctl USING BRIN (event_date);

-- B: Mantle per-project usage (native Project dimension → chargeback) ----------
CREATE TABLE IF NOT EXISTS f_mantle_project (
    event_date  DATE NOT NULL,
    accountId   TEXT NOT NULL,
    region      TEXT NOT NULL,
    project     TEXT NOT NULL,
    modelId     TEXT NOT NULL DEFAULT '__all__',
    total_requests      BIGINT,
    client_errors_4xx   BIGINT,
    total_input_tokens  BIGINT,
    total_output_tokens BIGINT,
    PRIMARY KEY (event_date, accountId, region, project, modelId)
);
CREATE INDEX IF NOT EXISTS ix_f_mantle_project_brin ON f_mantle_project USING BRIN (event_date);

-- G: per-principal attribution from invocation logs (identity.arn) -------------
-- Rolling window like f_hourly_status; populated only when invocation logging on.
CREATE TABLE IF NOT EXISTS f_identity_usage (
    event_date  DATE NOT NULL,
    accountId   TEXT NOT NULL,
    region      TEXT NOT NULL,
    identity_arn TEXT NOT NULL,
    modelId     TEXT NOT NULL DEFAULT '__all__',
    endpoint    TEXT NOT NULL DEFAULT 'runtime',
    total_requests      BIGINT,
    total_input_tokens  BIGINT,
    total_output_tokens BIGINT,
    failed_requests     BIGINT,
    PRIMARY KEY (event_date, accountId, region, identity_arn, modelId, endpoint)
);
CREATE INDEX IF NOT EXISTS ix_f_identity_usage_brin ON f_identity_usage USING BRIN (event_date);

INSERT INTO ingestion_meta (key, value, updated_at)
  VALUES ('cache_generation', extract(epoch from now())::text, now())
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

COMMIT;
