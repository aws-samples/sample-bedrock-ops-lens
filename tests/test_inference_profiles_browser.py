"""Opt-in Playwright check against the real API and a disposable PostgreSQL DB.

Run `cd frontend && npm ci && npm run build` first, then from the repo root:
LENS_AIP_BROWSER=1 .venv/bin/python -m pytest tests/test_inference_profiles_browser.py -q
Requires local PostgreSQL binaries and Playwright's Chromium.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from test_inference_profiles_db import (
    ACCOUNT, MODEL, REGION, ROOT, TODAY, catalog, daily, hourly, postgres, profile,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LENS_AIP_BROWSER") != "1", reason="set LENS_AIP_BROWSER=1 to run Playwright")


async def seed_browser(conn):
    a = profile(destinations=(REGION, "us-west-2"))
    b = profile("bbbbbb123456")
    a["inferenceProfileName"] = "Support assistant"
    b["inferenceProfileName"] = "Analytics assistant"
    b["models"] = b["models"][:1]
    await catalog(conn, a, b)
    await conn.execute(
        "UPDATE public.dim_inference_profiles SET api_visible=FALSE WHERE profile_id=$1",
        b["inferenceProfileId"])
    unknown = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/dddddd123456"
    for mid, count in [(MODEL, 10), (a["inferenceProfileArn"], 20),
                       (b["inferenceProfileId"], 30), ("amazon.nova-pro-v1:0", 40),
                       (unknown, 5)]:
        await daily(conn, mid, count)
        await hourly(conn, mid, 9, count, count * 100)
    await conn.execute(
        "INSERT INTO public.dim_account (accountid,account_name) VALUES ($1,'Profile test account')",
        ACCOUNT)
    await conn.execute(
        """INSERT INTO public.ingestion_meta (key,value)
           VALUES ('last_cw_metrics_refresh',$1)""", datetime.now(timezone.utc).isoformat())
    for mid, count, name in [(a["inferenceProfileArn"], 20, "Support"),
                             (b["inferenceProfileId"], 30, "Analytics")]:
        for tag_key, tag_value in [("__all__", "__all__"), ("workload", name)]:
            await conn.execute(
                """INSERT INTO public.f_daily_tagged
                     (event_date,accountid,modelid,region,tag_key,tag_value,total_requests)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                TODAY, ACCOUNT, mid, REGION, tag_key, tag_value, count)
        await conn.execute(
            """INSERT INTO public.dim_tags
                 (tag_key,tag_value,first_seen,last_seen,total_requests_30d)
               VALUES ('workload',$1,$2,$2,$3)""", name, TODAY, count)
    for code, model_name in [("sonnet", "Claude Sonnet 4.5"), ("nova", "Amazon Nova Pro")]:
        for metric, value in [("TPM", 10000), ("RPM", 1000)]:
            await conn.execute(
                """INSERT INTO public.f_quotas
                     (accountid,region,quota_code,quota_name,model_name,traffic_type,
                      metric,applied_value,default_value)
                   VALUES ($1,$2,$3,$4,$5,'On-demand',$6,$7,$7)""",
                ACCOUNT, REGION, f"{code}-{metric}", f"{model_name} {metric}",
                model_name, metric, value)
    await conn.execute(
        """INSERT INTO public.dim_model_lifecycle
             (modelid,region,status,model_name,provider,legacy_time,end_of_life_time)
           VALUES ($1,$2,'LEGACY','Claude Sonnet 4.5','Anthropic',$3,$4)""",
        MODEL, REGION, datetime.now(timezone.utc) - timedelta(days=30),
        datetime.now(timezone.utc) + timedelta(days=90))
    for mid, count, avg in [(MODEL, 10, 50), (a["inferenceProfileArn"], 20, 10),
                            (b["inferenceProfileId"], 30, 100)]:
        await conn.execute(
            """INSERT INTO public.f_latency_daily
                 (event_date,accountid,modelid,region,sample_count,
                  avg_e2e,p50_e2e,p90_e2e,p99_e2e)
               VALUES ($1,$2,$3,$4,$5,$6,$6,$6,$6)""",
            TODAY, ACCOUNT, mid, REGION, count, avg)


async def test_profile_browser(postgres, tmp_path):
    assert (ROOT / "frontend/dist/index.html").exists(), "Build the frontend first"
    conn = await asyncpg.connect(**postgres)
    try:
        await seed_browser(conn)
    finally:
        await conn.close()
    # Pass a bound socket to uvicorn to avoid racing another local dev server.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG")}
    env.update(
        DATABASE_URL=f"postgresql://lens_aip_test@/postgres?{urllib.parse.urlencode({'host': postgres['host'], 'port': postgres['port']})}",
        AUTH_ENABLED="false", CACHE_TTL_SECONDS="0", TZ="UTC",
        AWS_EC2_METADATA_DISABLED="true", AWS_ACCESS_KEY_ID="testing",
        AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION=REGION,
        PYTHONPATH=str(ROOT / "backend"),
        LENS_AIP_BASE_URL=f"http://127.0.0.1:{port}",
    )
    app_code = (
        "from app.main import app\n"
        "from fastapi.staticfiles import StaticFiles\n"
        "import uvicorn\n"
        f"app.mount('/', StaticFiles(directory={str(ROOT / 'frontend/dist')!r}, html=True))\n"
        f"uvicorn.run(app, fd={listener.fileno()}, log_level='warning')\n"
    )
    log_path = tmp_path / "api.log"
    with log_path.open("w") as log:
        server = subprocess.Popen(
            [sys.executable, "-c", app_code], cwd=ROOT, env=env,
            stdout=log, stderr=log, pass_fds=(listener.fileno(),))
        listener.close()
        try:
            for _ in range(100):
                assert server.poll() is None, log_path.read_text()
                try:
                    with urllib.request.urlopen(
                        env["LENS_AIP_BASE_URL"] + "/api/summary", timeout=1
                    ) as response:
                        assert json.load(response)["total_requests"] == 105
                    break
                except urllib.error.URLError:
                    time.sleep(0.1)
            else:
                pytest.fail("Local API did not start: " + log_path.read_text())
            result = subprocess.run(
                ["node", "node_modules/@playwright/test/cli.js", "test",
                 "inference-profiles.spec.js", "--workers=1"],
                cwd=ROOT / "frontend", env=env, capture_output=True, text=True, timeout=180)
            assert result.returncode == 0, result.stdout + result.stderr + log_path.read_text()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
