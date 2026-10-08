-- ============================================================================
-- Partition bootstrap — creates monthly partitions for the partitioned tables.
-- Runs after schema.sql on every deploy (SchemaInit), idempotently.
--
-- Partitioned tables: f_daily, f_daily_tagged, f_hourly_peak.
-- Each gets one partition per calendar month from current_month-12 through
-- current_month+2 (12 months back, current, +1, +2 — covers the dashboard's
-- 30-day default window plus a buffer for clock skew and pre-creation).
--
-- Rows for a month that had no partition yet land in the table's DEFAULT
-- partition. PostgreSQL refuses to create a partition while the default holds
-- rows for its range ("updated partition constraint for default partition ...
-- would be violated"), which failed every upgrade made more than two months
-- after the previous deploy. Such rows are moved into the new partition first.
-- The whole block is one transaction: no row moves unless every step succeeds.
-- ============================================================================

DO $$
DECLARE
    parent       TEXT;
    parents      TEXT[] := ARRAY['f_daily', 'f_daily_tagged', 'f_hourly_peak'];
    range_start  DATE;
    range_end    DATE;
    part_name    TEXT;
    default_name TEXT;
    cols         TEXT;
    misfiled     BOOLEAN;
BEGIN
    FOREACH parent IN ARRAY parents LOOP
        default_name := parent || '_default';
        SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO cols
          FROM pg_attribute
         WHERE attrelid = parent::regclass AND attnum > 0 AND NOT attisdropped
           AND attgenerated = '';                     -- generated columns are recomputed
        FOR offset_months IN -12..2 LOOP
            range_start := date_trunc('month', current_date)::date + (offset_months || ' months')::interval;
            range_end   := range_start + interval '1 month';
            part_name   := format('%s_%s', parent, to_char(range_start, 'YYYYMM'));
            CONTINUE WHEN to_regclass(part_name) IS NOT NULL;

            misfiled := false;
            IF to_regclass(default_name) IS NOT NULL THEN
                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM %I WHERE event_date >= %L AND event_date < %L)',
                    default_name, range_start, range_end) INTO misfiled;
            END IF;

            IF NOT misfiled THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                    part_name, parent, range_start, range_end);
            ELSE
                -- Build the month's table, move its rows out of the default
                -- partition, then attach it. Attaching creates the partitioned
                -- indexes, primary key included.
                EXECUTE format(
                    'CREATE TABLE %I (LIKE %I INCLUDING DEFAULTS INCLUDING CONSTRAINTS '
                    'INCLUDING GENERATED)',
                    part_name, parent);
                EXECUTE format(
                    'WITH moved AS (DELETE FROM %I WHERE event_date >= %L AND event_date < %L '
                    'RETURNING %s) INSERT INTO %I (%s) SELECT %s FROM moved',
                    default_name, range_start, range_end, cols, part_name, cols, cols);
                EXECUTE format(
                    'ALTER TABLE %I ATTACH PARTITION %I FOR VALUES FROM (%L) TO (%L)',
                    parent, part_name, range_start, range_end);
            END IF;
        END LOOP;
    END LOOP;
END $$;
