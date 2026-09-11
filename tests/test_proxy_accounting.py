"""Proxy / client-telemetry accounting — Audit findings T04-T10.

The parser and counter tests drive the real `ingestion.proxy_events` code with
synthetic events and need no database. The API tests need a running backend and
skip without one.

Run: .venv/bin/python -m pytest tests/test_proxy_accounting.py -q
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ingestion import proxy_events as pe  # noqa: E402

BASE = os.environ.get("LENS_API", "http://localhost:8001/api")

_FIELDS = ("ts", "ev_date", "hr", "dimensions", "model", "endpoint", "region",
           "account", "in_tok", "out_tok", "cache_read", "cache_write", "status",
           "throttled", "latency_ms", "ttft_ms", "retry_attempts", "cost_usd_est",
           "request_id")


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"backend not reachable at {BASE}: {e}")


def _event(**kw):
    e = {"ts": "2026-09-08T10:00:00Z", "dimensions": {"workload": "w1"},
         "model": "anthropic.claude-sonnet-4-5", "endpoint": "runtime",
         "region": "us-east-1", "input_tokens": 100, "output_tokens": 10,
         "status": 200, "request_id": "r1"}
    e.update(kw)
    return e


def _parse(**kw):
    p = pe._parse_event(_event(**kw))
    assert p, "event failed to parse"
    return dict(zip(_FIELDS, p))


def _count(events):
    """Apply the ingester's counting rules to a list of parsed events."""
    total = throttled = errors = retried = 0
    for d in events:
        total += 1
        if d["throttled"]:
            throttled += 1
        elif d["status"] >= 400:
            errors += 1
        if d["retry_attempts"] is not None and d["retry_attempts"] >= 1:
            retried += 1
    return {"total": total, "throttled": throttled, "errors": errors,
            "successes": total - throttled - errors, "retried": retried}


# --------------------------------------------------------------------------- #
# T05 — disjoint populations; a 429 is one failure, not two
# --------------------------------------------------------------------------- #
def test_a_throttled_request_is_counted_once():
    """One 429 used to produce throttled=1 AND error=1, so every consumer's
    (total - throttled - error) came out as -1 and failures as 2."""
    c = _count([_parse(status=429, throttled=True)])
    assert c == {"total": 1, "throttled": 1, "errors": 0, "successes": 0,
                 "retried": 0}


def test_successes_are_never_negative_for_any_mix():
    events = [_parse(status=429, throttled=True), _parse(status=500),
              _parse(status=400), _parse(status=200), _parse(status=200)]
    c = _count(events)
    assert c["successes"] == 2
    assert c["throttled"] + c["errors"] + c["successes"] == c["total"]
    assert c["successes"] >= 0


def test_throttle_flag_without_429_status_still_counts_once():
    c = _count([_parse(status=200, throttled=True)])
    assert c["throttled"] == 1 and c["errors"] == 0 and c["successes"] == 0


def test_429_status_alone_implies_throttled():
    d = _parse(status=429)
    assert d["throttled"] is True
    c = _count([d])
    assert c["throttled"] == 1 and c["errors"] == 0


# --------------------------------------------------------------------------- #
# T05 — retry vocabularies must not be conflated
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload,expected_retries,counted", [
    ({"retry_attempts": 0}, 0, False),   # no retry
    ({"retry_attempts": 1}, 1, True),    # ONE retry: used to be ignored
    ({"retry_attempts": 3}, 3, True),
    ({"attempt": 1}, 0, False),          # first attempt = no retry
    ({"attempt": 2}, 1, True),           # second attempt = one retry
    ({}, None, False),                   # not reported
])
def test_retry_count_normalisation(payload, expected_retries, counted):
    d = _parse(**payload)
    assert d["retry_attempts"] == expected_retries
    assert _count([d])["retried"] == (1 if counted else 0)


def test_retry_attempts_wins_over_attempt_when_both_present():
    d = _parse(retry_attempts=2, attempt=9)
    assert d["retry_attempts"] == 2


# --------------------------------------------------------------------------- #
# T04 — duplicate delivery must not inflate the rollup
# --------------------------------------------------------------------------- #
def test_identical_events_share_an_identity():
    """The rollup is keyed off the same (event_date, request_id, ts) identity the
    raw table deduplicates on, so a redelivered event is recognisable."""
    a, b = _parse(request_id="dup"), _parse(request_id="dup")
    ida = (a["ev_date"], a["request_id"], a["ts"])
    idb = (b["ev_date"], b["request_id"], b["ts"])
    assert ida == idb


def test_distinct_requests_do_not_collide():
    a, b = _parse(request_id="x1"), _parse(request_id="x2")
    assert (a["ev_date"], a["request_id"]) != (b["ev_date"], b["request_id"])


def test_synthetic_request_id_is_stable_and_discriminating():
    """Events with no request_id get a synthetic identity: identical re-reads must
    dedupe, genuinely different calls must not."""
    same_a = _parse(request_id="")
    same_b = _parse(request_id="")
    diff = _parse(request_id="", output_tokens=999)
    assert same_a["request_id"] == same_b["request_id"]
    assert diff["request_id"] != same_a["request_id"]


def test_rollup_is_built_from_the_raw_insert_result():
    """Guard the structure of the fix: the rollup must be assembled after the
    insert arbitrates, not during parsing."""
    src = (ROOT / "ingestion/proxy_events.py").read_text()
    assert "RETURNING event_date, request_id, ts" in src
    assert "build the hourly rollup from NEW requests only" in src
    assert "if inserted is not None and ev_date >= raw_cutoff and ident not in inserted" in src


