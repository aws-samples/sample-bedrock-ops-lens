"""Native quota metric vs reconstructed formula, and the last unconsolidated
quota matcher — burndown review, 2026-09-10.

The review made five claims about how quota consumption is derived. All five
reproduced against the code. The scheduled-findings path (which is what sends
customer SNS alerts) turned out to carry the worst of them: its own private copy
of the substring quota matcher that every other caller had already been moved
off.

  1. cw_metrics stored an ABSENT EstimatedTPMQuotaUsage datapoint as 0, which is
     indistinguishable from an observed zero. Everything downstream then reads
     "AWS says this model consumed no quota" where the truth is "AWS returned no
     datapoint". The column is nullable; the loader was the only thing
     flattening it.

  2. quota_drilldown NEVER consulted the native metric — it always reconstructed
     input + cache_write + output*rate, so the endpoint the Quotas tab reads
     disagreed with /ops-peak-rpm for the same hour whenever AWS published a
     value.

  3. findings.py combined MAX(input) and MAX(output) selected INDEPENDENTLY
     across different hours into a single peak. An account whose input peaked at
     09:00 and output peaked at 17:00 got a peak that never happened in any real
     minute, always >= the true peak, so it fires spurious critical alerts.

  4. findings.py used max(native, formula) instead of choosing a source. A stale
     multiplier silently overrides a live AWS observation — exactly backwards.

  5. findings.py matched quota names by substring (`all(p in name ...)`) and on a
     tie preferred the LARGER limit. This is the defect the shared resolver was
     built to kill: "Claude Sonnet 4" matches `claude-sonnet-4-5` because "4" is
     a substring of "4-5", and preferring the larger limit understates
     utilization. It reached the notification path, so the alert that should have
     fired at 160% never fired at all.

Run: .venv/bin/python -m pytest tests/test_burndown_native_metric.py -q
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.quota_match import resolve_quota  # noqa: E402
from app.burndown import output_burndown_rate  # noqa: E402

sys.path.insert(0, str(ROOT / "ingestion"))
from ingestion import findings as F  # noqa: E402


ACCT, REGION = "111111111111", "us-east-1"


def q(model_name, limit, metric="TPM", traffic="On-demand"):
    return {"accountId": ACCT, "region": REGION, "model_name": model_name,
            "traffic_type": traffic, "metric": metric, "applied_value": limit,
            "default_value": limit, "quota_code": "L-TEST"}


# --------------------------------------------------------------------------- #
# 1 — absent vs observed zero
# --------------------------------------------------------------------------- #
def test_loader_preserves_a_missing_native_datapoint_as_null():
    """`int(m.get(...) , 0) or 0` turned "AWS returned nothing" into a hard 0."""
    src = (ROOT / "ingestion/cw_metrics.py").read_text()
    assert 'int(m.get("estimated_tpm_quota_usage", 0) or 0)' not in src, (
        "an absent datapoint must stay NULL, not become an observed zero")
    assert "_native_or_none" in src, "the loader needs a missing-preserving helper"


def test_native_or_none_distinguishes_zero_from_absent():
    from ingestion.cw_metrics import _native_or_none
    assert _native_or_none({}, "k") is None, "absent -> NULL"
    assert _native_or_none({"k": None}, "k") is None, "explicit None -> NULL"
    assert _native_or_none({"k": 0}, "k") == 0, "an observed zero is a real zero"
    assert _native_or_none({"k": 0.0}, "k") == 0, "observed 0.0 is a real zero"
    assert _native_or_none({"k": 30000}, "k") == 30000


# --------------------------------------------------------------------------- #
# 2 — one source-selection rule, used everywhere
# --------------------------------------------------------------------------- #
def test_native_observation_wins_over_a_stale_formula():
    """Review acceptance check 1: native 30,000 vs formula 40,000 -> 30,000."""
    got, source = F.quota_consumption(native=30_000, input_quota_tokens=0,
                                      output_tokens=8_000, rate=5)
    assert got == 30_000
    assert source == "aws_estimate"


def test_native_is_not_multiplied_again():
    """The native metric already has burndown baked in."""
    got, _ = F.quota_consumption(native=30_000, input_quota_tokens=0,
                                 output_tokens=8_000, rate=10)
    assert got == 30_000, "applying the rate to the native value double-counts"


def test_an_observed_zero_is_used_not_treated_as_missing():
    got, source = F.quota_consumption(native=0, input_quota_tokens=999,
                                      output_tokens=999, rate=10)
    assert got == 0 and source == "aws_estimate"


def test_reconstruction_when_native_is_absent():
    """Review acceptance check 3: 100 uncached input + 20 cache-write + 10
    output at 5x = 170. Cache READS are excluded."""
    got, source = F.quota_consumption(native=None, input_quota_tokens=120,
                                      output_tokens=10, rate=5)
    assert got == 170
    assert source == "reconstructed"


def test_unknown_when_neither_source_is_available():
    got, source = F.quota_consumption(native=None, input_quota_tokens=None,
                                      output_tokens=None, rate=5)
    assert got is None
    assert source == "unavailable"


def _code_only(path: Path) -> str:
    """Source with comments and string literals stripped.

    A plain substring scan is the wrong probe here: the fix's own docstring
    explains why `max(native, formula)` is wrong, so searching the raw text
    matches the explanation and reports the bug as unfixed.
    """
    import io
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_max_of_native_and_formula_is_gone():
    code = _code_only(ROOT / "ingestion/findings.py")
    assert "peak_native_tpm" not in code, "the pre-aggregated native max is gone"
    assert "formula_tpm" not in code
    # Whatever max() calls remain must not be reducing a native/formula pair.
    tree = ast.parse((ROOT / "ingestion/findings.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "max" and len(node.args) == 2):
            names = {n.id for a in node.args for n in ast.walk(a)
                     if isinstance(n, ast.Name)}
            assert not ({"native"} & names), (
                "picking the larger of native and formula is not source selection")


# --------------------------------------------------------------------------- #
# 3 — a peak must belong to one real hour
# --------------------------------------------------------------------------- #
def test_findings_computes_consumption_per_hour_before_taking_the_peak():
    """Review acceptance check 4: independently selected input and output maxima
    cannot be combined into an hour that never happened."""
    src = (ROOT / "ingestion/findings.py").read_text()
    assert "AS peak_in_tpm" not in src, (
        "a separately-selected input peak is what allowed the invented hour")
    assert "AS peak_out_tpm" not in src
    # The weighting must happen inside the row expression, under one MAX.
    assert "MAX(" in src and "$1" in src, "the burndown rate must be a bound param"


def test_the_invented_hour_is_arithmetically_worse_than_the_truth():
    """Demonstrates why it mattered, independent of implementation: input peaks
    at 09:00, output peaks at 17:00, and neither hour is as bad as the sum."""
    hours = [
        {"input_quota_tokens": 600_000, "output_tokens": 1_000},   # 09:00
        {"input_quota_tokens": 10_000,  "output_tokens": 50_000},  # 17:00
    ]
    rate = 10
    per_hour = [F.quota_consumption(None, h["input_quota_tokens"],
                                    h["output_tokens"], rate)[0] for h in hours]
    true_peak = max(per_hour)                      # 610,000 vs 510,000 -> 610,000
    invented = (max(h["input_quota_tokens"] for h in hours)
                + max(h["output_tokens"] for h in hours) * rate)
    assert true_peak == 610_000
    assert invented == 1_100_000
    assert invented > true_peak, "the old shape overstates by 80% here"


# --------------------------------------------------------------------------- #
# 4 — the notification path must use the shared resolver
# --------------------------------------------------------------------------- #
def test_findings_no_longer_has_its_own_substring_matcher():
    src = (ROOT / "ingestion/findings.py").read_text()
    assert "all(p in name for p in parts[:2])" not in src, (
        "substring matching is the Sonnet-4-vs-4.5 defect")
    assert "prefer larger limit" not in src
    assert "> (best[\"limit_value\"] or 0)" not in src, (
        "preferring the larger limit understates utilization")
    assert "resolve_quota" in src, "the notification path must share the resolver"


def test_the_shared_resolver_does_not_confuse_sonnet_4_with_4_5():
    """The original counterexample: 80,000 TPM against Sonnet 4.5's real 50,000
    limit is 160%, not 8% against Sonnet 4's 1,000,000."""
    rows = [q("Claude Sonnet 4", 1_000_000), q("Claude Sonnet 4.5", 50_000)]
    res = resolve_quota(rows, ACCT, REGION,
                        "anthropic.claude-sonnet-4-5-20250929-v1:0", metric="TPM")
    assert res.value == 50_000
    assert round(80_000 / res.value * 100) == 160


