"""Ops Review findings and report against real PostgreSQL views.

Requires PostgreSQL 15+ binaries on PATH (skipped otherwise). Never connects to
DATABASE_URL or a running database. The report-writing model is mocked: it
returns caching advice the deterministic section must replace.
"""
from __future__ import annotations

import pytest

from test_inference_profiles_db import (  # noqa: F401 - fixtures by name
    ACCOUNT, REGION, TODAY, conn, postgres,
)

from app.filters import FilterSet
from app.ops_review import caching
from app.routers import ops_review

OTHER = "222222222222"
SONNET = "anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA = "amazon.nova-lite-v1:0"
OSS = "openai.gpt-oss-120b-1:0"
HAIKU = "anthropic.claude-haiku-4-5-20251001-v1:0"
OPUS = "anthropic.claude-opus-4-8"
LLAMA = "meta.llama3-1-8b-instruct-v1:0"


async def row(conn, mid, requests, input_tokens, output_tokens, read, write, *,
              account=ACCOUNT, endpoint="runtime", throttled=0):
    await conn.execute(
        """INSERT INTO public.f_daily
             (event_date, accountid, modelid, region, endpoint, total_requests,
              successful_requests, failed_requests, total_input_tokens, total_output_tokens,
              total_cache_read_input_tokens, total_cache_write_input_tokens, status_429_count)
           VALUES ($1,$2,$3,$4,$5,$6,$6,0,$7,$8,$9,$10,$11)""",
        TODAY, account, mid, REGION, endpoint, requests, input_tokens, output_tokens,
        read, write, throttled)


@pytest.fixture
async def fleet(conn):
    await row(conn, SONNET, 20_000, 400_000_000, 4_000_000, 0, 0, throttled=1_500)
    await row(conn, NOVA, 30_000, 300_000_000, 3_000_000, 100_000_000, 1_000_000,
              account=OTHER)
    # Input-heavy (900:1) on a model whose documentation does not describe caching.
    await row(conn, OSS, 20_000, 900_000_000, 1_000_000, 0, 0)
    await row(conn, HAIKU, 5_000, 50_000_000, 500_000, None, None)       # not observed
    await row(conn, OPUS, 5_000, 80_000_000, 400_000, 0, 0, endpoint="mantle")
    await row(conn, LLAMA, 4_000, 8_000_000, 400_000, 0, 0)
    await conn.execute(
        "INSERT INTO public.dim_account (accountid, account_name) VALUES ($1, 'Payments prod')",
        ACCOUNT)
    return conn


def window():
    return FilterSet(start=TODAY, end=TODAY)


async def test_caching_rows_follow_documented_support_and_measured_use(fleet):
    findings = await ops_review.ops_review_findings(window())
    rows = {r["modelId"]: r for r in findings["prompt_caching"]["models"]}
    assert rows[SONNET]["advice"] == "evaluate"
    assert rows[NOVA]["advice"] == "in_use"
    assert rows[NOVA]["cached_share_pct"] == pytest.approx(100 * 100 / 401, abs=0.01)
    assert rows[OSS]["advice"] == "not_documented"
    assert rows[HAIKU]["advice"] == "metrics_unavailable"     # NULL is not zero
    assert rows[OPUS]["advice"] == "metrics_unavailable"      # mantle zeros are not zero
    assert rows[OPUS]["usage_text"].startswith("Unknown: bedrock-mantle")
    assert findings["prompt_caching"]["unreviewed_models"] == [LLAMA]
    assert findings["prompt_caching"]["policy_version"] == caching.POLICY_VERSION


async def test_input_heavy_shape_alone_never_recommends_caching(fleet):
    findings = await ops_review.ops_review_findings(window())
    oss_shape = next(r for r in findings["request_shape"] if r["modelId"] == OSS)
    assert oss_shape["ratio"] == 900.0
    assert "cach" not in oss_shape["note"].lower()
    assert all("cach" not in r["note"].lower() for r in findings["request_shape"])
    caching_actions = [a for a in findings["recommended_actions"]
                       if a.get("topic") == "prompt_caching"]
    assert len(caching_actions) == 1
    assert SONNET in caching_actions[0]["detail"] and OSS not in caching_actions[0]["detail"]
    assert all(e["type"] != "caching_gap" for e in findings["engagement_opportunities"])
    assert "90%" not in str(findings)


async def test_rows_carry_account_names_and_unknown_names_stay_unknown(fleet):
    findings = await ops_review.ops_review_findings(window())
    cap = next(r for r in findings["capacity_health"] if r["modelId"] == SONNET)
    assert cap["account_name"] == "Payments prod"
    shapes = {(r["accountId"], r["modelId"]): r for r in findings["request_shape"]}
    assert shapes[(ACCOUNT, OSS)]["account_name"] == "Payments prod"
    for r in findings["request_shape"] + findings["capacity_health"]:
        if r["accountId"] == OTHER:
            assert r["account_name"] is None


async def test_a_failed_name_lookup_keeps_every_metric(fleet, monkeypatch):
    real_fetch = ops_review.db.fetch

    async def broken(query, *args):
        if "dim_account" in query:
            raise RuntimeError("names unavailable")
        return await real_fetch(query, *args)
    monkeypatch.setattr(ops_review.db, "fetch", broken)
    findings = await ops_review.ops_review_findings(window())
    cap = next(r for r in findings["capacity_health"] if r["modelId"] == SONNET)
    assert cap["account_name"] is None and cap["throttled"] == 1_500


