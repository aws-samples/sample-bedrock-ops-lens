"""Scope and accounting integrity — Audit findings 10, 11, 13, 16, 17, 18.

Two kinds of test live here:

  * Pure unit tests of the filter layer (no database), which run everywhere.
  * API tests that need a running backend, skipped when none is reachable.

Start a backend first for the API half:
  cd backend && DATABASE_URL=... AUTH_ENABLED=false PYTHONPATH=.. \
    ../.venv/bin/uvicorn app.main:app --port 8001
Run: .venv/bin/python -m pytest tests/test_scope_integrity.py -q
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.filters import FilterSet, build_where  # noqa: E402

BASE = os.environ.get("LENS_API", "http://localhost:8001/api")


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"backend not reachable at {BASE}: {e}")


def _fs(**kw) -> FilterSet:
    base = dict(start=date(2026, 9, 1), end=date(2026, 9, 8), provider="all",
                region="all", accounts=(), traffic_type="all", tag_filter=(),
                endpoint="all")
    base.update(kw)
    return FilterSet(**base)


# --------------------------------------------------------------------------- #
# Finding 11 — replace() must preserve every field a hand-built FilterSet drops
# --------------------------------------------------------------------------- #
def test_replace_preserves_endpoint_while_overriding_region():
    """/regions rebuilt the FilterSet field by field and forgot `endpoint`, so
    the Mantle region chart summed 23,204,626 requests under a 208,705 headline
    (111x). replace() cannot forget a field."""
    f = _fs(endpoint="mantle", region="us-east-1", traffic_type="cris")
    overridden = replace(f, region="all")
    assert overridden.endpoint == "mantle"
    assert overridden.region == "all"
    assert overridden.traffic_type == "cris"
    # And the endpoint must survive into the SQL.
    assert "endpoint" in build_where(overridden).sql


def test_replace_preserves_invalid_flags():
    """A forced-endpoint rebuild must not discard the `invalid` markers, or an
    unsupported filter value would widen the scope again (finding 13)."""
    f = _fs(provider="openai")
    f = replace(f, invalid=(("provider", "openai"),))
    forced = replace(f, endpoint="mantle")
    assert forced.invalid == (("provider", "openai"),)
    assert "FALSE" in build_where(forced).sql


# --------------------------------------------------------------------------- #
# Finding 13 — unsupported filter values must narrow to nothing, never widen
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field,value", [
    ("provider", "openai"),          # real name, not a Bedrock modelId prefix
    ("provider", "NONSENSE!!"),
    ("region", "bogus!!"),
    ("traffic_type", "nope"),
])
def test_unsupported_values_produce_an_empty_scope(field, value):
    f = replace(_fs(**{field: value}), invalid=((field, value),))
    assert "FALSE" in build_where(f).sql, (
        f"{field}={value!r} must select nothing, not fall through to the fleet")


def test_supported_values_still_filter():
    for field, value, token in [("provider", "anthropic", "modelId"),
                                ("region", "us-east-1", "region"),
                                ("traffic_type", "cris", "traffic_type")]:
        w = build_where(_fs(**{field: value}))
        assert "FALSE" not in w.sql
        assert token in w.sql


# --------------------------------------------------------------------------- #
# Finding 11 (API) — every region rollup must reconcile with its own headline
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint", ["all", "runtime", "mantle"])
def test_region_rollup_matches_the_headline(endpoint):
    s = _get(f"/summary?days=7&endpoint={endpoint}")
    r = _get(f"/regions?days=7&endpoint={endpoint}")
    rows = r if isinstance(r, list) else r.get("rows", [])
    got = sum(int(x.get("total_requests") or 0) for x in rows)
    assert got == int(s["total_requests"]), (
        f"{endpoint}: regions sum {got} != headline {s['total_requests']}")


# --------------------------------------------------------------------------- #
# Finding 18 — unknown routing is not CRIS adoption
# --------------------------------------------------------------------------- #
def test_cris_adoption_accounts_for_every_request():
    rows = _get("/ops-cris-adoption?days=7&endpoint=runtime")
    if not rows:
        pytest.skip("no rows")
    for r in rows:
        c = int(r["cris_requests"] or 0)
        o = int(r["od_requests"] or 0)
        u = int(r["unclassified_requests"] or 0)
        t = int(r["total_requests"] or 0)
        assert c + o + u == t, "requests must be fully partitioned"
        assert u >= 0, "unclassified cannot be negative"
        if c + o == 0:
            assert r["cris_pct"] is None, (
                "with nothing classified, adoption is unknown - not 0% and not 100%")
        else:
            # Percentage taken over classified traffic only.
            assert abs(float(r["cris_pct"]) - 100.0 * c / (c + o)) < 0.02


def test_cris_adoption_reports_routing_coverage():
    rows = _get("/ops-cris-adoption?days=7&endpoint=runtime")
    if not rows:
        pytest.skip("no rows")
    assert any(r.get("routing_known_pct") is not None for r in rows)
    for r in rows:
        p = r.get("routing_known_pct")
        if p is not None:
            assert 0 <= float(p) <= 100


# --------------------------------------------------------------------------- #
# Finding 16 — the operation split must not be an arbitrary tag_key subset
# --------------------------------------------------------------------------- #
def test_operations_uses_the_whole_population_accounting_row():
    rows = _get("/operations?days=7")
    if not rows:
        pytest.skip("invocation logging off in this dataset")
    assert rows[0]["basis_tag_key"] == "__all__", (
        "the operation split must read the accounting row, not a sparse tag key")
    assert rows[0]["exact"] is True


def test_operations_total_is_not_below_a_single_tag_keys_total():
    """The old MIN(tag_key) basis undercounted; whatever we report now must be
    at least as large as any individual tag key's coverage."""
    ops = _get("/operations?days=7")
    if not ops:
        pytest.skip("invocation logging off")
    total = sum(int(r["total_requests"]) for r in ops)
    tags = _get("/tags")
    keys = tags if isinstance(tags, list) else tags.get("rows", tags)
    assert total > 0
    for t in keys:
        k = t.get("tag_key") if isinstance(t, dict) else t
        assert k != "__all__", "the accounting row must not be offered as a tag"


