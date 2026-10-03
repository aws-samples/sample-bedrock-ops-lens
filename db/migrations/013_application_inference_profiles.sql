-- Preserve invocation identifiers in public facts. The API reads lens_read
-- projections so discovering a profile also resolves its historical usage.
-- No fact rows, primary keys, ingestion checkpoints, or counters are rewritten.
CREATE TABLE IF NOT EXISTS public.dim_inference_profiles (
    accountId           TEXT NOT NULL,
    region              TEXT NOT NULL,
    profile_id          TEXT NOT NULL,
    profile_arn         TEXT NOT NULL,
    profile_name        TEXT NOT NULL,
    model_id            TEXT,
    model_arns          TEXT[] NOT NULL,
    destination_regions TEXT[] NOT NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    api_visible         BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (accountId, region, profile_id),
    UNIQUE (accountId, region, profile_arn)
);

-- One match at most for either the ARN or the opaque ID, scoped to its owner
-- and source Region. A profile can reference several regional model ARNs;
-- ingestion sets model_id only when ALL of them identify the same model.
CREATE OR REPLACE VIEW public.inference_profile_aliases AS
SELECT p.*, profile_id AS invocation_id FROM public.dim_inference_profiles p
UNION ALL
SELECT p.*, profile_arn AS invocation_id FROM public.dim_inference_profiles p;

CREATE SCHEMA IF NOT EXISTS lens_read;

-- Build column-preserving projections from the existing schema. Keeping the
-- raw grain avoids merging percentiles, sparse tags, or request identities.
-- Only f_hourly_peak needs a combined per-hour row (defined below).
-- An uncached opaque 12-character ID only flags unknown quota routing; model
-- resolution always requires a validated, account/Region-scoped cache match.
DO $$
DECLARE
    fact TEXT;
    view_name TEXT;
    columns_sql TEXT;
    endpoint_guard TEXT;
BEGIN
    FOREACH fact IN ARRAY ARRAY[
        'f_daily', 'f_daily_tagged', 'f_daily_by_identity', 'f_hourly_peak',
        'f_hourly_errors', 'f_hourly_status', 'f_latency_daily',
        'f_context_length', 'f_request_events', 'f_proxy_dim_hourly',
        'f_identity_usage'
    ] LOOP
        SELECT string_agg(
            CASE WHEN column_name = 'modelid'
                 THEN 'COALESCE(p.model_id, f.modelid) AS modelid'
                 ELSE format('f.%I', column_name)
            END, ', ' ORDER BY ordinal_position),
            CASE WHEN bool_or(column_name = 'endpoint')
                 THEN ' AND f.endpoint = ''runtime''' ELSE '' END
          INTO columns_sql, endpoint_guard
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = fact;
        view_name := CASE WHEN fact = 'f_hourly_peak'
                          THEN 'hourly_profile_sources' ELSE fact END;
        EXECUTE format(
            'CREATE OR REPLACE VIEW lens_read.%I AS
             SELECT f.modelid AS invoked_model_id,
                    p.profile_arn AS application_profile_arn,
                    p.profile_name AS application_profile_name,
                    (p.profile_id IS NOT NULL OR
                      ((f.modelid ~ ''^arn:[^:]+:bedrock:.*:application-inference-profile/''
                        OR f.modelid ~ ''^[A-Za-z0-9]{12}$'') %s))
                      AS has_application_profile, %s
             FROM public.%I f
             LEFT JOIN public.inference_profile_aliases p
               ON p.accountid = f.accountid AND p.region = f.region
              AND p.invocation_id = f.modelid %s',
            view_name, endpoint_guard, columns_sql, fact, endpoint_guard);
    END LOOP;
END $$;

-- Sum coincident hours BEFORE consumers take a peak. Never add individual
-- peaks, and never treat a partial native TPM sum as a complete observation.
-- The existing API contract remains hourly totals / 60, not minute peaks.
CREATE OR REPLACE VIEW lens_read.f_hourly_peak AS
SELECT event_date, year, month, day, hour, accountId, modelId, region, endpoint,
       SUM(total_requests)::BIGINT AS total_requests,
       SUM(total_input_tokens)::BIGINT AS total_input_tokens,
       SUM(total_output_tokens)::BIGINT AS total_output_tokens,
       SUM(total_cache_read_input_tokens)::BIGINT AS total_cache_read_input_tokens,
       SUM(total_cache_write_input_tokens)::BIGINT AS total_cache_write_input_tokens,
       CASE WHEN COUNT(estimated_tpm_quota_usage) = COUNT(*)
            THEN SUM(estimated_tpm_quota_usage)::BIGINT END AS estimated_tpm_quota_usage,
       SUM(status_429_count)::BIGINT AS status_429_count,
       BOOL_OR(has_application_profile) AS has_application_profile
FROM lens_read.hourly_profile_sources
GROUP BY event_date, year, month, day, hour, accountId, modelId, region, endpoint;

COMMENT ON COLUMN public.dim_inference_profiles.model_id IS
    'Foundation model ID shared by all destination model ARNs; NULL if unresolved. '
    'List/GetInferenceProfile do not return modelSource.copyFrom, so this does '
    'not establish the original system profile or its quota routing family.';
