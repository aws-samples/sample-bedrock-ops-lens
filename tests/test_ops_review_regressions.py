"""Pure and mocked regressions for the reviewed Ops Review changes."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT), str(ROOT / "tests")]

from app.ops_review import caching
from app import rate_catalog
from app.routers import ops_review
from app.filters import FilterSet
from test_cw_accounting import _run, _daily

MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
CAP = caching.load_catalog().lookup(MODEL)


def metrics(**overrides):
    return replace(caching.ModelCacheMetrics(
        model_id=MODEL, requests=10000, runtime_requests=10000,
        runtime_rows=1, input_tokens=20000000,
    ), **overrides)


def test_a_read_is_evidence_even_when_every_rows_write_metric_is_missing():
    m = metrics(cache_read_tokens=7, rows_missing_cache=1)
    assert caching.usage_status(m) == "in_use"
    assert caching.cached_share_pct(m, CAP) is None
    assert caching.advise(m, CAP)[0] == "in_use"


def test_missing_input_prevents_an_enablement_recommendation():
    m = metrics(rows_missing_input=1)
    assert caching.advise(m, CAP)[0] == "metrics_unavailable"


def test_invalid_rows_cannot_be_hidden_by_positive_aggregate_totals():
    m = metrics(cache_read_tokens=7, invalid_rows=1)
    assert caching.usage_status(m) == "unknown"
    assert caching.cached_share_pct(m, CAP) is None
    assert caching.advise(m, CAP)[0] == "metrics_unavailable"


@pytest.mark.parametrize("model_text", [
    '```python\nrequest = {"cache_control": {"type": "ephemeral"}}\n```\n',
    '````markdown\n## Prompt caching\nEnable on every model for 90% savings.\n````\n',
    '~~~mermaid\nflowchart LR\nA["Enable prompt caching for 90% savings"]\n~~~\n',
    'Prompt caching\n--------------\nEnable on every model for 90% savings.\n',
    'Prompt caching on this model\nsaves 90% of all costs.\n',
])
def test_caching_advice_cannot_survive_in_fences_headings_or_wrapped_paragraphs(model_text):
    md = "## Findings\nThrottle rate is 7%.\n\n" + model_text + "\n## Next steps\nRequest a quota review.\n"
    out = caching.strip_caching_content(md)
    assert "cache_control" not in out and "90%" not in out
    assert "Throttle rate is 7%." in out
    assert "Request a quota review." in out
    assert caching.strip_caching_content(out) == out


def test_unrelated_code_and_diagrams_survive_unchanged():
    code = '```text\n5. Example list entry\n## Recommendations\n```\n'
    diagram = '```mermaid\nflowchart LR\nA["Client"] --> B["Runtime"]\n```\n'
    clean = caching.strip_caching_content("## Findings\n\n" + code + "\n" + diagram)
    assert code in clean and diagram in clean
    result = caching.insert_section(clean, "## Prompt caching\nReviewed data only.\n")
    assert code in result and diagram in result


def findings():
    return {
        "window": {"start": "2026-10-01", "end": "2026-10-07"},
        "account_ids": ["111111111111"], "account_count": 1,
        "summary": {"total_requests": 10000, "throttled": 100},
        "capacity_health": [
            {"accountId": "111111111111", "modelId": MODEL, "region": "us-east-1",
             "throttle_pct": 1, "severity": "warning"},
        ],
        "recommended_actions": [],
        "prompt_caching": caching.evaluate_models([metrics()]),
    }


@pytest.mark.parametrize("change", [
    lambda f: f["summary"].update(throttled=200),
    lambda f: f["capacity_health"][0].update(throttle_pct=4),
    lambda f: f["capacity_health"][0].update(modelId="amazon.nova-pro-v1:0"),
    lambda f: f.update(account_ids=["222222222222"]),
])
def test_report_cache_changes_when_findings_change_without_changing_counts(change):
    first = findings()
    second = deepcopy(first)
    change(second)
    assert ops_review._findings_cache_key(first) != ops_review._findings_cache_key(second)


def test_cache_metrics_are_fresh_without_invalidating_an_identical_non_caching_report():
    first = findings()
    second = deepcopy(first)
    second["prompt_caching"] = caching.evaluate_models([metrics(cache_read_tokens=5)])
    assert ops_review._findings_cache_key(first) == ops_review._findings_cache_key(second)
    assert "5 tokens read from cache" in ops_review._assemble_report("## Findings\nStable.\n", second)


async def test_report_path_reuses_only_identical_findings_and_strips_conflicting_code(monkeypatch):
    from datetime import date
    current = findings()
    monkeypatch.setattr(ops_review, "ops_review_findings", AsyncMock(side_effect=lambda _: deepcopy(current)))
    monkeypatch.setattr(ops_review, "_NARRATIVE_CACHE", {})
    calls = []

    def writer(prompt):
        calls.append(prompt)
        return {"content": [{"type": "text", "text":
            '## Findings\nThrottle rate is measured.\n\n```python\ncache_control = "enable"\n```\n'}]}

    monkeypatch.setattr(ops_review, "_synthesize_via_runtime", writer)
    monkeypatch.setattr(ops_review, "_synthesize_via_mantle", writer)
    window = FilterSet(start=date(2026, 10, 1), end=date(2026, 10, 7))
    first = await ops_review.ops_review_synthesize(window)
    assert "cache_control =" not in first["narrative"]
    current["prompt_caching"] = caching.evaluate_models([metrics(cache_read_tokens=7)])
    second = await ops_review.ops_review_synthesize(window)
    assert second["cached"] is True and len(calls) == 1
    assert "7 tokens read from cache" in second["narrative"]
    current["capacity_health"][0]["throttle_pct"] = 8
    third = await ops_review.ops_review_synthesize(window)
    assert third["cached"] is False and len(calls) == 2
    monkeypatch.setattr(ops_review, "_NARRATIVE_CACHE_LIMIT", 2)
    current["capacity_health"][0]["throttle_pct"] = 9
    await ops_review.ops_review_synthesize(window)
    assert len(ops_review._NARRATIVE_CACHE) == 2
    assert ops_review._findings_cache_key(current) in ops_review._NARRATIVE_CACHE


async def test_collector_payload_preserves_absent_cache_metrics():
    conn = await _run({"Invocations": 10, "InputTokenCount": 5000})
    row = _daily(conn)
    assert row[12] == 5000
    assert row[14] is None and row[15] is None
    conn = await _run({"Invocations": 10, "InputTokenCount": 5000,
                       "CacheReadInputTokenCount": 0, "CacheWriteInputTokenCount": 0})
    assert _daily(conn)[14:16] == (0, 0)


async def test_collector_payload_keeps_positive_reads_when_writes_are_absent():
    conn = await _run({"Invocations": 10, "CacheReadInputTokenCount": 7})
    row = _daily(conn)
    assert row[12] is None and row[14] == 7 and row[15] is None


async def test_report_burndown_uses_observation_dates_and_scoped_queries(monkeypatch):
    from datetime import date
    import re
    calls = []
    key = {"accountid": "111111111111", "modelid": MODEL, "region": "us-east-1"}

    async def fetch(query, *args):
        calls.append((query, args))
        if "AS avg_input" in query and "HAVING SUM(total_requests) >= 100\n" in query:
            return [{**key, "avg_input": 60, "avg_output": 60}]
        if "AS input_quota_tokens" in query:
            return [
                {**key, "event_date": date(2026, 10, 1),
                 "input_quota_tokens": 600, "total_output_tokens": 600},
                {**key, "event_date": date(2026, 10, 2),
                 "input_quota_tokens": 0, "total_output_tokens": 600},
            ]
        return []

    catalog = rate_catalog.Catalog(entries=(
        {"all_of": ["sonnet45"], "rate": 5},
        {"all_of": ["sonnet45"], "rate": 10, "effective_from": "2026-10-02"},
    ))
    monkeypatch.setattr(ops_review.db, "fetch", fetch)
    monkeypatch.setattr(ops_review.db, "fetchrow", AsyncMock(return_value={}))
    monkeypatch.setattr(ops_review.rate_catalog, "snapshot", AsyncMock(return_value=catalog))
    monkeypatch.setattr(ops_review, "_load_lifecycle", AsyncMock(return_value={}))
    f = FilterSet(start=date(2026, 10, 1), end=date(2026, 10, 2),
                  endpoint="runtime", region="us-east-1", accounts={"111111111111"})
    result = await ops_review.ops_review_findings(f)
    [risk] = result["burndown_risk"]
    assert risk["busiest_hour_avg_effective_tpm"] == 100
    assert risk["burndown_rate"] == 10
    for query, args in calls:
        assert max(map(int, re.findall(r"\$(\d+)", query)), default=0) == len(args)
        if "AS input_quota_tokens" in query:
            assert "AND endpoint = 'runtime'" in query
            assert "runtime" in args and "us-east-1" in args and ["111111111111"] in args
        if "AS busiest_hour_avg_rpm" in query:
            assert args[-1] == "runtime"
            assert f"h.endpoint = ${len(args)}" in query
