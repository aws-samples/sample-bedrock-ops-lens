"""Runtime-editable burndown rate catalog — the 9 acceptance checks.

Checks 1-4 and 7-9 are covered here or in test_burndown_native_metric.py;
check 5 (one Settings change reaches every consumer) and check 6 (a
future-effective rate changes only eligible observations) are the two that
needed the catalog to exist at all.

Run: .venv/bin/python -m pytest tests/test_rate_catalog.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app import rate_catalog as rc  # noqa: E402


def cat(entries, revision="1") -> rc.Catalog:
    return rc.Catalog(entries=tuple(entries), revision=revision)


def _code_only(path: Path) -> str:
    """Source with comments and string literals removed.

    Needed because these fixes DOCUMENT the bug they fix, so a raw substring
    scan matches the explanation and reports the bug as still present.
    """
    import io
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


# --------------------------------------------------------------------------- #
# Matching + provenance
# --------------------------------------------------------------------------- #
def test_a_catalog_entry_overrides_the_bundled_rate():
    """The whole point: AWS changes a rate, an admin edits one row, no redeploy."""
    c = cat([{"id": "nova-pro", "all_of": ["novapro"], "rate": 4,
              "endpoint": "runtime", "verified_on": "2026-09-10"}])
    res = c.rate_for("amazon.nova-pro-v1:0")
    assert res.rate == 4                      # bundled table says 1
    assert res.source == "catalog"
    assert res.verified is True
    assert res.entry_id == "nova-pro"


def test_separator_forms_both_match():
    c = cat([{"all_of": ["gpt56sol"], "rate": 10}])
    assert c.rate_for("openai.gpt-5-6-sol").rate == 10
    assert c.rate_for("x", "GPT-5.6 Sol").rate == 10


def test_an_unmatched_model_falls_back_and_is_labelled_unverified():
    """Acceptance check 8: an unverified fallback must never look confirmed."""
    c = cat([{"all_of": ["novapro"], "rate": 4}])
    res = c.rate_for("anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert res.rate == 5                       # bundled Claude<=4.7 rule
    assert res.source == "bundled_default"
    assert res.verified is False


def test_an_entry_without_a_verified_date_is_not_presented_as_verified():
    c = cat([{"all_of": ["novapro"], "rate": 7}])
    res = c.rate_for("amazon.nova-pro-v1:0")
    assert res.rate == 7 and res.source == "catalog"
    assert res.verified is False, "no verified_on means we cannot vouch for it"


# --------------------------------------------------------------------------- #
# Acceptance check 6 — history is not rewritten
# --------------------------------------------------------------------------- #
def test_a_future_effective_rate_does_not_apply_to_earlier_dates():
    c = cat([
        {"id": "old", "all_of": ["novapro"], "rate": 2},
        {"id": "new", "all_of": ["novapro"], "rate": 9,
         "effective_from": "2026-09-01"},
    ])
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2026, 8, 31)).rate == 2
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2026, 9, 1)).rate == 9
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2026, 12, 1)).rate == 9


def test_a_dated_entry_supersedes_an_open_ended_one_from_its_start_date():
    c = cat([
        {"id": "always", "all_of": ["novapro"], "rate": 2},
        {"id": "sched", "all_of": ["novapro"], "rate": 5,
         "effective_from": "2026-09-05"},
    ])
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2026, 9, 4)).entry_id == "always"
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2026, 9, 5)).entry_id == "sched"


def test_verified_on_is_never_used_for_date_selection():
    """A doc-verification date is a fact about us, not about when AWS's policy
    began. Using it to select would invent history."""
    c = cat([{"all_of": ["novapro"], "rate": 6, "verified_on": "2026-09-10"}])
    # Verified today, but no effective_from -> applies to old dates too.
    assert c.rate_for("amazon.nova-pro-v1:0", on_date=date(2020, 1, 1)).rate == 6


# --------------------------------------------------------------------------- #
# Acceptance check 7 — endpoint scoping
# --------------------------------------------------------------------------- #
def test_mantle_never_gets_a_burndown_multiplier():
    """Mantle has separate input/output quotas; a multiplier there is fiction,
    whatever the catalog says."""
    c = cat([{"all_of": ["claude"], "rate": 15, "endpoint": "any"}])
    res = c.rate_for("anthropic.claude-opus-4-8", is_mantle=True)
    assert res.rate == 1
    assert res.source == "endpoint_rule"
    assert res.verified is True


def test_a_mantle_only_entry_is_ignored_on_runtime():
    c = cat([{"all_of": ["novapro"], "rate": 8, "endpoint": "mantle"}])
    assert c.rate_for("amazon.nova-pro-v1:0").source == "bundled_default"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad,msg", [
    ("not-a-list", "must be a list"),
    ([{"rate": 5}], "all_of"),
    ([{"all_of": ["x"], "rate": "abc"}], "integer"),
    ([{"all_of": ["x"], "rate": 0}], "between"),
    ([{"all_of": ["x"], "rate": 5000}], "between"),
    ([{"all_of": ["x"], "rate": 5, "endpoint": "bogus"}], "endpoint"),
    ([{"all_of": ["x"], "rate": 5, "effective_from": "not-a-date"}], "effective_from"),
    ([{"all_of": ["x"], "rate": 5, "verified_on": "13/40/2026"}], "verified_on"),
])
def test_invalid_input_is_rejected_with_a_useful_message(bad, msg):
    with pytest.raises(rc.CatalogError) as e:
        rc.validate(bad)
    assert msg in str(e.value)


def test_conflicting_entries_are_rejected_rather_than_ordered_by_luck():
    """Two entries matching the same tokens/endpoint/date would both apply, and
    which one won would depend on list order."""
    with pytest.raises(rc.CatalogError) as e:
        rc.validate([{"all_of": ["claude", "opus5"], "rate": 10},
                     {"all_of": ["opus5", "claude"], "rate": 12}])
    assert "duplicate" in str(e.value)


def test_the_same_tokens_on_different_dates_are_allowed():
    """That is a scheduled change, not a conflict."""
    out = rc.validate([{"all_of": ["x"], "rate": 5},
                       {"all_of": ["x"], "rate": 9, "effective_from": "2026-10-01"}])
    assert len(out) == 2


def test_validation_normalizes_and_defaults():
    out = rc.validate([{"all_of": " claude , opus5 ".split(","), "rate": "10"}])
    assert out[0]["all_of"] == ["claude", "opus5"]
    assert out[0]["rate"] == 10
    assert out[0]["endpoint"] == "runtime"
    assert out[0]["id"] == "claude-opus5"


# --------------------------------------------------------------------------- #
# The bundled seed must agree with the documented table
# --------------------------------------------------------------------------- #
def test_the_bundled_entries_reproduce_the_documented_rates():
    c = cat(rc.BUNDLED_ENTRIES)
    expected = {
        "anthropic.claude-opus-4-8-20260115-v1:0": 15,
        "anthropic.claude-sonnet-5": 10,
        "anthropic.claude-opus-5": 10,
        "anthropic.claude-fable-5-1": 10,
        "openai.gpt-5-6-sol": 10,
        "openai.gpt-5-6-terra": 10,
        "openai.gpt-5-6-luna": 10,
    }
    for mid, rate in expected.items():
        assert c.rate_for(mid).rate == rate, mid
    # And the rules that are NOT per-SKU entries still come from the bundled fn.
    assert c.rate_for("anthropic.claude-sonnet-4-5").rate == 5
    assert c.rate_for("amazon.nova-lite-v1:0").rate == 1


def test_bundled_entries_pass_their_own_validator():
    rc.validate(rc.BUNDLED_ENTRIES)


def test_cris_prefixed_ids_resolve_the_same():
    c = cat(rc.BUNDLED_ENTRIES)
    assert c.rate_for("us.anthropic.claude-opus-4-8-20260115-v1:0").rate == 15
    assert c.rate_for("global.anthropic.claude-sonnet-5").rate == 10


# --------------------------------------------------------------------------- #
# Unmapped-model surfacing (item 4 of the recommendation)
# --------------------------------------------------------------------------- #
def test_unmapped_reports_models_whose_rate_is_an_unverified_assumption():
    c = cat(rc.BUNDLED_ENTRIES)
    seen = ["anthropic.claude-3-5-sonnet-20241022-v2:0",   # bundled 5x, unverified
            "amazon.nova-lite-v1:0",                        # bundled 1x, uninteresting
            "anthropic.claude-opus-5"]                      # catalog entry, fine
    assert c.unmapped(seen) == ["anthropic.claude-3-5-sonnet-20241022-v2:0"]


# --------------------------------------------------------------------------- #
# Snapshot / caching behavior (acceptance check 5's mechanism)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_snapshot_reads_stored_json_and_exposes_a_revision(monkeypatch):
    from datetime import datetime, timezone
    stored = {"revision": 7, "entries": [{"all_of": ["novapro"], "rate": 3}]}

    async def fake_fetchrow(q, *a):
        return {"value": json.dumps(stored),
                "updated_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    monkeypatch.setattr(rc.db, "fetchrow", fake_fetchrow)
    rc.reset_cache()
    snap = await rc.snapshot(force=True)
    assert snap.revision == "7"
    assert snap.rate_for("amazon.nova-pro-v1:0").rate == 3
    assert snap.stale is False


@pytest.mark.asyncio
async def test_a_read_failure_serves_the_last_good_snapshot_not_a_reset(monkeypatch):
    """A transient DB blip must not silently change every quota number on screen."""
    from datetime import datetime, timezone
    good = {"revision": 2, "entries": [{"all_of": ["novapro"], "rate": 6}]}

    async def ok(q, *a):
        return {"value": json.dumps(good),
                "updated_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    monkeypatch.setattr(rc.db, "fetchrow", ok)
    rc.reset_cache()
    first = await rc.snapshot(force=True)
    assert first.rate_for("amazon.nova-pro-v1:0").rate == 6

    async def boom(q, *a):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(rc.db, "fetchrow", boom)
    second = await rc.snapshot(force=True)
    assert second.rate_for("amazon.nova-pro-v1:0").rate == 6
    assert second.stale is True, "a stale snapshot must admit it is stale"


@pytest.mark.asyncio
async def test_corrupt_stored_json_does_not_crash_the_dashboard(monkeypatch):
    from datetime import datetime, timezone

    async def junk(q, *a):
        return {"value": "{not json",
                "updated_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    monkeypatch.setattr(rc.db, "fetchrow", junk)
    rc._last_good = None
    rc.reset_cache()
    snap = await rc.snapshot(force=True)
    assert snap.stale is True
    # Still resolves, via the bundled values.
    assert snap.rate_for("anthropic.claude-opus-4-8").rate == 15


@pytest.mark.asyncio
async def test_no_stored_row_falls_back_to_bundled_and_says_so(monkeypatch):
    async def none(q, *a):
        return None
    monkeypatch.setattr(rc.db, "fetchrow", none)
    rc.reset_cache()
    snap = await rc.snapshot(force=True)
    assert snap.seeded is True
    assert snap.rate_for("openai.gpt-5-6-luna").rate == 10


@pytest.mark.asyncio
async def test_snapshot_is_memoized_so_a_batch_does_not_hammer_the_db(monkeypatch):
    from datetime import datetime, timezone
    calls = {"n": 0}

    async def counting(q, *a):
        calls["n"] += 1
        return {"value": json.dumps({"revision": 1, "entries": []}),
                "updated_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    monkeypatch.setattr(rc.db, "fetchrow", counting)
    rc.reset_cache()
    for _ in range(50):
        await rc.snapshot()
    assert calls["n"] == 1, "500 models must not mean 500 queries"


# --------------------------------------------------------------------------- #
# Wiring: every consumer must go through the catalog, and the arithmetic helper
# must stay free of I/O.
# --------------------------------------------------------------------------- #
def test_the_arithmetic_helper_has_no_database_dependency():
    """Configuration loading is separate from arithmetic on purpose: burndown.py
    stays synchronous and importable with no pool."""
    src = (ROOT / "backend/app/burndown.py").read_text()
    for bad in ("import db", "from . import db", "rate_catalog", "await "):
        assert bad not in src, f"burndown.py must stay pure ({bad})"


@pytest.mark.parametrize("rel", [
    "backend/app/routers/quota_drilldown.py",
    "backend/app/routers/ops_insights.py",
    "backend/app/routers/ops_review.py",
])
def test_quota_consumers_resolve_rates_through_the_catalog(rel):
    src = (ROOT / rel).read_text()
    assert "rate_catalog" in src, f"{rel} still hardcodes its multiplier source"
    assert "rate_for(" in src


def test_saving_rates_invalidates_the_response_cache():
    """Otherwise the admin saves, sees nothing change for a minute, and
    concludes the feature does not work."""
    api = (ROOT / "backend/app/routers/burndown_rates.py").read_text()
    assert "cache.invalidate_all()" in api
    ui = (ROOT / "frontend/src/components/BurndownRateSettings.jsx").read_text()
    assert "clearCache()" in ui, "the frontend holds the real 60s response cache"


def test_the_editor_is_admin_gated_on_the_server_not_only_the_ui():
    src = (ROOT / "backend/app/routers/burndown_rates.py").read_text()
    assert src.count("is_admin(request)") >= 3
    assert '403, "admin access required"' in src


def test_the_ui_separates_verified_from_effective_dates():
    ui = (ROOT / "frontend/src/components/BurndownRateSettings.jsx").read_text()
    assert "effective_from" in ui and "verified_on" in ui
    assert "unmapped_models" in ui, "a new SKU with no rate must be surfaced"


# --------------------------------------------------------------------------- #
# "Restore AWS defaults" must actually restore
# --------------------------------------------------------------------------- #
def test_restore_overwrites_but_first_boot_seeding_does_not():
    """Found live: the seed endpoint delegated to `seed_if_absent()`, which is a
    no-op once any catalog exists. So "Restore AWS defaults" returned the very
    edited catalog the operator was discarding — and reported success.

    The two operations have opposite requirements, so they must stay separate:
    restore ALWAYS writes; first-boot seeding must NEVER clobber real edits on a
    restart or redeploy.
    """
    api = _code_only(ROOT / "backend/app/routers/burndown_rates.py")
    assert "restore_bundled" in api
    assert "seed_if_absent" not in api, (
        "the restore endpoint must not use the absent-only helper")

    src = (ROOT / "backend/app/rate_catalog.py").read_text()
    assert "async def restore_bundled" in src
    # seed_if_absent must keep its early return.
    seed = src.split("async def seed_if_absent")[1]
    assert "if existing:" in seed and "return await snapshot()" in seed

    main = _code_only(ROOT / "backend/app/main.py")
    assert "seed_if_absent" in main, "first boot should seed, not restore"
    assert "restore_bundled" not in main, (
        "a restart must never overwrite an operator's edits")


@pytest.mark.asyncio
async def test_restore_bundled_writes_the_documented_rates(monkeypatch):
    saved = {}

    async def fake_save(entries):
        saved["entries"] = entries
        return cat(entries, revision="9")
    monkeypatch.setattr(rc, "save", fake_save)
    out = await rc.restore_bundled()
    rates = {e["id"]: e["rate"] for e in saved["entries"]}
    assert rates["claude-opus-4-8"] == 15
    assert rates["gpt-5-6-luna"] == 10
    assert out.revision == "9"
    # Stamped with a verification date and the doc URL, but NO invented
    # effective_from (that would re-rate history).
    for e in saved["entries"]:
        assert e["verified_on"]
        assert e["source_url"] == rc.DOC_URL
        assert e.get("effective_from") is None
