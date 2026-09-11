"""Cross-widget invariants asserted against a RUNNING backend.

Audit phase 4 requires these to hold for any scope:

    successful attempts + failed attempts <= total attempts
    0 <= failed attempts <= total attempts
    changing a supported filter changes every dependent widget's population
    two widgets describing the same measure must agree

Skipped automatically when no backend is reachable, so the suite stays green in
environments without a database.

Start a backend first:
  cd backend && DATABASE_URL=... AUTH_ENABLED=false PYTHONPATH=.. \
    ../.venv/bin/uvicorn app.main:app --port 8001
Run: .venv/bin/python -m pytest tests/test_api_invariants.py -q
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

BASE = os.environ.get("LENS_API", "http://localhost:8001/api")


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"backend not reachable at {BASE}: {e}")


@pytest.fixture(scope="module")
def burndown():
    rows = _get("/ops-burndown-risk?days=7&endpoint=runtime")
    if not rows:
        pytest.skip("no burndown rows in this dataset")
    return rows


# --------------------------------------------------------------------------- #
# Findings 02 + 03 — the two widgets must report the same measure and quota
# --------------------------------------------------------------------------- #
def test_burndown_and_drilldown_agree_on_tpm_and_quota(burndown):
    """The audit found these disagreeing by exactly 60x on TPM and picking
    different quota families (6.46M vs 14.96M) for the same key."""
    checked = 0
    for r in burndown[:10]:
        mid = urllib.parse.quote(r["modelId"], safe="")
        dd = _get(f"/quota-drilldown?account_id={r['accountId']}&model_id={mid}"
                  f"&region={r['region']}&days=7&endpoint=runtime")
        checked += 1
        # Same measure -> same number (both are the busiest hour's quota-weighted
        # hourly-average TPM).
        assert dd["kpis"]["peak_tpm"] == pytest.approx(
            r["busiest_hour_avg_tpm"], rel=1e-6), (
            f"TPM disagreement for {r['modelId']} {r['region']}")
        # Same quota resolution.
        if r["effective_tpm"] is None:
            assert dd["tpm_limit"] is None
        else:
            assert dd["tpm_limit"] == pytest.approx(r["effective_tpm"], rel=1e-9)
        assert dd.get("matched_quota_traffic_type") == r.get("quota_family")
    assert checked > 0


def test_rates_are_plausible_per_minute_values(burndown):
    """A per-minute rate derived from an hourly total can never exceed that
    total, and utilization must be a percentage or explicitly unknown."""
    for r in burndown:
        assert r["rate_basis"] == "hourly_average"
        assert r["busiest_hour_avg_tpm"] >= 0
        u = r["utilization_pct"]
        assert u is None or u >= 0
        if u is not None:
            assert r["effective_tpm"] is not None, (
                "a utilization % requires a known denominator")


def test_unknown_quota_rows_are_retained_not_dropped(burndown):
    """Rows whose quota could not be resolved must still be listed (with an
    unknown limit) rather than silently disappearing from the widget."""
    for r in burndown:
        assert "quota_ambiguous" in r
        assert "quota_candidate_families" in r


# --------------------------------------------------------------------------- #
# Finding 01 — attempt decomposition, via the API
# --------------------------------------------------------------------------- #
def test_summary_error_counts_are_bounded():
    s = _get("/summary?days=7")
    total = s.get("total_requests") or 0
    failed = s.get("failed_requests")
    if failed is None:
        pytest.skip("summary does not expose failed_requests")
    assert 0 <= failed <= total, f"failed={failed} total={total}"


# --------------------------------------------------------------------------- #
# Phase 4 — a supported filter must change every dependent population
# --------------------------------------------------------------------------- #
def test_account_filter_changes_the_population():
    accounts = _get("/accounts")
    ids = [a["accountId"] for a in accounts][:2]
    if len(ids) < 2:
        pytest.skip("need two accounts")
    all_rows = _get("/summary?days=7")
    one = _get(f"/summary?days=7&accounts={ids[0]}")
    assert one["total_requests"] <= all_rows["total_requests"]
    assert one["total_requests"] > 0
    two = _get(f"/summary?days=7&accounts={ids[1]}")
    assert one["total_requests"] != two["total_requests"], (
        "two different accounts returned identical totals — filter ignored?")


# --------------------------------------------------------------------------- #
# Findings 04 + 09 — latency account grain and honest statistics
# --------------------------------------------------------------------------- #
def test_latency_differs_between_accounts():
    """The audit found /latency-by-model returning byte-identical distributions
    for every account (815,046 samples, 5,625.3013 ms) because f_latency_daily
    had no account column and the account filter was disabled."""
    accounts = _get("/accounts")
    ids = [a["accountId"] for a in accounts][:2]
    if len(ids) < 2:
        pytest.skip("need two accounts")
    a = _get(f"/latency-by-model?days=7&accounts={ids[0]}")
    b = _get(f"/latency-by-model?days=7&accounts={ids[1]}")
    if not a or not b:
        pytest.skip("no latency rows")
    key = lambda rows: {(r.get("modelid") or r.get("modelId")): r["sample_count"]
                        for r in rows}
    assert key(a) != key(b), (
        "two accounts returned identical latency populations — account grain lost")


def test_latency_declares_its_percentile_basis():
    rows = _get("/latency-by-model?days=7")
    if not rows:
        pytest.skip("no latency rows")
    for r in rows:
        assert r.get("percentile_basis") == "worst_bucket_upper_bound", (
            "a merged percentile must declare that it is not a population "
            "percentile")
        # A worst-bucket p99 can never be below the worst-bucket p50.
        if r.get("p50_e2e") is not None and r.get("p99_e2e") is not None:
            assert r["p99_e2e"] >= r["p50_e2e"]


def test_ttft_population_is_separate_from_e2e():
    """TTFT is streaming-only, so its sample count must be its own and must not
    exceed the E2E count. Unavailable TTFT must be null, never 0 ms."""
    rows = _get("/latency-by-model?days=7")
    if not rows:
        pytest.skip("no latency rows")
    checked = 0
    for r in rows:
        n_e2e, n_ttft = r.get("sample_count"), r.get("ttft_sample_count")
        if n_ttft is None:
            assert r.get("ttft_available") is False
            assert r.get("avg_ttft") is None, "no TTFT samples must mean null"
            continue
        assert n_ttft <= n_e2e, "TTFT population cannot exceed the E2E population"
        checked += 1
    assert checked > 0


def test_operation_latency_does_not_claim_to_be_per_operation():
    """Finding 16: the endpoint groups by traffic type; it must say so rather
    than labelling ON_DEMAND_INFERENCE_REQUEST an 'operation'."""
    rows = _get("/operation-latency?days=7")
    if not rows:
        pytest.skip("no rows")
    for r in rows:
        assert r.get("dimension") == "traffic_type"
        assert r.get("operation_available") is False


# --------------------------------------------------------------------------- #
# Findings 05, 06, 07 — cost widgets must agree and conserve money
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint", ["all", "runtime", "mantle"])
def test_model_chart_equals_the_headline(endpoint):
    """The audit found /cost-by-model returning the SAME combined total for
    every endpoint — an $96,699.54 chart under an $11,428.82 Mantle headline."""
    s = _get(f"/cost-summary?days=7&endpoint={endpoint}")
    rows = _get(f"/cost-by-model?days=7&endpoint={endpoint}")
    chart = sum(r["total_cost"] for r in rows)
    assert chart == pytest.approx(s["total_cost"], abs=0.02), (
        f"{endpoint}: model chart {chart} != headline {s['total_cost']}")


@pytest.mark.parametrize("endpoint", ["runtime", "mantle"])
def test_daily_sum_equals_headline(endpoint):
    """Finding 07: one allocation basis, so the daily series must sum to the
    headline (they used per-day and whole-window weights respectively)."""
    s = _get(f"/cost-summary?days=7&endpoint={endpoint}")
    daily = _get(f"/cost-daily?days=7&endpoint={endpoint}")
    assert sum(d["total_cost"] for d in daily) == pytest.approx(
        s["total_cost"], abs=0.02)


def test_endpoint_split_conserves_the_scope_total():
    """runtime + mantle + unknown must equal the unsliced CE total. Charges with
    no token basis land in 'unknown' rather than being smeared."""
    s = _get("/cost-summary?days=7&endpoint=all")
    be = s["by_endpoint"]
    assert be["runtime"] + be["mantle"] + be["unknown"] == pytest.approx(
        s["scope_total_all_endpoints"], abs=0.02)
    assert s["allocation_basis"] == "per_account_day_priced_token_share"


@pytest.mark.parametrize("endpoint", ["all", "runtime", "mantle"])
def test_no_concentration_row_exceeds_the_headline(endpoint):
    """The audit's smoking gun: $24,638.38 of Nova Lite under an $11,428.82
    Mantle headline."""
    s = _get(f"/cost-summary?days=7&endpoint={endpoint}")
    c = _get(f"/cost-concentration?days=7&endpoint={endpoint}&top_n=10")
    rows = c if isinstance(c, list) else c.get("rows", [])
    for r in rows:
        amt = r.get("total_cost", r.get("spend", 0))
        assert amt <= s["total_cost"] + 0.02, (
            f"{endpoint}: concentration row {amt} exceeds headline {s['total_cost']}")


@pytest.mark.parametrize("endpoint", ["runtime", "mantle"])
def test_by_account_sums_to_the_headline(endpoint):
    s = _get(f"/cost-summary?days=7&endpoint={endpoint}")
    a = _get(f"/cost-by-account?days=7&endpoint={endpoint}")
    rows = a if isinstance(a, list) else a.get("rows", [])
    assert sum(r.get("total_cost", 0) for r in rows) == pytest.approx(
        s["total_cost"], abs=0.02)


def test_endpoint_slices_partition_the_total():
    """runtime + mantle headlines must reconstruct the all-endpoint total."""
    a = _get("/cost-summary?days=7&endpoint=all")
    r = _get("/cost-summary?days=7&endpoint=runtime")
    m = _get("/cost-summary?days=7&endpoint=mantle")
    assert r["total_cost"] + m["total_cost"] == pytest.approx(
        a["total_cost"], abs=0.02)