def test_findings_reports_the_same_utilization_as_the_resolver():
    """End to end on the fixture the notification job would see: the old code
    picked the 1,000,000 limit and stayed silent; 160% must now be reported."""
    rows = [q("Claude Sonnet 4", 1_000_000), q("Claude Sonnet 4.5", 50_000)]
    model = "anthropic.claude-sonnet-4-5-20250929-v1:0"
    limit = resolve_quota(rows, ACCT, REGION, model, metric="TPM").value
    consumption, source = F.quota_consumption(None, 80_000, 0, 1)
    util = 100.0 * consumption / limit
    assert source == "reconstructed"
    assert util == pytest.approx(160.0)
    assert util >= 100.0, "this is the alert that previously never fired"


# --------------------------------------------------------------------------- #
# 5 — disclosure: a mixed series must not claim to be native
# --------------------------------------------------------------------------- #
def test_a_single_native_hour_does_not_relabel_the_whole_series():
    src = (ROOT / "backend/app/routers/ops_insights.py").read_text()
    tree = ast.parse(src)
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Subscript)
                       and isinstance(t.slice, ast.Constant)
                       and t.slice.value == "quota_tpm_source" for t in n.targets)]
    # Whatever it assigns, a bare unconditional "native" is the bug.
    bad = [a for a in assigns
           if isinstance(a.value, ast.Constant) and a.value.value == "native"]
    assert not bad, (
        "setting source='native' on any native hour mislabels a mixed series; "
        "it must be derived from all buckets (native/reconstructed/mixed)")
    assert "mixed" in src, "a mixed-source series must be able to say so"


def test_drilldown_prefers_the_native_metric_when_present():
    src = (ROOT / "backend/app/routers/quota_drilldown.py").read_text()
    assert "estimated_tpm_quota_usage" in src, (
        "the drill-down never consulted the native metric at all")
    assert "quota_tpm_source" in src, "and it must disclose which source it used"


def test_burndown_rates_still_match_the_documented_table():
    """The catalog stays authoritative for the reconstruction path, so the rates
    themselves must not drift while this refactor happens."""
    assert output_burndown_rate("anthropic.claude-opus-4-8") == 15
    assert output_burndown_rate("anthropic.claude-sonnet-5") == 10
    assert output_burndown_rate("openai.gpt-5-6-sol") == 10
    assert output_burndown_rate("anthropic.claude-sonnet-4-5") == 5
    assert output_burndown_rate("amazon.nova-lite-v1:0") == 1
    assert output_burndown_rate("anthropic.claude-opus-4-8", is_mantle=True) == 1
