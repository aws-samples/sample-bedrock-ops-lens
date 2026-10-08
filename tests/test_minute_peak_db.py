"""The minute-peak and profile endpoints against a real PostgreSQL.

Exercises the actual router functions and the real lens_read projection, so the
SQL is proven rather than assumed. Mirrors tests/test_inference_profiles_db.py.

Run: python -m pytest tests/test_minute_peak_db.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

asyncpg = pytest.importorskip("asyncpg")

from app import db  # noqa: E402
from app.filters import FilterSet  # noqa: E402
from app.routers import errors as errors_api  # noqa: E402
from app.routers import ops_insights  # noqa: E402

pytestmark = pytest.mark.asyncio

TODAY = date.today()
ACCOUNT, REGION = "111111111111", "us-east-1"
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
PROFILE_ID = "aaaaaa111111"
PROFILE_ARN = (f"arn:aws:bedrock:{REGION}:{ACCOUNT}:"
               f"application-inference-profile/{PROFILE_ID}")


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    for command in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(command):
            pytest.skip(f"local PostgreSQL binary unavailable: {command}")
    base = tmp_path_factory.mktemp("lens-min-pg")
    data = base / "data"
    socket_dir = tempfile.TemporaryDirectory(prefix="lens-min-", dir="/tmp")
    sock = Path(socket_dir.name)
    r = subprocess.run(["initdb", "-D", str(data), "-U", "lens_min_test", "--no-locale",
                        "--encoding=UTF8", "--auth-local=trust", "--auth-host=reject",
                        "--no-sync"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    options = f"-F -k {shlex.quote(str(sock))} -p 55474 -h ''"
    r = subprocess.run(["pg_ctl", "-D", str(data), "-l", str(base / "server.log"),
                        "-o", options, "-w", "start"], capture_output=True, text=True)
    assert r.returncode == 0, (base / "server.log").read_text()
    try:
        for sql in [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]:
            r = subprocess.run(["psql", "-h", str(sock), "-p", "55474", "-U", "lens_min_test",
                                "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", str(sql)],
                               capture_output=True, text=True)
            assert r.returncode == 0, f"{sql.name}: {r.stderr}"
        yield {"host": str(sock), "port": 55474, "user": "lens_min_test", "database": "postgres"}
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"],
                       capture_output=True, check=True)
        socket_dir.cleanup()


@pytest.fixture
async def conn(postgres, monkeypatch):
    c = await asyncpg.connect(**postgres)
    tx = c.transaction()
    await tx.start()
    await c.execute("SET LOCAL search_path = lens_read, public")
    monkeypatch.setattr(db, "fetch", c.fetch)
    monkeypatch.setattr(db, "fetchrow", c.fetchrow)
    monkeypatch.setattr(db, "fetchval", c.fetchval)
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


def filters(**kw):
    return FilterSet(start=TODAY - timedelta(days=2), end=TODAY, **kw)


async def add_profile(c, model=MODEL, profile_id=PROFILE_ID, name="expense-assistant"):
    await c.execute(
        """INSERT INTO public.dim_inference_profiles
             (accountid, region, profile_id, profile_arn, profile_name, model_id,
              model_arns, destination_regions)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        ACCOUNT, REGION, profile_id,
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/{profile_id}",
        name, model, [f"arn:aws:bedrock:{REGION}::foundation-model/{model}"], [REGION])


async def add_daily(c, mid, requests, failed=0):
    await c.execute(
        """INSERT INTO public.f_daily
             (event_date, accountid, modelid, region, endpoint, total_requests,
              successful_requests, failed_requests, status_429_count)
           VALUES ($1,$2,$3,$4,'runtime',$5::bigint,$5::bigint-$6::bigint,$6::bigint,$6::bigint)
           ON CONFLICT (event_date,accountid,modelid,region,operation,traffic_type,
                        service_tier,inference_profile_prefix,endpoint)
           DO UPDATE SET total_requests = public.f_daily.total_requests + EXCLUDED.total_requests,
                         failed_requests = public.f_daily.failed_requests + EXCLUDED.failed_requests""",
        TODAY, ACCOUNT, mid, REGION, requests, failed)