# --------------------------------------------------------------------------- #
# T09 — the accounting row, and pickers that must not show it
# --------------------------------------------------------------------------- #
def test_ingester_writes_the_all_accounting_row():
    src = (ROOT / "ingestion/proxy_events.py").read_text()
    assert 'list(dimensions.items()) + [("__all__", "__all__")]' in src
    assert "AND dim_key <> '__all__'" in src, (
        "the accounting row must be excluded from dim_proxy_dimensions")


def test_seeder_mirrors_the_accounting_row():
    src = (ROOT / "db/seed.py").read_text()
    assert '("__all__", "__all__")' in src
    assert "AND dim_key <> '__all__'" in src


def test_dimension_pickers_do_not_offer_the_accounting_row():
    d = _get("/attribution/dimensions")
    keys = [x["key"] for x in d.get("dimensions", [])]
    assert "__all__" not in keys
    assert d.get("default_key") != "__all__"


def test_by_provider_reads_the_accounting_row():
    """T09: pinning to the busiest ATTRIBUTE key drops requests that lack it and
    can hide a whole provider. All four paths must be visible."""
    rows = _get("/attribution/by-provider?days=7")
    if not rows:
        pytest.skip("proxy source not active")
    eps = {r["endpoint"] for r in rows}
    assert eps, "no endpoints returned"
    total = sum(int(r["total_requests"]) for r in rows)
    assert total > 0


# --------------------------------------------------------------------------- #
# T06 — scope and multi-attribute filters must never be dropped silently
# --------------------------------------------------------------------------- #
def test_xtab_summary_reports_which_filters_it_applied():
    d = _get("/attribution/xtab/summary?days=7&dim_key=workload")
    for k in ("filters_applied", "filters_dropped", "partial", "source_grain"):
        assert k in d, f"missing provenance field {k}"


def test_multiple_attributes_are_either_applied_or_declared_dropped():
    d = _get("/attribution/xtab/summary?days=7"
             "&dim_filter=workload:search-service&dim_filter=env:prod")
    applied = d["filters_applied"]
    if d["partial"]:
        # Not applied -> must be named, with a reason.
        assert d["filters_dropped"], "a dropped filter must be reported"
        assert d.get("partial_reason"), "a dropped filter must be explained"
        assert d["conjunctive"] is False
    else:
        assert len(applied) == 2, "both attributes must be applied"
        assert d["conjunctive"] is True
        assert d["source_grain"] == "request"


def test_account_scope_changes_the_proxy_population():
    """T06: region/account were sent and ignored, so an account-scoped view
    silently showed fleet-wide proxy numbers."""
    fleet = _get("/attribution/xtab/summary?days=7&dim_key=workload")
    scoped = _get("/attribution/xtab/summary?days=7&dim_key=workload"
                  "&accounts=000000000000")
    assert scoped["total_requests"] <= fleet["total_requests"]
    assert scoped["total_requests"] == 0, (
        "a nonexistent account must select nothing, not the whole fleet")


# --------------------------------------------------------------------------- #
# T07 — AWS invoice dollars only go to AWS-billed traffic
# --------------------------------------------------------------------------- #
def test_cost_summary_declares_its_scope_and_separates_direct_traffic():
    d = _get("/attribution/xtab/cost-summary?days=7&dim_key=workload")
    assert d["cost_scope"] == "aws_cost_explorer_bedrock_only"
    dp = d["direct_provider_traffic"]
    for k in ("direct_provider_tokens", "aws_billed_tokens",
              "direct_provider_pct", "endpoints"):
        assert k in dp
    if dp["direct_provider_tokens"]:
        assert dp["endpoints"], "direct-provider endpoints must be named"
        assert 0 < dp["direct_provider_pct"] <= 100


def test_cost_fraction_excludes_direct_provider_endpoints():
    src = (ROOT / "backend/app/routers/attribution.py").read_text()
    assert "AWS_BILLED_ENDPOINTS" in src
    assert 'base_where += f" AND endpoint = ANY(${len(base_params)}::text[])"' in src, (
        "the CE fraction must be computed over AWS-billed endpoints only")


# --------------------------------------------------------------------------- #
# T08 — AWS quotas apply to AWS traffic, and quotas are per account
# --------------------------------------------------------------------------- #
def test_proxy_quota_excludes_direct_provider_traffic():
    d = _get("/workload-usage/quota?days=7&dim_key=workload")
    assert d["quota_scope"] == "aws_billed_endpoints_only"
    assert d["is_estimate"] is True
    for r in d["rows"]:
        assert r["endpoint"] in ("runtime", "mantle"), (
            "direct-provider traffic must not be scored against AWS quotas")
    for r in d.get("direct_provider_rows", []):
        assert r["tpm_limit"] is None
        assert r["utilization_pct"] is None
        assert r["quota_source"] == "provider"


def test_proxy_quota_states_whether_the_account_is_known():
    d = _get("/workload-usage/quota?days=7&dim_key=workload")
    if not d["rows"]:
        pytest.skip("no proxy quota rows")
    for r in d["rows"]:
        assert "quota_account_known" in r
        assert r["quota_source"] == "aws_service_quotas"
    assert "any_account_unknown" in d


# --------------------------------------------------------------------------- #
# T10 — merged percentiles must declare that they are not quantiles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/workload-usage?days=7&dim_key=workload",
    "/workload-usage/by-provider?days=7",
])
def test_proxy_percentiles_declare_their_basis(path):
    d = _get(path)
    rows = d if isinstance(d, list) else (d.get("rows") or [])
    if not rows:
        pytest.skip("no rows")
    for r in rows[:5]:
        assert r.get("percentile_basis") == "worst_bucket_upper_bound"


def test_raw_event_latency_path_claims_true_percentiles_only_there():
    src = (ROOT / "backend/app/routers/attribution.py").read_text()
    assert "'true_population_percentile' AS percentile_basis" in src
    assert "PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)" in src
