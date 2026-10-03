"""Exercise real PostgreSQL views and readers in a disposable local cluster.

Requires PostgreSQL 15+ binaries on PATH. Never connects to DATABASE_URL or a
running customer/dev database. AWS is mocked; only local fixture data is used.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import asyncpg
import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app import db
from app.filters import FilterSet
from app.routers import inference_profiles as profile_api
from app.routers import latency, model_insights, overview, quota_drilldown
from ingestion import inference_profiles as ip
from test_inference_profiles import ACCOUNT, MODEL, REGION, profile

TODAY = datetime.now(timezone.utc).date()
MIGRATION = ROOT / "db/migrations/013_application_inference_profiles.sql"


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    for command in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(command):
            pytest.skip(f"local PostgreSQL binary unavailable: {command}")
    base = tmp_path_factory.mktemp("lens-aip-pg")
    data = base / "data"
    # macOS's default temporary directory can exceed the Unix socket path limit.
    socket_dir = tempfile.TemporaryDirectory(prefix="lens-aip-", dir="/tmp")
    sock = Path(socket_dir.name)
    result = subprocess.run(
        ["initdb", "-D", str(data), "-U", "lens_aip_test", "--no-locale",
         "--encoding=UTF8", "--auth-local=trust", "--auth-host=reject", "--no-sync"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    options = f"-F -k {shlex.quote(str(sock))} -p 55473 -h ''"
    result = subprocess.run(
        ["pg_ctl", "-D", str(data), "-l", str(base / "server.log"),
         "-o", options, "-w", "start"], capture_output=True, text=True)
    assert result.returncode == 0, (base / "server.log").read_text()
    try:
        for sql in [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]:
            result = subprocess.run(
                ["psql", "-h", str(sock), "-p", "55473", "-U", "lens_aip_test",
                 "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", str(sql)],
                capture_output=True, text=True)
            assert result.returncode == 0, f"{sql.name}: {result.stderr}"
        yield {"host": str(sock), "port": 55473, "user": "lens_aip_test",
               "database": "postgres"}
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"],
                       capture_output=True, check=True)
        socket_dir.cleanup()


@pytest.fixture
async def conn(postgres, monkeypatch):
    connection = await asyncpg.connect(**postgres)
    tx = connection.transaction()
    await tx.start()
    # Exercise the same search path as the API's pool; fixture writes below
    # name public explicitly, just as the ingestion session's default does.
    await connection.execute("SET LOCAL search_path = lens_read, public")
    monkeypatch.setattr(db, "fetch", connection.fetch)
    monkeypatch.setattr(db, "fetchrow", connection.fetchrow)
    monkeypatch.setattr(db, "fetchval", connection.fetchval)
    try:
        yield connection
    finally:
        await tx.rollback()
        await connection.close()


async def catalog(conn, *profiles):
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": list(profiles)}
    await ip.refresh_region(conn, client, ACCOUNT, REGION)
    return client


async def daily(conn, mid, requests, *, account=ACCOUNT, region=REGION, endpoint="runtime"):
    await conn.execute(
        """INSERT INTO public.f_daily
             (event_date, accountid, modelid, region, endpoint, total_requests,
              successful_requests, failed_requests, total_input_tokens, total_output_tokens)
           VALUES ($1,$2,$3,$4,$5,$6::bigint,$6::bigint,0,$6::bigint*10,$6::bigint*2)
           ON CONFLICT (event_date,accountid,modelid,region,operation,
                        traffic_type,service_tier,inference_profile_prefix,endpoint)
           DO UPDATE SET total_requests=EXCLUDED.total_requests""",
        TODAY, account, mid, region, endpoint, requests)


async def hourly(conn, mid, hour, requests, native=0):
    await conn.execute(
        """INSERT INTO public.f_hourly_peak
           (event_date,hour,accountid,modelid,region,total_requests,
            total_input_tokens,total_output_tokens,estimated_tpm_quota_usage)
           VALUES ($1,$2,$3,$4,$5,$6::bigint,$6::bigint*10,$6::bigint*2,$7)""",
        TODAY, hour, ACCOUNT, mid, REGION, requests, native)


def filters():
    return FilterSet(start=TODAY, end=TODAY, accounts=(ACCOUNT,))


async def test_cached_model_rollup_preserves_raw_rows_and_replay(conn):
    a, b = profile(), profile("bbbbbb123456")
    await catalog(conn, a, b)
    await daily(conn, MODEL, 10)
    await daily(conn, a["inferenceProfileArn"], 20)
    await daily(conn, b["inferenceProfileId"], 30)
    raw = await conn.fetch("SELECT * FROM public.f_daily ORDER BY modelid")
    summary = await overview.summary(filters())
    models = await overview.requests_by_model(filters())
    assert len(models) == 1
    assert models[0]["modelid"] == MODEL and models[0]["total_requests"] == 60
    await daily(conn, a["inferenceProfileArn"], 20)  # idempotent CW replay
    await catalog(conn, a, b)  # idempotent profile refresh
    assert await overview.summary(filters()) == summary
    assert await conn.fetch("SELECT * FROM public.f_daily ORDER BY modelid") == raw
    assert await conn.fetchval("SELECT COUNT(*) FROM public.dim_inference_profiles") == 2


async def test_accounts_regions_and_endpoints_cannot_borrow_a_mapping(conn):
    a = profile()
    await catalog(conn, a)
    mid = a["inferenceProfileId"]
    await daily(conn, mid, 10)
    await daily(conn, mid, 20, account="222222222222")
    await daily(conn, mid, 30, region="eu-west-1")
    await daily(conn, mid, 40, endpoint="mantle")
    rows = await conn.fetch("SELECT accountid,region,endpoint,modelid,total_requests FROM f_daily")
    mapped = [r for r in rows if r["modelid"] == MODEL]
    assert len(mapped) == 1 and mapped[0]["total_requests"] == 10
    assert sum(r["total_requests"] for r in rows) == 100


async def test_history_resolves_after_discovery_and_survives_deletion_and_denial(conn):
    a = profile()
    await daily(conn, a["inferenceProfileArn"], 7)
    assert await conn.fetchval("SELECT modelid FROM f_daily") == a["inferenceProfileArn"]
    client = await catalog(conn, a)
    assert await conn.fetchval("SELECT modelid FROM f_daily") == MODEL
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    client.get_inference_profile.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetInferenceProfile")
    await ip.refresh_region(conn, client, ACCOUNT, REGION)
    assert await conn.fetchval("SELECT modelid FROM f_daily") == MODEL
    assert await conn.fetchval("SELECT api_visible FROM public.dim_inference_profiles") is False
    before = await conn.fetch("SELECT * FROM public.dim_inference_profiles")
    client.list_inference_profiles.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListInferenceProfiles")
    with pytest.raises(ClientError):
        await ip.refresh_region(conn, client, ACCOUNT, REGION)
    assert await conn.fetch("SELECT * FROM public.dim_inference_profiles") == before


async def test_partial_pagination_does_not_hide_or_overwrite_cached_profiles(conn):
    a = profile()
    client = await catalog(conn, a)
    before = await conn.fetch("SELECT * FROM public.dim_inference_profiles")
    client.list_inference_profiles.side_effect = [
        {"inferenceProfileSummaries": [profile("bbbbbb123456")], "nextToken": "page2"},
        ClientError({"Error": {"Code": "ThrottlingException"}}, "ListInferenceProfiles"),
    ]
    with pytest.raises(ClientError):
        await ip.refresh_region(conn, client, ACCOUNT, REGION)
    assert await conn.fetch("SELECT * FROM public.dim_inference_profiles") == before


async def test_hourly_sum_precedes_peak_and_partial_native_sum_stays_unknown(conn):
    a, b = profile(), profile("bbbbbb123456")
    await catalog(conn, a, b)
    # Peaks at different hours: 90 + 80 is NOT a real 170-request hour.
    await hourly(conn, a["inferenceProfileArn"], 9, 90, 900)
    await hourly(conn, b["inferenceProfileId"], 9, 10, 100)
    await hourly(conn, MODEL, 9, 5, 50)
    await hourly(conn, a["inferenceProfileArn"], 17, 5, 50)
    await hourly(conn, b["inferenceProfileId"], 17, 80, None)
    rows = await conn.fetch("SELECT * FROM f_hourly_peak ORDER BY hour")
    assert [r["total_requests"] for r in rows] == [105, 85]
    assert [r["estimated_tpm_quota_usage"] for r in rows] == [1050, None]
    assert all(r["has_application_profile"] for r in rows)
    result = await quota_drilldown.quota_drilldown(
        account_id=ACCOUNT, model_id=MODEL, region=REGION, days=1, endpoint="runtime")
    assert result["kpis"]["peak_rpm"] == 105 / 60
    assert result["tpm_limit"] is None


async def test_log_tags_and_proxy_identity_remain_separate_from_cw_totals(conn):
    a = profile()
    await catalog(conn, a)
    await daily(conn, MODEL, 10)  # CW already reported the foundation model
    await conn.execute(
        """INSERT INTO public.f_daily_tagged
           (event_date,accountid,modelid,region,tag_key,tag_value,total_requests)
           VALUES ($1,$2,$3,$4,'__all__','__all__',10),
                  ($1,$2,$3,$4,'team','support',10)""",
        TODAY, ACCOUNT, a["inferenceProfileArn"], REGION)
    await conn.execute(
        """INSERT INTO public.f_request_events
           (ts,event_date,modelid,region,accountid,request_id,dimensions)
           VALUES (now(),$1,$2,$3,$4,'request-1','{\"team\":\"support\"}')""",
        TODAY, a["inferenceProfileArn"], REGION, ACCOUNT)
    tagged = await conn.fetchrow(
        "SELECT * FROM f_daily_tagged WHERE tag_key='team'")
    assert tagged["modelid"] == MODEL
    assert tagged["application_profile_arn"] == a["inferenceProfileArn"]
    assert tagged["tag_value"] == "support"
    event = await conn.fetchrow("SELECT * FROM f_request_events")
    assert event["modelid"] == MODEL and event["request_id"] == "request-1"
    assert json.loads(event["dimensions"]) == {"team": "support"}
    assert event["invoked_model_id"] == a["inferenceProfileArn"]
    assert await conn.fetchval("SELECT SUM(total_requests) FROM f_daily") == 10


async def test_latency_preserves_sample_weights_and_percentile_bounds(conn):
    a = profile()
    await catalog(conn, a)
    await conn.executemany(
        """INSERT INTO public.f_latency_daily
           (event_date,accountid,modelid,region,sample_count,avg_e2e,p50_e2e,
            ttft_sample_count,avg_ttft)
           VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8)""",
        [(TODAY, ACCOUNT, MODEL, REGION, 9900, 1.0, None, None),
         (TODAY, ACCOUNT, a["inferenceProfileArn"], REGION, 100, 10000.0, 100, 200.0)])
    rows = await latency.latency_by_model(filters())
    assert len(rows) == 1
    assert rows[0]["avg_e2e"] == pytest.approx(100.99)
    assert rows[0]["p50_e2e"] == 10000.0
    assert rows[0]["percentile_basis"] == "worst_bucket_upper_bound"
    assert rows[0]["avg_ttft"] == 200.0
    assert rows[0]["ttft_sample_count"] == 100
    # The traffic breakdowns also combine profile/direct buckets and must
    # follow the same statistics contract as the main latency table.
    split = await latency.latency_cris_vs_od(filters())
    assert split[0]["p50_e2e"] == 10000.0
    assert split[0]["avg_ttft"] == 200.0
    assert split[0]["ttft_sample_count"] == 100
    traffic = await latency.operation_latency(filters())
    assert traffic[0]["p50_e2e"] == 10000.0
    assert traffic[0]["percentile_basis"] == "worst_bucket_upper_bound"


async def test_mapping_api_paginates_and_migration_is_idempotent(conn):
    a, b = profile(), profile("bbbbbb123456")
    await catalog(conn, a, b)
    await daily(conn, a["inferenceProfileArn"], 5)
    before = await conn.fetch("SELECT * FROM public.f_daily")
    await conn.execute(MIGRATION.read_text())
    page = await profile_api.inference_profiles(accounts=[ACCOUNT], region=REGION, limit=1, offset=0)
    assert len(page["profiles"]) == 1 and page["next_offset"] == 1
    assert page["profiles"][0]["modelId"] == MODEL
    assert page["profiles"][0]["quota_routing_known"] is False
    assert await conn.fetch("SELECT * FROM public.f_daily") == before
    assert await conn.fetchval("SELECT modelid FROM f_daily") == MODEL