async def add_hourly(c, mid, hour, requests, est_tpm):
    await c.execute(
        """INSERT INTO public.f_hourly_peak
             (event_date, hour, accountid, modelid, region, endpoint,
              total_requests, total_input_tokens, total_output_tokens,
              estimated_tpm_quota_usage)
           VALUES ($1,$2,$3,$4,$5,'runtime',$6::bigint,$6::bigint*10,$6::bigint,$7::bigint)""",
        TODAY, hour, ACCOUNT, mid, REGION, requests, est_tpm)


async def add_minute(c, mid, quota_tpm, rpm=10, source="aws_estimate",
                     source_ids=None, has_profile=False, day=None):
    await c.execute(
        """INSERT INTO public.f_minute_peak
             (event_date, accountid, modelid, region, endpoint, peak_rpm, peak_rpm_at,
              peak_input_tpm, peak_output_tpm, peak_quota_tpm, peak_quota_tpm_at,
              quota_tpm_source, burndown_rate, burndown_rate_source, source_ids,
              has_application_profile, active_minutes)
           VALUES ($1,$2,$3,$4,'runtime',$5,$6,$7,$8,$9,$6,$10,10,'bundled_catalog',
                   $11,$12,5)""",
        day or TODAY, ACCOUNT, mid, REGION, rpm,
        datetime.now(timezone.utc).replace(second=0, microsecond=0),
        quota_tpm // 2, quota_tpm // 20, quota_tpm, source,
        source_ids or [mid], has_profile)


# --------------------------------------------------------------------------- #
# Errors: model aggregate preserved, profile detail beneath
# --------------------------------------------------------------------------- #
async def test_errors_by_model_keeps_one_row_per_model_for_two_profiles(conn):
    """Two profiles on one model must produce ONE model row with the combined
    total — never a repeated model label."""
    await add_profile(conn, profile_id="aaaaaa111111", name="expense-assistant")
    await add_profile(conn, profile_id="bbbbbb222222", name="support-summarizer")
    await add_daily(conn, PROFILE_ARN, 100, failed=10)
    await add_daily(conn, "bbbbbb222222", 60, failed=6)
    await add_daily(conn, MODEL, 40, failed=4)          # direct traffic too

    rows = await errors_api.errors_by_model(filters())
    model_rows = [r for r in rows if (r.get("modelid") or r.get("modelId")) == MODEL]
    assert len(model_rows) == 1, "one row per model, not per profile"
    assert model_rows[0]["total_requests"] == 200       # 100 + 60 + 40
    assert model_rows[0]["failed_requests"] == 20
    assert model_rows[0]["has_application_profile"] is True
    assert model_rows[0]["profile_count"] == 2


async def test_errors_by_profile_detail_sums_back_to_the_model_total(conn):
    await add_profile(conn, profile_id="aaaaaa111111", name="expense-assistant")
    await add_profile(conn, profile_id="bbbbbb222222", name="support-summarizer")
    await add_daily(conn, PROFILE_ARN, 100, failed=10)
    await add_daily(conn, "bbbbbb222222", 60, failed=6)

    detail = await errors_api.errors_by_profile(filters())
    assert {r["application_profile_name"] for r in detail} == {
        "expense-assistant", "support-summarizer"}
    # Keyed by account/Region/profile id, so same-named profiles stay distinct.
    assert all(r["accountid"] == ACCOUNT and r["region"] == REGION for r in detail)
    assert sum(r["total_requests"] for r in detail) == 160
    assert sum(r["failed_requests"] for r in detail) == 16


