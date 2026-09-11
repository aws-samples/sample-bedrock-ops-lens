-- 011: cache-WRITE tokens on the proxy/client-telemetry tables.
--
-- Audit T02/T14: prompt tokens split three ways - fresh input, cache reads and
-- cache WRITES (the Bedrock Runtime TokenUsage structure carries all three as
-- separate counters). The proxy tables only had cache_read_tokens, so a
-- cache-creation count reported by LiteLLM (`cache_creation_input_tokens`) or by
-- an OTEL emitter (`gen_ai.usage.cache_creation_input_tokens`) was parsed and
-- then dropped on the floor - and any cached-share computed from these tables
-- would have the same inflated denominator that finding 14 fixed for f_daily.
--
-- Idempotent: safe to re-run. Rollback:
--   ALTER TABLE f_proxy_dim_hourly DROP COLUMN IF EXISTS cache_write_tokens;
--   ALTER TABLE f_request_events   DROP COLUMN IF EXISTS cache_write_tokens;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'f_proxy_dim_hourly') THEN
        ALTER TABLE f_proxy_dim_hourly
            ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'f_request_events') THEN
        ALTER TABLE f_request_events
            ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0;
    END IF;
END $$;

COMMENT ON COLUMN f_proxy_dim_hourly.cache_write_tokens IS
    'Prompt tokens written to cache, as reported by the client/proxy. Disjoint '
    'from input_tokens and cache_read_tokens: a cached-share denominator is all '
    'three added (audit T02/14).';
