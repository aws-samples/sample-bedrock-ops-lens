"""Quota identity, family and peak selection — Audit follow-up round 2.

Three defects survived the first consolidation. Each one had a executed
counterexample; each one is pinned here, including the explicit requirement that
a GENUINE breach survives selection. "Zero rows above 100%" is not an acceptance
check - the first defect made a real 160% breach disappear, which would have
satisfied that assertion.

Run: .venv/bin/python -m pytest tests/test_quota_identity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app import proxy_quota as pq  # noqa: E402
from app.model_identity import identity, matches  # noqa: E402
from app.quota_match import family_hint_from_model_id, resolve_quota  # noqa: E402

ACCT_A, ACCT_B = "111111111111", "222222222222"
REGION = "us-east-1"


def q(acct, region, name, limit, traffic="On-demand"):
    return {"accountId": acct, "region": region, "model_name": name,
            "traffic_type": traffic, "metric": "TPM", "applied_value": limit,
            "default_value": limit, "quota_code": f"L-{abs(hash(name)) % 9999}"}


@pytest.fixture()
def stub_quota_rows(monkeypatch):
    def install(rows):
        async def fake():
            return rows
        monkeypatch.setattr(pq, "load_tpm_quota_rows", fake)
    return install


def ev(value, model, account, region=REGION, endpoint="runtime", tpm=0):
    """One hour of traffic at `tpm` tokens/minute."""
    return {"dim_value": value, "modelId": model, "endpoint": endpoint,
            "region": region, "accountId": account,
            "input_tokens": int(tpm * 60), "output_tokens": 0}


# --------------------------------------------------------------------------- #
# Defect 1 — the worst UTILIZATION must win, not the worst traffic
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_real_breach_survives_selection(stub_quota_rows):
    """Executed counterexample: same workload and model, two accounts.

        account A: 100,000 TPM / 1,000,000 limit =  10%
        account B:  80,000 TPM /    50,000 limit = 160%   <- a real breach

    Keying the peak on (value, model) alone let A win on traffic, and the limit
    was then resolved for A. The output reported 10% and B's breach vanished.
    """
    stub_quota_rows([q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000),
                     q(ACCT_B, REGION, "Claude Sonnet 5", 50_000)])
    out = await pq.score([
        ev("w1", "anthropic.claude-sonnet-5", ACCT_A, tpm=100_000),
        ev("w1", "anthropic.claude-sonnet-5", ACCT_B, tpm=80_000),
    ])
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["utilization_pct"] == pytest.approx(160.0), (
        "the breach must be the reported figure, not the higher-traffic row")
    assert row["accountId"] == ACCT_B
    assert row["tpm_limit"] == 50_000
    assert out["breach_count"] == 1, "a breach must be counted, not hidden"


@pytest.mark.asyncio
async def test_every_account_candidate_stays_inspectable(stub_quota_rows):
    """Selecting one row per value must not delete the others from the payload."""
    stub_quota_rows([q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000),
                     q(ACCT_B, REGION, "Claude Sonnet 5", 50_000)])
    out = await pq.score([
        ev("w1", "anthropic.claude-sonnet-5", ACCT_A, tpm=100_000),
        ev("w1", "anthropic.claude-sonnet-5", ACCT_B, tpm=80_000),
    ])
    utils = sorted(c["utilization_pct"] for c in out["all_candidates"])
    assert utils == [pytest.approx(10.0), pytest.approx(160.0)]


@pytest.mark.asyncio
async def test_regions_are_scored_separately(stub_quota_rows):
    """A limit is per region too, so the same account in two regions is two
    different utilizations."""
    stub_quota_rows([q(ACCT_A, "us-east-1", "Claude Sonnet 5", 1_000_000),
                     q(ACCT_A, "eu-west-1", "Claude Sonnet 5", 40_000)])
    out = await pq.score([
        ev("w1", "anthropic.claude-sonnet-5", ACCT_A, "us-east-1", tpm=100_000),
        ev("w1", "anthropic.claude-sonnet-5", ACCT_A, "eu-west-1", tpm=60_000),
    ])
    assert out["rows"][0]["region"] == "eu-west-1"
    assert out["rows"][0]["utilization_pct"] == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_an_unknown_limit_never_displaces_a_real_breach(stub_quota_rows):
    stub_quota_rows([q(ACCT_A, REGION, "Claude Sonnet 5", 50_000)])
    out = await pq.score([
        ev("w1", "anthropic.claude-sonnet-5", ACCT_A, tpm=80_000),
        ev("w1", "meta.llama3-1-70b-instruct-v1:0", ACCT_A, tpm=9_000_000),
    ])
    assert out["rows"][0]["utilization_pct"] == pytest.approx(160.0)


@pytest.mark.asyncio
async def test_zero_breaches_is_only_reported_when_there_are_none(stub_quota_rows):
    """The complement of the first test: a genuinely healthy fleet must report 0,
    so `breach_count` means something."""
    stub_quota_rows([q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000)])
    out = await pq.score([ev("w1", "anthropic.claude-sonnet-5", ACCT_A, tpm=100_000)])
    assert out["breach_count"] == 0
    assert out["rows"][0]["utilization_pct"] == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Defect 2 — version boundaries in model identity
# --------------------------------------------------------------------------- #
SONNET45 = "anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_a_shorter_version_is_not_a_match():
    """Executed counterexample: "Claude Sonnet 4" matched a Sonnet 4.5 id because
    "4" is a substring of "4-5". With limits of 1,000,000 and 50,000, 80,000 TPM
    reported 8% instead of 160%."""
    assert matches("Claude Sonnet 4.5", SONNET45) is True
    assert matches("Claude Sonnet 4", SONNET45) is False
    assert matches("Claude Sonnet 5", SONNET45) is False


def test_the_wrong_model_family_is_not_a_match():
    assert matches("Claude Haiku 4.5", SONNET45) is False
    assert matches("Amazon Nova Lite", "amazon.nova-pro-v1:0") is False


def test_the_version_boundary_counterexample_resolves_to_the_right_limit():
    rows = [q(ACCT_A, REGION, "Claude Sonnet 4", 1_000_000),
            q(ACCT_A, REGION, "Claude Sonnet 4.5", 50_000)]
    res = resolve_quota(rows, ACCT_A, REGION, SONNET45, "TPM", None)
    assert res.value == 50_000
    assert 80_000 / res.value * 100 == pytest.approx(160.0)


@pytest.mark.parametrize("name,model_id", [
    ("Claude Sonnet 5", "us.anthropic.claude-sonnet-5"),
    ("Claude Opus 4.8", "anthropic.claude-opus-4-8"),
    ("Claude 3.7 Sonnet", "us.anthropic.claude-3-7-sonnet-20250219-v1:0"),
    ("Claude Haiku 4.5", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("Amazon Nova Lite", "amazon.nova-lite-v1:0"),
    ("Amazon Nova Pro", "amazon.nova-pro-v1:0"),
    ("GPT-5.6 Sol", "openai.gpt-5-6-sol"),
    # A CRIS prefix and a revision suffix are not identity.
    ("Claude Sonnet 4.5", "global.anthropic.claude-sonnet-4-5-20250929-v2:0"),
])
def test_legitimate_pairs_still_match(name, model_id):
    assert matches(name, model_id) is True, f"{name} should match {model_id}"


def test_identity_is_words_version_and_revision():
    """Round 3 added a third component: a trailing -vN[:M] is a REVISION when the
    name already carries a version, and IS the version when nothing else does."""
    assert identity("Claude Sonnet 4.5") == (("claude", "sonnet"), (4, 5), ())
    assert identity(SONNET45) == (("claude", "sonnet"), (4, 5), (1,))
    assert identity("Claude Sonnet 4") == (("claude", "sonnet"), (4,), ())
    # A trailing .0 is not a distinct version.
    assert identity("Claude Sonnet 4.0") == identity("Claude Sonnet 4")
    # The revision carries the generation when nothing else does.
    assert identity("cohere.embed-v4:0") == (("embed",), (4,), ())
    assert identity("Cohere Embed V4") == (("embed",), (4,), ())
    # Real Mistral SKUs keep their YYMM versions.
    assert identity("mistral.mistral-large-2402-v1:0") == (("large",), (2402,), (1,))
    assert identity("Mistral Large 2407") == (("large",), (2407,), ())


def test_traffic_family_words_are_not_model_identity():
    """Quota names sometimes carry the family; that dimension is traffic_type and
    must not leak into the model comparison."""
    assert matches("Claude Sonnet 5 (cross-region)", "anthropic.claude-sonnet-5")


def test_the_drilldown_and_the_resolver_share_one_matcher():
    """Two matchers meant the drill-down and the burndown table could disagree
    about which quota a row belongs to."""
    from app.routers.quota_drilldown import _matches
    assert _matches("Claude Sonnet 4", SONNET45) is False
    assert _matches("Claude Sonnet 4.5", SONNET45) is True
    src = (ROOT / "backend/app/routers/quota_drilldown.py").read_text()
    assert "from ..model_identity import matches" in src


# --------------------------------------------------------------------------- #
# Defect 3 — a family named by the model id must not borrow another family
# --------------------------------------------------------------------------- #
CRIS_MODEL = "us.anthropic.claude-sonnet-5"


def test_a_cris_model_does_not_inherit_the_on_demand_limit():
    """Executed counterexample: a `us.` Cross-region model with only an On-demand
    entry received the On-demand limit with quota_ambiguous=false - a confident
    number from the wrong quota."""
    assert family_hint_from_model_id(CRIS_MODEL) == "Cross-region"
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand")]
    res = resolve_quota(rows, ACCT_A, REGION, CRIS_MODEL, "TPM",
                        family_hint_from_model_id(CRIS_MODEL))
    assert res.value is None, "the On-demand limit must not be substituted"
    assert res.family_missing is True
    assert res.family == "Cross-region"


def test_the_named_family_is_used_when_it_exists():
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand"),
            q(ACCT_A, REGION, "Claude Sonnet 5", 2_000_000, "Cross-region")]
    res = resolve_quota(rows, ACCT_A, REGION, CRIS_MODEL, "TPM",
                        family_hint_from_model_id(CRIS_MODEL))
    assert res.value == 2_000_000
    assert res.family == "Cross-region"
    assert res.ambiguous is False


def test_a_global_prefix_selects_the_global_family():
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand"),
            q(ACCT_A, REGION, "Claude Sonnet 5", 3_000_000, "Global cross-region")]
    mid = "global.anthropic.claude-sonnet-5"
    res = resolve_quota(rows, ACCT_A, REGION, mid, "TPM",
                        family_hint_from_model_id(mid))
    assert res.value == 3_000_000
    assert res.family == "Global cross-region"


def test_an_unprefixed_model_names_the_on_demand_family():
    """The absence of a CRIS prefix is itself an identification: the call went to
    the model directly, so On-demand is the family - not a guess among families.
    The On-demand limit is therefore used and the result is NOT ambiguous, even
    though a Cross-region entry also exists for the same model."""
    mid = "anthropic.claude-sonnet-5"
    assert family_hint_from_model_id(mid) == "On-demand"
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand"),
            q(ACCT_A, REGION, "Claude Sonnet 5", 2_000_000, "Cross-region")]
    res = resolve_quota(rows, ACCT_A, REGION, mid, "TPM",
                        family_hint_from_model_id(mid))
    assert res.value == 1_000_000
    assert res.family == "On-demand"
    assert res.ambiguous is False


def test_ambiguity_is_reserved_for_a_caller_that_cannot_name_the_family():
    """`ambiguous` must mean "more than one family could apply and nobody could
    say which" - which is what happens when no hint is supplied at all."""
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand"),
            q(ACCT_A, REGION, "Claude Sonnet 5", 2_000_000, "Cross-region")]
    res = resolve_quota(rows, ACCT_A, REGION, "anthropic.claude-sonnet-5", "TPM",
                        None)
    assert res.ambiguous is True
    assert res.value == 1_000_000, "the preference order still prefers On-demand"


