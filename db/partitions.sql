-- ============================================================================
-- Partition bootstrap — creates monthly partitions for the partitioned tables.
-- Run once after schema.sql; the ingester re-runs this idempotently each month.
--
-- Partitioned tables: f_daily, f_daily_tagged, f_hourly_peak.
-- Each gets one partition per calendar month from current_month-12 through
-- current_month+2 (12 months back, current, +1, +2 — covers the dashboard's
-- 30-day default window plus a buffer for clock skew and pre-creation).
-- ============================================================================

DO $$
DECLARE
    parent       TEXT;
    parents      TEXT[] := ARRAY['f_daily', 'f_daily_tagged', 'f_hourly_peak'];
    m            DATE;
    range_start  DATE;
    range_end    DATE;
    part_name    TEXT;
BEGIN
    FOREACH parent IN ARRAY parents LOOP
        FOR offset_months IN -12..2 LOOP
            range_start := date_trunc('month', current_date)::date + (offset_months || ' months')::interval;
            range_end   := range_start + interval '1 month';
            part_name   := format('%s_%s', parent, to_char(range_start, 'YYYYMM'));

            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part_name, parent, range_start, range_end
            );
        END LOOP;
    END LOOP;
END $$;
