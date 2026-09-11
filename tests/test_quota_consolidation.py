"""One quota resolver, and visible disclosure — Audit follow-up review.

Three regressions the follow-up review caught after the first pass:

  1. The Workloads UI calls /attribution/quota, which still had its own unfixed
     copy of the quota lookup. Both copies matched a quota row on ANY token of
     its model_name, so a Sonnet workload inherited a Haiku ceiling (200% instead
     of 10%), ignored the account, fell back to the region's smallest limit, and
     scored direct-provider traffic against AWS quotas.
  2. The burndown helper returned 1x for Claude Fable 5.1 and the GPT-5.6 SKUs,
     which the AWS doc puts at 10x - and a test enforced the wrong value.
  3. `filters_dropped` / `partial_reason` were returned as JSON and never
     rendered, so a partially applied filter looked fully applied.

Run: .venv/bin/python -m pytest tests/test_quota_consolidation.py -q
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

from app import proxy_quota as pq  # noqa: E402
from app.burndown import output_burndown_rate  # noqa: E402

BASE = os.environ.get("LENS_API", "http://localhost:8001/api")


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"backend not reachable at {BASE}: {e}")


def q(account, region, model_name, limit, traffic="On-demand"):
    return {"accountId": account, "region": region, "model_name": model_name,
            "traffic_type": traffic, "metric": "TPM", "applied_value": limit,
            "default_value": limit, "quota_code": f"L-{abs(hash(model_name)) % 9999}"}


ACCT_A, ACCT_B = "111111111111", "222222222222"
SONNET5 = "anthropic.claude-sonnet-5"

FIXTURE = [
    q(ACCT_A, "us-east-1", "Claude Sonnet 5", 1_000_000),
    q(ACCT_A, "us-east-1", "Claude Haiku 4.5", 50_000),
]


# --------------------------------------------------------------------------- #
# 1 — the resolver must not borrow another model's limit
# --------------------------------------------------------------------------- #
def test_a_sonnet_workload_uses_the_sonnet_limit():
    """The review's counterexample: 100,000 TPM of Sonnet against a 1,000,000
    Sonnet limit is 10%. Matching on ANY shared token ("claude") let the 50,000
    Haiku row win via min(), reporting 200%."""
    v = pq.resolve(FIXTURE, SONNET5, "us-east-1", ACCT_A)
    assert v.limit == 1_000_000
    assert round(100_000 / v.limit * 100, 1) == 10.0
    assert v.model_matched and v.account_known


def test_an_unmatched_model_reports_unknown_not_the_region_floor():
    """The old fallback was min(limit) across the region, so an unrecognised
    model silently inherited the smallest ceiling in scope."""
    v = pq.resolve(FIXTURE, "meta.llama3-1-70b-instruct-v1:0", "us-east-1", ACCT_A)
    assert v.limit is None
    assert v.model_matched is False


def test_a_different_region_is_not_a_match():
    v = pq.resolve(FIXTURE, SONNET5, "eu-west-1", ACCT_A)
    assert v.limit is None


def test_an_account_with_no_entry_is_unknown_not_another_accounts_limit():
    v = pq.resolve(FIXTURE, SONNET5, "us-east-1", ACCT_B)
    assert v.limit is None
    assert v.account_known is True      # the account was named...
    assert v.model_matched is False     # ...but it has no matching quota row


def test_unknown_account_accepts_a_unanimous_limit():
    """Most accounts sit on the published default, so when every candidate agrees
    the utilization is well defined even without an account on the event."""
    rows = FIXTURE + [q(ACCT_B, "us-east-1", "Claude Sonnet 5", 1_000_000)]
    v = pq.resolve(rows, SONNET5, "us-east-1", "__none__")
    assert v.limit == 1_000_000
    assert v.account_known is False


def test_unknown_account_with_disagreeing_limits_is_ambiguous_not_a_guess():
    rows = FIXTURE + [q(ACCT_B, "us-east-1", "Claude Sonnet 5", 2_000_000)]
    v = pq.resolve(rows, SONNET5, "us-east-1", "__none__")
    assert v.limit is None
    assert v.ambiguous is True


@pytest.mark.asyncio
async def test_scoring_separates_direct_provider_traffic(monkeypatch):
    """Direct-provider calls consume no AWS quota, so they must not be scored."""
    async def fake_rows():
        return FIXTURE
    monkeypatch.setattr(pq, "load_tpm_quota_rows", fake_rows)
    rows = [
        {"dim_value": "w1", "modelId": SONNET5, "endpoint": "runtime",
         "region": "us-east-1", "accountId": ACCT_A,
         "input_tokens": 6_000_000, "output_tokens": 0},
        {"dim_value": "w1", "modelId": "claude-sonnet-5", "endpoint": "anthropic-api",
         "region": "us-east-1", "accountId": ACCT_A,
         "input_tokens": 60_000_000, "output_tokens": 0},
    ]
    out = await pq.score(rows)
    assert out["quota_scope"] == "aws_billed_endpoints_only"
    assert [r["endpoint"] for r in out["rows"]] == ["runtime"]
    assert out["rows"][0]["utilization_pct"] == pytest.approx(10.0, abs=0.01)
    assert len(out["direct_provider_rows"]) == 1
    d = out["direct_provider_rows"][0]
    assert d["tpm_limit"] is None and d["utilization_pct"] is None
    assert d["quota_source"] == "provider"


@pytest.mark.asyncio
async def test_a_known_utilization_outranks_an_unknown_one(monkeypatch):
    """Picking the 'worst' row must not let an unknown-limit row displace a row
    that has a real utilization."""
    async def fake_rows():
        return FIXTURE
    monkeypatch.setattr(pq, "load_tpm_quota_rows", fake_rows)
    rows = [
        {"dim_value": "w1", "modelId": SONNET5, "endpoint": "runtime",
         "region": "us-east-1", "accountId": ACCT_A,
         "input_tokens": 6_000_000, "output_tokens": 0},
        {"dim_value": "w1", "modelId": "meta.llama3-70b", "endpoint": "runtime",
         "region": "us-east-1", "accountId": ACCT_A,
         "input_tokens": 600_000_000, "output_tokens": 0},
    ]
    out = await pq.score(rows)
    assert len(out["rows"]) == 1
    assert out["rows"][0]["utilization_pct"] is not None


def test_no_borrowing_code_survives_anywhere():
    """Guard both deleted copies: an ANY-token match or a region-min fallback
    must not come back."""
    for rel in ("backend/app/routers/attribution.py",
                "backend/app/routers/workload_usage.py",
                "backend/app/proxy_quota.py"):
        src = (ROOT / rel).read_text()
        assert "any(t in mid for t in toks)" not in src, rel
        assert "min(lim for _, lim in cands)" not in src, rel


def test_both_quota_endpoints_use_the_shared_resolver():
    for rel in ("backend/app/routers/attribution.py",
                "backend/app/routers/workload_usage.py"):
        src = (ROOT / rel).read_text()
        assert "proxy_quota.score(" in src, f"{rel} still has its own lookup"


def test_the_workloads_tab_endpoint_is_the_fixed_one():
    """The review's first finding: the UI called the endpoint that was never
    fixed. Whichever it calls must now route through the shared resolver."""
    ui = (ROOT / "frontend/src/tabs/WorkloadsTab.jsx").read_text()
    called = "/attribution/quota" if "'/attribution/quota'" in ui else "/workload-usage/quota"
    router = "attribution.py" if "attribution" in called else "workload_usage.py"
    src = (ROOT / f"backend/app/routers/{router}").read_text()
    assert "proxy_quota.score(" in src


@pytest.mark.parametrize("path", ["/attribution/quota", "/workload-usage/quota"])
def test_live_quota_endpoints_agree_and_never_exceed_100_by_borrowing(path):
    d = _get(f"{path}?days=7&dim_key=workload")
    if not d.get("rows"):
        pytest.skip("no proxy quota rows")
    assert d["resolver"] == "proxy_quota.resolve"
    assert d["quota_scope"] == "aws_billed_endpoints_only"
    for r in d["rows"]:
        assert r["endpoint"] in ("runtime", "mantle")
        if r["utilization_pct"] is None:
            assert r["tpm_limit"] is None
            assert r.get("limit_unknown_reason"), "an unknown limit must say why"
        else:
            assert r["tpm_limit"] is not None


def test_live_quota_endpoints_return_identical_rows():
    a = _get("/attribution/quota?days=7&dim_key=workload")
    b = _get("/workload-usage/quota?days=7&dim_key=workload")
    key = lambda d: sorted((r["workload"], r["model"], r["utilization_pct"])
                           for r in d.get("rows", []))
    assert key(a) == key(b), "the two quota endpoints disagree again"


# --------------------------------------------------------------------------- #
# 2 — the burndown mapping must match the doc
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model,expected", [
    ("anthropic.claude-fable-5-1-20260101-v1:0", 10),
    ("openai.gpt-5-6-sol", 10),
    ("openai.gpt-5-6-terra", 10),
    ("openai.gpt-5-6-luna", 10),
    ("anthropic.claude-sonnet-5", 10),
    ("anthropic.claude-opus-5", 10),
    ("anthropic.claude-opus-4-8", 15),
    ("anthropic.claude-sonnet-4-5", 5),
    ("amazon.nova-lite-v1:0", 1),
    ("openai.gpt-5-2-mini", 1),
])
def test_burndown_matches_the_documented_rates(model, expected):
    assert output_burndown_rate(model) == expected


def test_the_ten_x_skus_are_not_understated_as_1x():
    """Reporting a 10x SKU at 1x understates the output portion of quota burn
    tenfold, which is the difference between 'healthy' and 'throttling'."""
    for m in ("anthropic.claude-fable-5-1", "openai.gpt-5-6-sol"):
        assert output_burndown_rate(m) == 10, m


def test_burndown_still_off_on_mantle():
    for m in ("anthropic.claude-fable-5-1", "openai.gpt-5-6-sol",
              "anthropic.claude-opus-4-8"):
        assert output_burndown_rate(m, is_mantle=True) == 1


# --------------------------------------------------------------------------- #
# 3 — the dropped-filter disclosure must be rendered, not just returned
# --------------------------------------------------------------------------- #
def test_api_layer_publishes_the_disclosure():
    src = (ROOT / "frontend/src/api.js").read_text()
    assert "subscribeDisclosure" in src
    assert "noteDisclosure(data)" in src, "live responses must be inspected"
    assert "noteDisclosure(hit.data)" in src, "cached responses must be too"


def test_the_app_shell_renders_the_dropped_filter_warning():
    src = (ROOT / "frontend/src/App.jsx").read_text()
    assert "subscribeDisclosure" in src
    assert "filterDisclosure" in src
    assert "Not applied:" in src, "the dropped filter must be named on screen"
    assert 'type="warning"' in src, "a silently-broadened total warrants a warning"


def test_the_disclosure_resets_when_the_filter_changes():
    src = (ROOT / "frontend/src/App.jsx").read_text()
    assert "clearDisclosure()" in src, (
        "a stale banner from a previous selection would be its own defect")


def test_unknown_quota_limits_are_explained_on_screen():
    src = (ROOT / "frontend/src/tabs/WorkloadsTab.jsx").read_text()
    assert "limit_unknown_reason" in src
    assert "direct_provider_rows" in src, (
        "direct-provider traffic must be shown, not dropped")