async def test_two_profiles_sharing_a_name_remain_distinct_rows(conn):
    """Profile NAMES are not unique. The detail grain must not collapse them."""
    await add_profile(conn, profile_id="aaaaaa111111", name="shared-name")
    await add_profile(conn, profile_id="bbbbbb222222", name="shared-name")
    await add_daily(conn, PROFILE_ARN, 70, failed=7)
    await add_daily(conn, "bbbbbb222222", 30, failed=3)

    detail = await errors_api.errors_by_profile(filters())
    assert len(detail) == 2
    assert {r["invoked_model_id"] for r in detail} == {PROFILE_ARN, "bbbbbb222222"}
    model_rows = [r for r in await errors_api.errors_by_model(filters())
                  if (r.get("modelid") or r.get("modelId")) == MODEL]
    assert len(model_rows) == 1 and model_rows[0]["total_requests"] == 100


async def test_an_unresolved_identifier_keeps_its_hash_and_is_flagged(conn):
    """No cached mapping: the identifier must survive untouched, with no guessed
    model and no fabricated profile name."""
    await add_daily(conn, "zzzzzz999999", 25, failed=5)
    rows = await errors_api.errors_by_model(filters())
    hit = [r for r in rows if (r.get("modelid") or r.get("modelId")) == "zzzzzz999999"]
    assert len(hit) == 1
    assert hit[0]["has_application_profile"] is True   # looks like a profile id
    assert hit[0]["profile_count"] == 0                # but nothing is cached
    detail = await errors_api.errors_by_profile(filters())
    unresolved = [r for r in detail if r["invoked_model_id"] == "zzzzzz999999"]
    assert len(unresolved) == 1
    assert unresolved[0]["application_profile_name"] is None


# --------------------------------------------------------------------------- #
# ops-peak-rpm: minute and hourly are both reported, never conflated
# --------------------------------------------------------------------------- #
async def test_measured_minute_is_reported_alongside_the_hourly_average(conn):
    await add_hourly(conn, MODEL, 9, requests=6000, est_tpm=180_000)
    await add_minute(conn, MODEL, quota_tpm=90_000, rpm=400)

    rows = await ops_insights.ops_peak_rpm(filters())
    row = next(r for r in rows if r["modelId"] == MODEL)
    # Hourly average is retained and is the lower bound: 180,000 / 60 = 3,000.
    assert row["busiest_hour_avg_quota_tpm"] == pytest.approx(3000)
    assert row["peak_minute_estimated_quota_tpm"] == 90_000
    assert row["measured_minute_available"] is True
    assert row["minute_rate_basis"] == "measured_minute"
    assert row["peak_minute_quota_tpm_source"] == "aws_estimate"
    # 90,000 / 3,000 = 30x understatement by the hourly grain.
    assert row["quota_tpm_burstiness_x"] == pytest.approx(30.0)


async def test_a_model_without_minute_data_reports_nulls_not_the_hourly_value(conn):
    """Silently reusing the hourly average under a minute-grain name would be a
    fabricated measurement."""
    await add_hourly(conn, MODEL, 9, requests=6000, est_tpm=180_000)
    rows = await ops_insights.ops_peak_rpm(filters())
    row = next(r for r in rows if r["modelId"] == MODEL)
    assert row["measured_minute_available"] is False
    assert row["peak_minute_estimated_quota_tpm"] is None
    assert row["peak_minute_rpm"] is None
    assert row["minute_rate_basis"] is None
    assert row["peak_minute_quota_tpm_source"] == "unavailable"
    assert row["quota_tpm_burstiness_x"] is None
    # The hourly baseline still works.
    assert row["busiest_hour_avg_quota_tpm"] == pytest.approx(3000)


