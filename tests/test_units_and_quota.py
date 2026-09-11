"""Unit contracts: hourly->per-minute conversion, quota resolution, burndown.

Audit findings 02, 03, 08. Expected values are hand-computed in the
docstrings, not produced by calling the same helper under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.burndown import output_burndown_rate  # noqa: E402
from app.quota_match import (FAMILY_CROSS_REGION, FAMILY_GLOBAL_CRIS,  # noqa: E402
                             FAMILY_ON_DEMAND, resolve_quota)
from app.units import (hourly_total_to_per_minute,  # noqa: E402
                       max_hourly_total_to_per_minute, utilization_pct)


# --------------------------------------------------------------------------- #
# Finding 02 — units
# --------------------------------------------------------------------------- #
def test_hourly_total_converts_to_per_minute_average():
    """6,000 requests in an hour = 100/min on average (NOT 6,000, NOT 360,000)."""
    assert hourly_total_to_per_minute(6000) == 100.0


def test_the_60x_and_3600x_errors_are_gone():
    """The two reported bugs, stated numerically.

    Old burndown widget: returned the hourly sum as TPM -> 60x high.
    Old Ops Review: multiplied the hourly sum by 60 as RPM -> 3,600x high.
    73 requests in the busiest hour is 1.2166.../min."""
    hourly_sum = 73
    correct = hourly_total_to_per_minute(hourly_sum)
    assert round(correct, 4) == 1.2167
    # Ratios compared with a tolerance: 73/60 is not exactly representable.
    assert hourly_sum / correct == pytest.approx(60.0)          # old TPM error
    assert (hourly_sum * 60) / correct == pytest.approx(3600.0)  # old RPM error


def test_max_then_divide_equals_divide_then_max():
    assert max_hourly_total_to_per_minute([600, 1200, 300]) == 20.0


def test_none_and_empty_are_zero_not_crashes():
    assert hourly_total_to_per_minute(None) == 0.0
    assert max_hourly_total_to_per_minute([]) == 0.0
    assert max_hourly_total_to_per_minute([None, None]) == 0.0


def test_utilization_requires_a_known_limit():
    """90/min against a 100/min limit is 90%. An unknown or zero limit must be
    None (unknown), never 0% and never a percentage of another limit."""
    assert utilization_pct(90, 100) == 90.0
    assert utilization_pct(90, None) is None
    assert utilization_pct(90, 0) is None
    assert utilization_pct(None, 100) is None


# --------------------------------------------------------------------------- #
# Finding 03 — quota resolution
# --------------------------------------------------------------------------- #
def _q(family, value, metric="TPM", name="claude-sonnet-4-5-20250929",
       acct="482915037461", region="eu-west-1", code=None, applied=True):
    row = {"accountId": acct, "region": region, "model_name": name,
           "metric": metric, "traffic_type": family, "quota_code": code,
           "applied_value": value if applied else None,
           "default_value": None if applied else value}
    return row


MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
THREE_FAMILIES = [
    _q(FAMILY_ON_DEMAND, 6_460_459.81),
    _q(FAMILY_CROSS_REGION, 2_790_378.15),
    _q(FAMILY_GLOBAL_CRIS, 14_959_243.00),
]


def test_does_not_pick_the_largest_limit_across_families():
    """The reported bug: max() chose Global CRIS 14,959,243, turning a real
    90%-style utilization into single digits. On-demand is the documented
    default path and must be the primary, with the ambiguity flagged."""
    r = resolve_quota(THREE_FAMILIES, "482915037461", "eu-west-1", MODEL)
    assert r.value == 6_460_459.81
    assert r.family == FAMILY_ON_DEMAND
    assert r.ambiguous is True, "three candidate families must be flagged"
    assert r.range == (2_790_378.15, 14_959_243.00)
    assert len(r.candidates) == 3


def test_family_hint_resolves_the_ambiguity():
    r = resolve_quota(THREE_FAMILIES, "482915037461", "eu-west-1", MODEL,
                      family_hint=FAMILY_CROSS_REGION)
    assert r.value == 2_790_378.15
    assert r.family == FAMILY_CROSS_REGION
    assert r.ambiguous is False, "an explicit family is not ambiguous"


def test_single_family_is_unambiguous():
    r = resolve_quota([_q(FAMILY_ON_DEMAND, 100.0)], "482915037461",
                      "eu-west-1", MODEL)
    assert r.value == 100.0 and r.ambiguous is False


def test_unknown_quota_returns_not_known_rather_than_a_guess():
    r = resolve_quota([], "482915037461", "eu-west-1", MODEL)
    assert r.known is False and r.value is None
    assert utilization_pct(500, r.value) is None


def test_default_value_counts_as_a_real_limit():
    """An un-raised quota still has an AWS-published default; dropping those
    rows (old `applied_value IS NOT NULL`) made models look quota-less."""
    r = resolve_quota([_q(FAMILY_ON_DEMAND, 4_000.0, applied=False)],
                      "482915037461", "eu-west-1", MODEL)
    assert r.value == 4_000.0


def test_wrong_account_region_metric_never_matches():
    rows = [
        _q(FAMILY_ON_DEMAND, 999.0, acct="999999999999"),
        _q(FAMILY_ON_DEMAND, 888.0, region="us-east-1"),
        _q(FAMILY_ON_DEMAND, 777.0, metric="RPM"),
    ]
    r = resolve_quota(rows, "482915037461", "eu-west-1", MODEL, metric="TPM")
    assert r.known is False


def test_ambiguous_primary_falls_back_to_lowest_when_no_on_demand():
    """Without On-demand present, err toward over-reporting pressure (lowest
    limit) rather than hiding it (highest)."""
    rows = [_q(FAMILY_CROSS_REGION, 2_000.0), _q(FAMILY_GLOBAL_CRIS, 9_000.0)]
    r = resolve_quota(rows, "482915037461", "eu-west-1", MODEL)
    assert r.value == 2_000.0
    assert r.ambiguous is True


def test_as_dict_exposes_the_uncertainty_to_the_api():
    d = resolve_quota(THREE_FAMILIES, "482915037461", "eu-west-1", MODEL).as_dict()
    assert d["quota_ambiguous"] is True
    assert d["quota_family"] == FAMILY_ON_DEMAND
    assert d["quota_limit_range"] == {"min": 2_790_378.15, "max": 14_959_243.00}
    assert [c["family"] for c in d["quota_candidate_families"]] == [
        FAMILY_CROSS_REGION, FAMILY_ON_DEMAND, FAMILY_GLOBAL_CRIS]


# --------------------------------------------------------------------------- #
# Finding 08 — burndown must be per-model and applied WITHIN each hour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model,expected", [
    ("anthropic.claude-opus-4-8", 15),
    ("us.anthropic.claude-sonnet-5", 10),
    ("anthropic.claude-opus-5", 10),
    # The doc's 10x list is "Claude Sonnet 5, Claude Opus 5, and Claude Fable
    # 5.1", and separately "GPT-5.6 Sol, GPT-5.6 Terra, and GPT-5.6 Luna on the
    # bedrock-runtime endpoint". An earlier revision of this test asserted 1x for
    # the GPT SKUs on the incorrect reasoning that only Claude SKUs burn down and
    # that the OpenAI SKUs are mantle-only. It therefore locked in a tenfold
    # understatement of their quota burn.
    ("anthropic.claude-fable-5-1-20260101-v1:0", 10),
    ("openai.gpt-5.6-sol", 10),
    ("openai.gpt-5-6-terra", 10),
    ("gpt-5.6-luna", 10),
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", 5),
    ("anthropic.claude-3-haiku-20240307-v1:0", 5),
    ("amazon.nova-lite-v1:0", 1),
    ("meta.llama3-1-70b-instruct-v1:0", 1),
    # Not in any named group: GPT-5.2 and a hypothetical Fable 5.2 are 1:1 by the
    # doc's "all other models" clause. The 10x match must be SKU-exact, not a
    # prefix guess.
    ("openai.gpt-5-2-mini", 1),
    ("anthropic.claude-fable-5-2", 1),
])
def test_documented_burndown_rates(model, expected):
    assert output_burndown_rate(model) == expected


@pytest.mark.parametrize("name,expected", [
    ("Claude Fable 5.1", 10),
    ("GPT-5.6 Sol", 10),
    ("GPT-5.6 Terra", 10),
    ("GPT-5.6 Luna", 10),
    ("Claude Opus 4.8", 15),
    ("Claude 3.7 Sonnet", 5),
])
def test_documented_burndown_rates_by_public_name(name, expected):
    """Callers may only have the friendly name; both spellings must agree."""
    assert output_burndown_rate(None, name) == expected


@pytest.mark.parametrize("model", [
    "anthropic.claude-fable-5-1", "openai.gpt-5-6-sol", "gpt-5.6-terra",
    "anthropic.claude-sonnet-5", "anthropic.claude-opus-4-8",
])
def test_ten_x_skus_still_have_no_burndown_on_mantle(model):
    """Burndown applies only to bedrock-runtime: "Models available exclusively on
    the bedrock-mantle endpoint have separate quotas for input and output tokens,
    so burndown does not apply." """
    assert output_burndown_rate(model, is_mantle=True) == 1


