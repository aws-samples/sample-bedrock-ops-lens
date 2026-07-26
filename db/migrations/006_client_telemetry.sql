-- 006: client-telemetry extension of the proxy-events pipeline.
--
-- The event schema is transport-agnostic (proxy callback, OTEL collector→S3,
-- Claude Code events all land in the same NDJSON layout). This migration adds
-- the client-side fields those emitters carry beyond the original proxy set,
-- and widens `endpoint` semantics to direct-API paths:
--   'runtime' | 'mantle' | 'anthropic-api' | 'openai-api'
-- (endpoint is TEXT with no CHECK by design — the ingester normalizes.)
--
-- New per-request fields (all optional; absent = NULL/0):
--   ttft_ms        client-measured time-to-first-token/chunk (streaming)
--   retry_attempts total client attempts for the logical request (1 = none)
--   cost_usd_est   emitter-estimated cost (e.g. Claude Code api_request
--                  cost_usd). ESTIMATE — reconcile against CUR, never bill.

ALTER TABLE f_request_events ADD COLUMN IF NOT EXISTS ttft_ms        DOUBLE PRECISION;
ALTER TABLE f_request_events ADD COLUMN IF NOT EXISTS retry_attempts INTEGER;
ALTER TABLE f_request_events ADD COLUMN IF NOT EXISTS cost_usd_est   DOUBLE PRECISION;

ALTER TABLE f_proxy_dim_hourly ADD COLUMN IF NOT EXISTS p50_ttft_ms    DOUBLE PRECISION;
ALTER TABLE f_proxy_dim_hourly ADD COLUMN IF NOT EXISTS p90_ttft_ms    DOUBLE PRECISION;
ALTER TABLE f_proxy_dim_hourly ADD COLUMN IF NOT EXISTS retried_count  BIGINT NOT NULL DEFAULT 0;
ALTER TABLE f_proxy_dim_hourly ADD COLUMN IF NOT EXISTS cost_usd_est   DOUBLE PRECISION NOT NULL DEFAULT 0;