def test_an_unprefixed_model_with_only_a_cris_entry_is_unknown():
    """The mirror of the CRIS case: an On-demand call must not borrow the
    Cross-region ceiling either."""
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 2_000_000, "Cross-region")]
    mid = "anthropic.claude-sonnet-5"
    res = resolve_quota(rows, ACCT_A, REGION, mid, "TPM",
                        family_hint_from_model_id(mid))
    assert res.value is None
    assert res.family_missing is True


def test_the_missing_family_reason_is_stated_for_the_ui():
    rows = [q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand")]
    v = pq.resolve(rows, CRIS_MODEL, REGION, ACCT_A)
    assert v.limit is None
    assert v.family_missing is True
    assert "Cross-region" in v.reason
    assert "not substituted" in v.reason


@pytest.mark.asyncio
async def test_scoring_reports_a_missing_family_as_unknown(stub_quota_rows):
    stub_quota_rows([q(ACCT_A, REGION, "Claude Sonnet 5", 1_000_000, "On-demand")])
    out = await pq.score([ev("w1", CRIS_MODEL, ACCT_A, tpm=900_000)])
    row = out["rows"][0]
    assert row["utilization_pct"] is None
    assert row["tpm_limit"] is None
    assert row["quota_family_missing"] is True
    assert row["limit_unknown_reason"]