def test_a_ten_x_sku_understated_as_1x_is_a_tenfold_error():
    """100 input + 1,000 output on GPT-5.6 Sol: 100 + 1,000*10 = 10,100 quota
    tokens. Reporting it at 1x gives 1,100 - 89% low."""
    rate = output_burndown_rate("openai.gpt-5-6-sol")
    assert 100 + 1000 * rate == 10_100
    assert 100 + 1000 * 1 == 1_100  # the old, wrong value


def test_mantle_has_no_burndown():
    assert output_burndown_rate("anthropic.claude-opus-4-8", is_mantle=True) == 1


def test_opus_48_effective_tokens_are_not_computed_at_5x():
    """100 input + 1,000 output on Opus 4.8: 100 + 1,000*15 = 15,100 quota
    tokens. The old hardcoded 5x produced 5,100 — 66.2% low."""
    inp, out = 100, 1000
    rate = output_burndown_rate("anthropic.claude-opus-4-8")
    assert inp + out * rate == 15_100
    assert inp + out * 5 == 5_100  # the old, wrong value


def test_effective_peak_must_be_taken_per_hour_not_from_combined_maxima():
    """Two hours: A(raw 1,100 / out 100), B(raw 1,000 / out 1,000), rate 5x.

    Per-hour effective: A = 1,000+100*5 = 1,500 (raw 1,100 => input 1,000)
                        B = 0+1,000*5   = 5,000 (raw 1,000 => input 0)
    Correct effective peak = 5,000.
    The old MAX(raw) + 4*MAX(out) = 1,100 + 4*1,000 = 5,100 — a value neither
    hour ever had."""
    hours = [(1000, 100), (0, 1000)]   # (input_quota_tokens, output_tokens)
    rate = 5
    per_hour_eff = [inp + out * rate for inp, out in hours]
    assert per_hour_eff == [1500, 5000]
    assert max(per_hour_eff) == 5000
    wrong = max(inp + out for inp, out in hours) + (rate - 1) * max(out for _, out in hours)
    assert wrong == 5100 and wrong != max(per_hour_eff)