async def test_mixed_provenance_across_days_is_reported_as_mixed(conn):
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=6000)
    await add_minute(conn, MODEL, quota_tpm=5000, source="aws_estimate", day=TODAY)
    await add_minute(conn, MODEL, quota_tpm=7000, source="reconstructed",
                     day=TODAY - timedelta(days=1))
    rows = await ops_insights.ops_peak_rpm(filters())
    row = next(r for r in rows if r["modelId"] == MODEL)
    assert row["peak_minute_quota_tpm_source"] == "mixed"
    assert row["peak_minute_estimated_quota_tpm"] == 7000   # max across the window
    assert row["minute_days_with_data"] == 2


async def test_date_filter_returns_only_that_days_peak(conn):
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=6000)
    await add_minute(conn, MODEL, quota_tpm=500, day=TODAY - timedelta(days=2))
    await add_minute(conn, MODEL, quota_tpm=100, day=TODAY)
    only_today = FilterSet(start=TODAY, end=TODAY)
    rows = await ops_insights.ops_peak_rpm(only_today)
    row = next(r for r in rows if r["modelId"] == MODEL)
    assert row["peak_minute_estimated_quota_tpm"] == 100, "day 2's 500 must not leak in"


async def test_collection_coverage_is_separate_from_active_datapoints(conn):
    """One reported invocation minute in a fully collected day is complete
    coverage with one active datapoint, not one minute of coverage."""
    await add_hourly(conn, MODEL, 9, requests=1, est_tpm=50)
    await add_minute(conn, MODEL, quota_tpm=50)
    start = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc)
    await conn.execute(
        """INSERT INTO public.f_minute_collection
             (event_date, accountid, region, endpoint, window_start, window_end,
              status, series_requested, series_complete, partial_day, last_success_at)
           VALUES ($1,$2,$3,'runtime',$4,$5,'complete',45,45,false,$5)""",
        TODAY, ACCOUNT, REGION, start, start + timedelta(days=1))
    rows = await ops_insights.ops_peak_rpm(filters())
    row = next(r for r in rows if r["modelId"] == MODEL)
    assert row["minute_collection"]["days_complete"] == 1
    assert row["minute_collection"]["days_incomplete"] == 0
    assert row["minute_active_minutes"] == 5


async def test_complete_collection_does_not_make_a_null_active_day_exact(conn):
    yesterday = TODAY - timedelta(days=1)
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=60000)
    for day in (yesterday, TODAY):
        await add_minute(conn, MODEL, quota_tpm=100, day=day)
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        await conn.execute(
            """INSERT INTO public.f_minute_collection
                 (event_date, accountid, region, endpoint, window_start, window_end,
                  status, series_requested, series_complete, partial_day, last_success_at)
               VALUES ($1,$2,$3,'runtime',$4,$5,'complete',5,5,false,$5)""",
            day, ACCOUNT, REGION, start, start + timedelta(days=1))
    await conn.execute(
        "UPDATE public.f_minute_peak SET peak_quota_tpm=NULL WHERE event_date=$1", TODAY)
    [model] = await ops_insights.ops_peak_rpm(FilterSet(start=yesterday, end=TODAY))
    assert model["minute_collection"]["days_complete"] == 2
    assert model["peak_minute_estimated_quota_tpm"] == 100
    assert model["busiest_hour_avg_quota_tpm"] == 1000
    assert model["minute_quota_complete"] is False
    assert model["minute_rpm_complete"] is True


async def test_peak_quota_resolution_uses_the_matching_model_and_family(conn):
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=6000)
    for code, name, family, limit in (
        ("L-wrong", "Claude Haiku 4.5", "On-demand", 1000000),
        ("L-global", "Claude Sonnet 4.5", "Global cross-region", 100000),
        ("L-exact", "Claude Sonnet 4.5", "On-demand", 10000),
    ):
        await conn.execute(
            """INSERT INTO public.f_quotas
                 (accountid, region, quota_code, quota_name, model_name, metric,
                  traffic_type, applied_value, default_value)
               VALUES ($1,$2,$3,$4,$4,'TPM',$5,$6,$6)""",
            ACCOUNT, REGION, code, name, family, limit)
    [model] = await ops_insights.ops_peak_rpm(
        FilterSet(start=TODAY, end=TODAY), include_quotas=True)
    assert model["quota_tpm"]["limit_per_minute"] == 10000
    assert model["quota_tpm"]["quota_family"] == "On-demand"