LLM_TEXT = """## Executive summary
Throttling on Claude Sonnet 4.5 is the top issue. Enable prompt caching on gpt-oss-120b to save 90%.

## Key findings
- Throttling: 7.5% for Payments prod (111111111111).
- Prompt caching: gpt-oss-120b has a 0% cache hit rate.

## Recommendations (priority-ordered)
1. **Enable prompt caching on gpt-oss-120b.**
   It is input-heavy.
2. **Request a quota increase.**
   Throttle rate 7.5%.

## Priority matrix
| Priority | Action | Effort | Impact | Cost direction |
|---|---|---|---|---|
| Low | Enable prompt caching | Small | Medium | ↓ cost |
| High | Request a quota increase | Small | Large | neutral |
"""


async def test_the_report_carries_the_deterministic_section_not_the_models_advice(
        fleet, monkeypatch):
    prompts = []

    def fake_runtime(prompt):
        prompts.append(prompt)
        return {"content": [{"type": "text", "text": LLM_TEXT}],
                "usage": {"input_tokens": 10, "output_tokens": 20}}
    # Whichever endpoint is primary, the model answers with the same text.
    monkeypatch.setattr(ops_review, "_synthesize_via_runtime", fake_runtime)
    monkeypatch.setattr(ops_review, "_synthesize_via_mantle", fake_runtime)
    monkeypatch.setattr(ops_review, "_NARRATIVE_CACHE", {})

    out = await ops_review.ops_review_synthesize(window(), force=True)
    report = out["narrative"]
    assert report.count("## Prompt caching") == 1
    section = report.split("## Prompt caching", 1)[1].split("## Recommendations", 1)[0]
    assert "gpt-oss-120b" in section and "No enablement recommendation" in section
    assert "AWS Doc:" in section
    outside = report.replace(section, "")
    assert not caching._CACHING.search(outside.replace("## Prompt caching", "")), outside
    assert "1. **Request a quota increase.**" in report
    assert "| High | Request a quota increase |" in report
    # The model never saw the caching findings.
    assert '"prompt_caching"' not in prompts[0]
    assert "Evaluate prompt caching" not in prompts[0]
    assert "Payments prod" in prompts[0]

    again = await ops_review.ops_review_synthesize(window())
    assert again["cached"] is True and again["narrative"] == report
    assert len(prompts) == 1


async def test_report_cache_keys_change_with_the_caching_policy(fleet, monkeypatch):
    findings = await ops_review.ops_review_findings(window())
    key = ops_review._findings_cache_key(findings)
    for name in ("POLICY_VERSION", "RENDER_VERSION"):
        with monkeypatch.context() as m:
            m.setattr(caching, name, "next")
            assert ops_review._findings_cache_key(findings) != key
    renamed = dict(findings, capacity_health=[
        dict(r, account_name="Renamed") for r in findings["capacity_health"]])
    assert ops_review._findings_cache_key(renamed) != key


async def test_a_cached_report_still_shows_current_cache_metrics(fleet, monkeypatch):
    """The model's text is cached; the caching section is not."""
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return {"content": [{"type": "text", "text": "## Executive summary\nFine.\n"}],
                "usage": {}}
    monkeypatch.setattr(ops_review, "_synthesize_via_runtime", fake)
    monkeypatch.setattr(ops_review, "_synthesize_via_mantle", fake)
    monkeypatch.setattr(ops_review, "_NARRATIVE_CACHE", {})
    first = await ops_review.ops_review_synthesize(window(), force=True)
    assert "Evaluate:" in first["narrative"]
    await fleet.execute(
        """UPDATE public.f_daily SET total_cache_read_input_tokens = 50000000
            WHERE modelid = $1""", SONNET)
    second = await ops_review.ops_review_synthesize(window())
    assert second["cached"] is True and len(calls) == 1
    assert "In use: 50,000,000 tokens read from cache" in second["narrative"]


async def test_partial_cache_counters_keep_positive_read_evidence(conn):
    await row(conn, SONNET, 10000, None, 100, 7, None)
    findings = await ops_review.ops_review_findings(window())
    [model] = findings["prompt_caching"]["models"]
    assert model["usage"] == "in_use"
    assert model["cached_share_pct"] is None
    assert model["advice"] == "in_use"
    assert findings["summary"]["total_output_tokens"] == 100


async def test_negative_cache_rows_cannot_cancel_into_valid_aggregate_metrics(conn):
    await row(conn, SONNET, 10000, 100000, 100, -20, 0)
    await row(conn, SONNET, 10000, 100000, 100, 30, 0, account=OTHER)
    findings = await ops_review.ops_review_findings(window())
    [model] = findings["prompt_caching"]["models"]
    assert model["usage"] == "unknown"
    assert model["cached_share_pct"] is None
    assert model["advice"] == "metrics_unavailable"


async def test_hourly_capacity_and_burndown_honor_the_endpoint_filter(conn):
    for endpoint, hourly_requests in (("runtime", 6000), ("mantle", 600000)):
        await row(conn, SONNET, 10000, 60000, 600000, 0, 0,
                  endpoint=endpoint, throttled=1000)
        await conn.execute(
            """INSERT INTO public.f_hourly_peak
                 (event_date, hour, accountid, modelid, region, endpoint,
                  total_requests, total_input_tokens, total_output_tokens)
               VALUES ($1,0,$2,$3,$4,$5,$6,60000,600000)""",
            TODAY, ACCOUNT, SONNET, REGION, endpoint, hourly_requests)
    runtime = await ops_review.ops_review_findings(
        FilterSet(start=TODAY, end=TODAY, endpoint="runtime"))
    mantle = await ops_review.ops_review_findings(
        FilterSet(start=TODAY, end=TODAY, endpoint="mantle"))
    assert runtime["capacity_health"][0]["busiest_hour_avg_rpm"] == 100
    assert mantle["capacity_health"][0]["busiest_hour_avg_rpm"] == 10000
    assert runtime["burndown_risk"]
    assert mantle["burndown_risk"] == []
