-- 010: give f_latency_daily an account grain, and TTFT its own sample count.
--
-- Audit findings 04 and 09.
--
-- 04: the table's key was (event_date, modelId, traffic_type, region, endpoint)
--     with NO accountId, but ingestion runs PER ACCOUNT and upserts with
--     DO UPDATE SET. Account B's row therefore replaced account A's for the
--     same key: A's 100 samples averaging 10 ms and B's 900 averaging 1,000 ms
--     left only B's numbers, while the combined population averages 901 ms.
--     Account filters were also disabled on the read side (has_account=False),
--     so every account showed identical latency.
--
-- 09: TTFT was aggregated using the E2E `sample_count` as its weight and
--     denominator. TimeToFirstToken is only emitted for STREAMING operations
--     (ConverseStream / InvokeModelWithResponseStream), so mixing in
--     non-streaming observations divides by too large a denominator: 900
--     non-streaming + 100 streaming at 200 ms reported 20 ms. TTFT needs its
--     own count.
--
-- COMPATIBILITY / BACKFILL
--   accountId defaults to '__unknown__' for pre-existing rows. Those samples
--   genuinely cannot be attributed after the fact — the overwriting upsert
--   destroyed the information and CloudWatch would have to be re-queried to
--   recover it. We do NOT invent an owner: '__unknown__' is surfaced as
--   "unattributed" and excluded from per-account conclusions. Re-running the
--   ingester repopulates real account rows going forward.
--   ttft_sample_count is NULL for pre-existing rows, which readers must treat
--   as "unknown TTFT population" rather than zero.
--
-- ROLLBACK
--   This table is a pure aggregate of CloudWatch metrics, so it is rebuildable:
--     BEGIN;
--       ALTER TABLE f_latency_daily DROP CONSTRAINT f_latency_daily_pkey;
--       ALTER TABLE f_latency_daily
--         ADD PRIMARY KEY (event_date, modelId, traffic_type, region, endpoint);
--       ALTER TABLE f_latency_daily DROP COLUMN IF EXISTS accountId;
--       ALTER TABLE f_latency_daily DROP COLUMN IF EXISTS ttft_sample_count;
--     COMMIT;
--   (Duplicate keys can block the old PK if multiple accounts are present;
--    TRUNCATE f_latency_daily first, then re-ingest, if that happens.)

ALTER TABLE f_latency_daily
    ADD COLUMN IF NOT EXISTS accountId TEXT NOT NULL DEFAULT '__unknown__';

ALTER TABLE f_latency_daily
    ADD COLUMN IF NOT EXISTS ttft_sample_count BIGINT;

-- Re-key on the account grain. Guarded so the migration is idempotent.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'f_latency_daily' AND c.contype = 'p'
          AND NOT EXISTS (
              SELECT 1 FROM pg_attribute a
              WHERE a.attrelid = t.oid
                AND a.attname = 'accountid'
                AND a.attnum = ANY(c.conkey)
          )
    ) THEN
        EXECUTE 'ALTER TABLE f_latency_daily DROP CONSTRAINT '
             || (SELECT c.conname FROM pg_constraint c
                 JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = 'f_latency_daily' AND c.contype = 'p');
        EXECUTE 'ALTER TABLE f_latency_daily ADD PRIMARY KEY '
             || '(event_date, accountId, modelId, traffic_type, region, endpoint)';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_f_latency_account
    ON f_latency_daily (accountId, event_date);