async def test_profile_minute_row_carries_its_contributing_sources(conn):
    """f_minute_peak is pre-resolved; source_ids records what was combined so a
    reader can see that profile and direct traffic were summed."""
    await add_profile(conn)
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=6000)
    await add_minute(conn, MODEL, quota_tpm=4000, has_profile=True,
                     source_ids=[MODEL, PROFILE_ARN])
    row = await conn.fetchrow(
        "SELECT source_ids, has_application_profile FROM public.f_minute_peak "
        "WHERE modelid = $1", MODEL)
    assert set(row["source_ids"]) == {MODEL, PROFILE_ARN}
    assert row["has_application_profile"] is True
    rows = await ops_insights.ops_peak_rpm(filters())
    assert next(r for r in rows if r["modelId"] == MODEL)["peak_minute_estimated_quota_tpm"] == 4000


# --------------------------------------------------------------------------- #
# The Overview profile-usage table
# --------------------------------------------------------------------------- #
async def test_arn_and_short_id_usage_combine_into_one_profile_row(conn):
    """A caller may pass the full ARN in some code paths and the bare
    12-character id in others. Both are the same profile, so the table must show
    ONE row with the combined usage — not the same profile twice with its usage
    split, which is what grouping on the invoked identifier produced.
    """
    from app.routers import inference_profiles as ipr
    await add_profile(conn)
    await add_daily(conn, PROFILE_ARN, 100)     # invoked by ARN
    await add_daily(conn, PROFILE_ID, 40)       # same profile, invoked by short id

    rows = await ipr.inference_profile_usage(filters())
    mine = [r for r in rows if r["application_profile_name"] == "expense-assistant"]
    assert len(mine) == 1, "ARN and short id must not appear as two rows"
    assert mine[0]["total_requests"] == 140
    assert mine[0]["invoked_form_count"] == 2
    assert set(mine[0]["invoked_identifiers"]) == {PROFILE_ARN, PROFILE_ID}
    assert mine[0]["resolved"] is True
    assert mine[0]["modelid"] == MODEL


async def test_profile_usage_shows_name_model_and_token_columns(conn):
    """The columns the table exists to answer: profile name, resolved model,
    requests, input tokens and output tokens, with account and Region."""
    await add_profile(conn)
    from app.routers import inference_profiles as ipr
    await conn.execute(
        """INSERT INTO public.f_daily
             (event_date, accountid, modelid, region, endpoint, total_requests,
              successful_requests, failed_requests, total_input_tokens, total_output_tokens)
           VALUES ($1,$2,$3,$4,'runtime',10,10,0,5000,700)""",
        TODAY, ACCOUNT, PROFILE_ARN, REGION)
    row = (await ipr.inference_profile_usage(filters()))[0]
    assert row["application_profile_name"] == "expense-assistant"
    assert row["modelid"] == MODEL and row["resolved"] is True
    assert row["total_requests"] == 10
    assert row["total_input_tokens"] == 5000
    assert row["total_output_tokens"] == 700
    assert row["accountid"] == ACCOUNT and row["region"] == REGION


async def test_an_unidentified_identifier_is_labelled_not_called_unresolved(conn):
    """It keeps its identifier, reports resolved=false and no model. The UI must
    say the model is not identified rather than implying the profile is broken."""
    from app.routers import inference_profiles as ipr
    await add_daily(conn, "zzzzzz999999", 25)
    rows = await ipr.inference_profile_usage(filters())
    hit = [r for r in rows if "zzzzzz999999" in r["invoked_identifiers"]]
    assert len(hit) == 1
    assert hit[0]["resolved"] is False
    assert hit[0]["application_profile_name"] is None
    assert hit[0]["modelid"] is None


