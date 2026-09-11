"""Request shape and cache accounting — Audit findings 08, 12, 14.

Needs a running backend for the API assertions (skipped otherwise):
  cd backend && DATABASE_URL=... AUTH_ENABLED=false PYTHONPATH=.. \
    ../.venv/bin/uvicorn app.main:app --port 8001
Run: .venv/bin/python -m pytest tests/test_shape_and_cache.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

BASE = os.environ.get("LENS_API", "http://localhost:8001/api")


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"backend not reachable at {BASE}: {e}")


# --------------------------------------------------------------------------- #
# Finding 12 — the ratio must mean what its label says
# --------------------------------------------------------------------------- #
def test_ratio_is_input_over_output_and_matches_the_averages():
    """The audit's case: 9,253 avg input / 124 avg output is 74.7:1 input-heavy,
    but the panel rendered "0.0:1" (it was returning output/input) and its
    `< 2` "output-heavy" warning fired on input-heavy traffic."""
    rows = _get("/ops-request-shape?days=7")
    if not rows:
        pytest.skip("no shape rows")
    for r in rows[:10]:
        ai, ao = float(r["avg_input"]), float(r["avg_output"])
        assert r["ratio_basis"] == "input_tokens_per_output_token"
        if ao == 0:
            assert r["input_output_ratio"] is None
            continue
        expected = ai / ao
        assert float(r["input_output_ratio"]) == pytest.approx(expected, rel=0.02), (
            "input:output ratio must equal avg_input / avg_output")
        assert float(r["ratio"]) == pytest.approx(float(r["input_output_ratio"]), rel=1e-9), (
            "`ratio` must carry the labelled (input:output) orientation")
        if float(r["output_input_ratio"]) > 0:
            assert float(r["input_output_ratio"]) == pytest.approx(
                1.0 / float(r["output_input_ratio"]), rel=0.02)


def test_input_heavy_traffic_is_not_flagged_output_heavy():
    """The UI severity thresholds (>50 input-heavy, <2 output-heavy) must be
    applied to the input:output orientation, so they agree with Ops Review."""
    rows = _get("/ops-request-shape?days=7")
    if not rows:
        pytest.skip("no shape rows")
    for r in rows[:10]:
        ai, ao = float(r["avg_input"]), float(r["avg_output"])
        if ao == 0 or ai <= ao:
            continue
        v = float(r["input_output_ratio"])
        assert v > 1, f"input-heavy row ({ai} in / {ao} out) must not read as {v}"


def test_shape_agrees_with_ops_review_orientation():
    """Both panels describe the same measure, so an input-heavy row must not be
    'input-heavy' in one place and 'output-heavy' in the other."""
    shape = _get("/ops-request-shape?days=7")
    review = _get("/ops-review?days=7")
    rr = (review or {}).get("findings", review or {}).get("request_shape") \
        if isinstance(review, dict) else None
    if not shape or not rr:
        pytest.skip("no comparable rows")
    by_key = {(r.get("accountId"), r.get("modelId")): float(r["ratio"]) for r in rr
              if r.get("ratio") is not None}
    checked = 0
    for r in shape:
        k = (r.get("accountid") or r.get("accountId"), r.get("modelid") or r.get("modelId"))
        if k in by_key and r.get("input_output_ratio") is not None:
            # Same orientation, so same side of 1.0.
            a, b = float(r["input_output_ratio"]), by_key[k]
            assert (a > 1) == (b > 1), f"orientation disagreement for {k}: {a} vs {b}"
            checked += 1
    if checked == 0:
        pytest.skip("no overlapping rows in this window")


# --------------------------------------------------------------------------- #
# Finding 14 — cached share must include writes and must not claim to be a
# per-request hit rate
# --------------------------------------------------------------------------- #
def test_cached_share_denominator_includes_writes():
    rows = _get("/ops-caching?days=7")
    if not rows:
        pytest.skip("no caching rows")
    for r in rows:
        read = int(r["cache_read_tokens"] or 0)
        write = int(r["cache_write_tokens"] or 0)
        inp = int(r["total_input_tokens"] or 0)
        denom = read + write + inp
        assert int(r["prompt_tokens_total"]) == denom
        if denom == 0:
            continue
        assert float(r["cached_prompt_token_pct"]) == pytest.approx(
            100.0 * read / denom, abs=0.02)
        if write > 0:
            # Omitting writes always overstates the share.
            old = 100.0 * read / (read + inp) if (read + inp) else 0
            assert float(r["cached_prompt_token_pct"]) <= old + 1e-9


def test_cached_share_states_it_is_not_a_request_hit_rate():
    rows = _get("/ops-caching?days=7")
    if not rows:
        pytest.skip("no caching rows")
    for r in rows:
        assert r["basis"] == "cached_share_of_prompt_tokens"
        assert r["request_hit_rate_available"] is False
        assert float(r["cached_prompt_token_pct"]) <= 100.0


def test_model_insights_uses_the_same_cache_basis():
    rows = _get("/model-insights?days=7")
    items = rows if isinstance(rows, list) else rows.get("rows", [])
    if not items:
        pytest.skip("no model rows")
    for m in items[:10]:
        if m.get("cache_basis") is None:
            continue
        assert m["cache_basis"] == "cached_share_of_prompt_tokens"
        read = int(m.get("cache_read_tokens") or 0)
        write = int(m.get("cache_write_tokens") or 0)
        inp = int(m.get("input_tokens") or 0)
        denom = read + write + inp
        if denom:
            assert float(m["cached_prompt_token_pct"]) == pytest.approx(
                100.0 * read / denom, abs=0.02)
        # The legacy alias must not disagree with the corrected value.
        assert float(m["cache_hit_pct"]) == pytest.approx(
            float(m["cached_prompt_token_pct"]), abs=0.01)


def test_daily_trend_exposes_cache_writes_for_the_trend_chart():
    rows = _get("/daily-trend?days=7")
    if not rows:
        pytest.skip("no trend rows")
    assert "cache_write_tokens" in rows[0], (
        "the trend chart cannot compute the corrected denominator without writes")


# --------------------------------------------------------------------------- #
# Finding 08 — the burndown narrative must match the documented mechanism
# --------------------------------------------------------------------------- #
def test_ui_copy_does_not_claim_the_reservation_is_multiplied():
    """AWS documents the up-front deduction as (total input tokens + max_tokens).
    The burndown rate applies to tokens actually generated, so copy claiming
    "max_tokens x rate x RPM" is reserved is wrong."""
    src = (ROOT / "frontend/src/components/SectionInfo.jsx").read_text()
    for bad in ("max_tokens × rate × RPM", "max_tokens x rate x RPM",
                "3× larger than the old 5×", "3× worse than the old 5×"):
        assert bad not in src, f"stale overclaim still present: {bad}"
    assert "input tokens + max_tokens" in src, (
        "the documented reservation formula should be stated")
    # And it must be honest that the app cannot observe max_tokens.
    assert re.search(r"cannot (see|observe) max_tokens", src), (
        "copy should say max_tokens is not visible in CloudWatch metrics")


def test_ops_review_remediation_matches_the_documented_mechanism():
    src = (ROOT / "backend/app/routers/ops_review.py").read_text()
    assert "input tokens + max_tokens" in src
    assert "phantom quota burndown" not in src, (
        "'phantom burndown' overstated a mechanism AWS documents plainly")
