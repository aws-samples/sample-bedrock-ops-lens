-- 007: per-account latency on the hourly status grain.
--
-- Powers the "accounts impacted" drill-downs (Errors + Latency tabs).
-- f_hourly_status already carries (hour, account, model, region, endpoint)
-- from invocation logs; the ingester parses per-request latencyMs anyway,
-- so summing it here adds account-grain latency without a new table.
-- avg = latency_sum_ms / latency_count (percentiles aren't additive; avg +
-- the existing model-level percentiles in f_latency_daily cover the need).

ALTER TABLE f_hourly_status ADD COLUMN IF NOT EXISTS latency_sum_ms DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE f_hourly_status ADD COLUMN IF NOT EXISTS latency_count  BIGINT NOT NULL DEFAULT 0;