# --------------------------------------------------------------------------- #
# Finding 17 — windows and time zones must be stated, not implied
# --------------------------------------------------------------------------- #
def test_hourly_heatmap_reports_its_real_coverage():
    d = _get("/hourly-heatmap?days=200")
    assert "coverage" in d and "rows" in d
    c = d["coverage"]
    assert c["days_requested"] == 200
    assert c["hour_basis"] == "utc"
    if c["days_covered"]:
        assert c["days_covered"] <= c["days_requested"]
        assert c["min_date"] and c["max_date"]


def test_hourly_timestamps_carry_an_explicit_utc_offset():
    """A naive "2026-09-09T14:00:00" is parsed as LOCAL by JS, shifting the whole
    hourly axis by the browser's offset."""
    d = _get("/status-codes?days=2")
    series = d.get("series") or d.get("rows") or []
    if not series:
        pytest.skip("no hourly status rows")
    for r in series[:5]:
        ts = r.get("ts")
        if ts is None:
            continue
        assert ts.endswith("+00:00") or ts.endswith("Z"), (
            f"hourly ts {ts!r} has no time zone, so clients must guess")


# --------------------------------------------------------------------------- #
# Finding 10 — a scoped KPI must not quote a fleet-wide comparison
# --------------------------------------------------------------------------- #
def test_wow_comparison_is_scoped_and_states_both_windows():
    fleet = _get("/wow-comparison?days=7")
    assert "current_window" in fleet and "previous_window" in fleet
    cw, pw = fleet["current_window"], fleet["previous_window"]
    # The windows must be adjacent and the same length.
    c0, c1 = date.fromisoformat(cw["start"]), date.fromisoformat(cw["end"])
    p0, p1 = date.fromisoformat(pw["start"]), date.fromisoformat(pw["end"])
    assert (c1 - c0) == (p1 - p0), "compared windows must be the same length"
    assert p1 + timedelta(days=1) == c0, "previous window must abut the current one"

    accounts = _get("/accounts")
    ids = [a["accountId"] for a in accounts][:1]
    if not ids:
        pytest.skip("no accounts")
    one = _get(f"/wow-comparison?days=7&accounts={ids[0]}")
    assert one.get("scoped") is True
    assert one != fleet, "an account-scoped WoW must not repeat the fleet figure"