async def test_profile_usage_honours_the_overview_filters(conn):
    """Account, Region and date filters must apply, so the panel agrees with the
    rest of the Overview page."""
    from app.routers import inference_profiles as ipr
    await add_profile(conn)
    await add_daily(conn, PROFILE_ARN, 100)
    assert await ipr.inference_profile_usage(filters(accounts={ACCOUNT})) != []
    assert await ipr.inference_profile_usage(filters(accounts={"999999999999"})) == []
    assert await ipr.inference_profile_usage(filters(region=REGION)) != []
    assert await ipr.inference_profile_usage(filters(region="eu-west-1")) == []
    old = FilterSet(start=TODAY - timedelta(days=30), end=TODAY - timedelta(days=20))
    assert await ipr.inference_profile_usage(old) == []
    assert await ipr.inference_profile_usage(filters(provider="anthropic")) != []
    assert await ipr.inference_profile_usage(filters(provider="amazon")) == []
    assert await ipr.inference_profile_usage(filters(endpoint="mantle")) == []
    assert await ipr.inference_profile_usage(filters(invalid=(("accounts", "invalid"),))) == []


async def test_cached_unidentified_profile_combines_forms_without_inventing_a_model(conn):
    from app.routers import inference_profiles as ipr
    await add_profile(conn)
    await conn.execute("UPDATE public.dim_inference_profiles SET model_id=NULL")
    await add_daily(conn, PROFILE_ARN, 100)
    await add_daily(conn, PROFILE_ID, 40)
    [row] = await ipr.inference_profile_usage(filters())
    assert row["total_requests"] == 140
    assert row["invoked_form_count"] == 2
    assert row["resolved"] is False and row["modelid"] is None


async def test_same_named_profiles_stay_distinct_and_do_not_add_direct_usage(conn):
    from app.routers import inference_profiles as ipr
    await add_profile(conn, name="Shared name")
    await add_profile(conn, profile_id="bbbbbb222222", name="Shared name")
    await add_daily(conn, PROFILE_ID, 40)
    await add_daily(conn, "bbbbbb222222", 60)
    await add_daily(conn, MODEL, 200)
    rows = await ipr.inference_profile_usage(filters())
    assert len(rows) == 2
    assert len({row["profile_key"] for row in rows}) == 2
    assert sum(row["total_requests"] for row in rows) == 100


async def test_api_coverage_excludes_older_failures_and_other_endpoints(conn):
    await add_hourly(conn, MODEL, 9, requests=100, est_tpm=6000)
    await add_minute(conn, MODEL, quota_tpm=500)
    start = datetime.combine(TODAY, datetime.min.time(), timezone.utc)
    for day, endpoint, status in [
            (TODAY, "runtime", "complete"),
            (TODAY - timedelta(days=20), "runtime", "failed"),
            (TODAY, "mantle", "failed")]:
        await conn.execute(
            """INSERT INTO public.f_minute_collection
                 (event_date,accountid,region,endpoint,window_start,window_end,
                  status,last_success_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$6)""",
            day, ACCOUNT, REGION, endpoint, start, start + timedelta(hours=12), status)
    [row] = await ops_insights.ops_peak_rpm(
        FilterSet(start=TODAY, end=TODAY, endpoint="runtime"))
    assert row["minute_coverage_complete"] is True
    assert row["minute_collection"]["days_attempted"] == 1
    assert row["minute_collection"]["days_incomplete"] == 0
    await conn.execute("UPDATE public.f_minute_peak SET resolution_stale=TRUE")
    [stale] = await ops_insights.ops_peak_rpm(
        FilterSet(start=TODAY, end=TODAY, endpoint="runtime"))
    assert stale["minute_coverage_status"] == "stale"
    assert stale["peak_minute_estimated_quota_tpm"] is None
