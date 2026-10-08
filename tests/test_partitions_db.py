"""db/partitions.sql against real PostgreSQL in a disposable local cluster.

Requires PostgreSQL 15+ binaries on PATH (skipped otherwise). The fixture
cluster has schema.sql and the migrations but no monthly partitions, so every
row lands in a DEFAULT partition: the state of a deployment upgraded more than
two months after its previous deploy. PostgreSQL refuses to create a partition
while the default partition holds rows for its range; upgrades failed exactly
that way.
"""
from __future__ import annotations

from datetime import date, timedelta

from test_inference_profiles_db import ACCOUNT, REGION, ROOT, conn, postgres  # noqa: F401

# SchemaInit runs with the default search path; the fixture's lens_read views
# would otherwise shadow the parent tables.
PARTITIONS = "SET LOCAL search_path = public;\n" + (ROOT / "db/partitions.sql").read_text()
TODAY = date.today()
LAST_MONTH = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=15)
LONG_AGO = TODAY.replace(day=1) - timedelta(days=600)        # outside the -12..+2 window


async def where(conn, table):
    rows = await conn.fetch(
        f"SELECT tableoid::regclass::text AS part, event_date FROM public.{table} ORDER BY event_date")
    return [(r["part"], r["event_date"]) for r in rows]


async def seed(conn):
    for d, model in [(TODAY, "a.model"), (LAST_MONTH, "b.model"), (LONG_AGO, "c.model")]:
        await conn.execute(
            """INSERT INTO public.f_daily (event_date, accountid, modelid, region, total_requests)
               VALUES ($1, $2, $3, $4, 7)""", d, ACCOUNT, model, REGION)
        await conn.execute(
            """INSERT INTO public.f_hourly_peak (event_date, hour, accountid, modelid, region,
                                                total_requests)
               VALUES ($1, 3, $2, $3, $4, 7)""", d, ACCOUNT, model, REGION)


async def test_rows_already_in_the_default_partition_move_to_their_month(conn):
    await seed(conn)
    assert {p for p, _ in await where(conn, "f_daily")} == {"f_daily_default"}
    await conn.execute(PARTITIONS)
    month = lambda d: f"f_daily_{d:%Y%m}"
    assert await where(conn, "f_daily") == [
        ("f_daily_default", LONG_AGO), (month(LAST_MONTH), LAST_MONTH), (month(TODAY), TODAY)]
    hourly = await where(conn, "f_hourly_peak")
    assert [p for p, _ in hourly] == [
        "f_hourly_peak_default", f"f_hourly_peak_{LAST_MONTH:%Y%m}", f"f_hourly_peak_{TODAY:%Y%m}"]
    assert await conn.fetchval("SELECT SUM(total_requests) FROM public.f_daily") == 21


async def test_a_moved_partition_keeps_the_primary_key_and_rejects_duplicates(conn):
    await seed(conn)
    await conn.execute(PARTITIONS)
    indexes = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = $1", f"f_daily_{TODAY:%Y%m}")
    assert any("UNIQUE" in r["indexdef"] for r in indexes)
    await conn.execute("SAVEPOINT dup")
    try:
        await conn.execute(
            """INSERT INTO public.f_daily (event_date, accountid, modelid, region, total_requests)
               VALUES ($1, $2, 'a.model', $3, 1)""", TODAY, ACCOUNT, REGION)
        raise AssertionError("duplicate key accepted")
    except Exception as exc:  # noqa: BLE001
        assert "duplicate key" in str(exc)
    finally:
        await conn.execute("ROLLBACK TO SAVEPOINT dup")


async def test_running_it_again_changes_nothing(conn):
    await seed(conn)
    await conn.execute(PARTITIONS)
    before = await where(conn, "f_daily")
    await conn.execute(PARTITIONS)
    assert await where(conn, "f_daily") == before


async def test_new_rows_route_to_the_created_partition(conn):
    await conn.execute(PARTITIONS)
    await conn.execute(
        """INSERT INTO public.f_daily (event_date, accountid, modelid, region, total_requests)
           VALUES ($1, $2, 'z.model', $3, 1)""", TODAY, ACCOUNT, REGION)
    assert await where(conn, "f_daily") == [(f"f_daily_{TODAY:%Y%m}", TODAY)]
