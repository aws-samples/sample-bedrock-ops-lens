"""Cost allocation contract: one basis, money conserved.

Audit findings 05, 06, 07. Each test states a hand-computed expectation
from the audit's own counterexamples and drives the real `app.cost_alloc.allocate`
with a stubbed database, so the arithmetic under test is production code.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app import cost_alloc  # noqa: E402
from app.filters import FilterSet  # noqa: E402

D1, D2 = date(2026, 9, 8), date(2026, 9, 9)
ACCT_A, ACCT_B = "111111111111", "222222222222"
SONNET = "anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA = "amazon.nova-lite-v1:0"


def _fs(endpoint="all", accounts=None, start=D1, end=D2) -> FilterSet:
    return FilterSet(start=start, end=end, provider="all", region="all",
                     accounts=tuple(accounts or ()), traffic_type="all",
                     tag_filter=(), endpoint=endpoint)


class _FakeDB:
    """Serves the two queries `allocate` runs, in order: CE rows then token rows."""

    def __init__(self, ce_rows, tok_rows):
        self.ce_rows, self.tok_rows = ce_rows, tok_rows
        self.n = 0

    async def fetch(self, sql, *params):
        self.n += 1
        return self.ce_rows if "f_daily_cost" in sql else self.tok_rows

    @staticmethod
    def rows_to_dicts(rows):
        return rows


def _install(monkeypatch, ce_rows, tok_rows):
    fake = _FakeDB(ce_rows, tok_rows)
    monkeypatch.setattr(cost_alloc, "db", fake)
    return fake


def ce(d, acct, service, amount, currency="USD"):
    return {"event_date": d, "accountId": acct, "service": service,
            "amount": amount, "currency": currency}


def tok(d, acct, endpoint, model, in_tok, out_tok):
    return {"event_date": d, "accountId": acct, "endpoint": endpoint,
            "modelId": model, "in_tok": in_tok, "out_tok": out_tok}


# --------------------------------------------------------------------------- #
# Finding 06 — no money may disappear
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_edition_service_no_longer_suppresses_consolidated_charges(monkeypatch):
    """$30 Claude Edition + $70 consolidated 'Amazon Bedrock' = $100.

    The old code returned as soon as it found ANY Edition row, so the chart
    showed $30 under a $100 headline. Both must now be present."""
    _install(monkeypatch,
             [ce(D1, ACCT_A, "Claude Sonnet (Amazon Bedrock Edition)", 30),
              ce(D1, ACCT_A, "Amazon Bedrock", 70)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 100_000)])
    a = await cost_alloc.allocate(_fs())
    assert a.scope_total == pytest.approx(100.0)
    assert sum(a.by_model.values()) == pytest.approx(100.0)
    a.check_conserved()


@pytest.mark.asyncio
async def test_billed_account_day_without_token_rows_is_not_dropped(monkeypatch):
    """$100 CE cost and no token rows at all: the old fallback iterated token
    rows, so the chart was EMPTY while the headline said $100. The money must
    now surface as explicitly unattributed."""
    _install(monkeypatch, [ce(D1, ACCT_A, "Amazon Bedrock", 100)], [])
    a = await cost_alloc.allocate(_fs())
    assert a.scope_total == pytest.approx(100.0)
    assert a.by_model == {}
    assert a.unattributed == pytest.approx(100.0)
    assert a.has_unattributed is True
    assert a.by_endpoint["unknown"] == pytest.approx(100.0)
    a.check_conserved()


@pytest.mark.asyncio
async def test_zero_token_models_do_not_swallow_or_lose_cost(monkeypatch):
    """A model with zero tokens carries zero weight, so cost goes to the models
    that actually have weight — and nothing is lost."""
    _install(monkeypatch, [ce(D1, ACCT_A, "Amazon Bedrock", 50)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0),
              tok(D1, ACCT_A, "runtime", NOVA, 0, 0)])
    a = await cost_alloc.allocate(_fs())
    assert a.by_model.get(NOVA) is None
    assert a.by_model[SONNET] == pytest.approx(50.0)
    a.check_conserved()


# --------------------------------------------------------------------------- #
# Finding 05 — endpoint scope must actually scope
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_endpoint_selection_changes_the_model_chart(monkeypatch):
    """Sonnet on runtime and Nova on mantle, equal priced weight is not assumed:
    weights are computed from tokens. The runtime slice must exclude the mantle
    model entirely, and the two slices must sum to the scope total.

    The audit found all three endpoint values returning the same 35 rows and the
    same $96,699.54."""
    ce_rows = [ce(D1, ACCT_A, "Amazon Bedrock", 100)]
    tok_rows = [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0),
                tok(D1, ACCT_A, "mantle", NOVA, 1_000_000, 0)]
    _install(monkeypatch, ce_rows, tok_rows)
    all_a = await cost_alloc.allocate(_fs("all"))
    _install(monkeypatch, ce_rows, tok_rows)
    rt = await cost_alloc.allocate(_fs("runtime"))
    _install(monkeypatch, ce_rows, tok_rows)
    mt = await cost_alloc.allocate(_fs("mantle"))

    assert set(all_a.by_model) == {SONNET, NOVA}
    assert set(rt.by_model) == {SONNET}, "runtime slice must not contain a mantle model"
    assert set(mt.by_model) == {NOVA}
    # The two endpoint slices partition the scope total.
    assert rt.selected_total + mt.selected_total == pytest.approx(all_a.scope_total)
    for a in (all_a, rt, mt):
        a.check_conserved()


@pytest.mark.asyncio
async def test_no_model_row_can_exceed_the_endpoint_total(monkeypatch):
    """The audit's smoking gun: a Mantle concentration row of $24,638 under an
    $11,428 Mantle headline. No single model may exceed the selected total."""
    _install(monkeypatch, [ce(D1, ACCT_A, "Amazon Bedrock", 1000)],
             [tok(D1, ACCT_A, "runtime", SONNET, 9_000_000, 0),
              tok(D1, ACCT_A, "mantle", NOVA, 1_000_000, 0)])
    mt = await cost_alloc.allocate(_fs("mantle"))
    assert mt.by_model
    for model, amt in mt.by_model.items():
        assert amt <= mt.selected_total + 0.01, f"{model} exceeds the endpoint total"


# --------------------------------------------------------------------------- #
# Finding 07 — one grain; per-day and window views must agree
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_daily_sum_equals_window_total(monkeypatch):
    """The audit's counterexample: day 1 = $90 at 90% runtime, day 2 = $10 at
    10% runtime. Per-day allocation gives $81 + $1 = $82; whole-window weights
    gave $50. With ONE per-day basis the daily figures must sum to the headline,
    so both views show $82."""
    ce_rows = [ce(D1, ACCT_A, "Amazon Bedrock", 90),
               ce(D2, ACCT_A, "Amazon Bedrock", 10)]
    # Day 1: 90% of weight on runtime. Day 2: 10% on runtime.
    tok_rows = [
        tok(D1, ACCT_A, "runtime", SONNET, 9_000_000, 0),
        tok(D1, ACCT_A, "mantle", SONNET, 1_000_000, 0),
        tok(D2, ACCT_A, "runtime", SONNET, 1_000_000, 0),
        tok(D2, ACCT_A, "mantle", SONNET, 9_000_000, 0),
    ]
    _install(monkeypatch, ce_rows, tok_rows)
    rt = await cost_alloc.allocate(_fs("runtime"))
    assert rt.by_date[D1] == pytest.approx(81.0)
    assert rt.by_date[D2] == pytest.approx(1.0)
    assert sum(rt.by_date.values()) == pytest.approx(rt.selected_total)
    assert rt.selected_total == pytest.approx(82.0), (
        "headline must equal the sum of the daily values, not a window-weighted 50")
    rt.check_conserved()


@pytest.mark.asyncio
async def test_multiple_accounts_are_allocated_independently(monkeypatch):
    """Account A's charges must never be allocated using account B's token mix."""
    _install(monkeypatch,
             [ce(D1, ACCT_A, "Amazon Bedrock", 100),
              ce(D1, ACCT_B, "Amazon Bedrock", 100)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0),
              tok(D1, ACCT_B, "mantle", NOVA, 1_000_000, 0)])
    rt = await cost_alloc.allocate(_fs("runtime"))
    # Only account A has runtime usage, so only its $100 lands in the runtime slice.
    assert rt.selected_total == pytest.approx(100.0)
    assert set(rt.by_account) == {ACCT_A}
    assert rt.by_endpoint["mantle"] == pytest.approx(100.0)
    assert rt.scope_total == pytest.approx(200.0)
    rt.check_conserved()


@pytest.mark.asyncio
async def test_unmatched_edition_service_is_kept_and_marked_derived(monkeypatch):
    """A per-model CE service whose family matches no observed model is still
    real money: allocate it across the account-day rather than dropping it."""
    _install(monkeypatch,
             [ce(D1, ACCT_A, "Llama 3 (Amazon Bedrock Edition)", 40)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0)])
    a = await cost_alloc.allocate(_fs())
    assert a.scope_total == pytest.approx(40.0)
    assert sum(a.by_model.values()) == pytest.approx(40.0)
    assert a.derived is True
    a.check_conserved()


@pytest.mark.asyncio
async def test_edition_service_attributes_to_its_own_family(monkeypatch):
    """'Claude Sonnet (Amazon Bedrock Edition)' must not be spread onto Nova."""
    _install(monkeypatch,
             [ce(D1, ACCT_A, "Claude Sonnet (Amazon Bedrock Edition)", 60)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0),
              tok(D1, ACCT_A, "runtime", NOVA, 9_000_000, 0)])
    a = await cost_alloc.allocate(_fs())
    assert a.by_model[SONNET] == pytest.approx(60.0)
    assert NOVA not in a.by_model
    a.check_conserved()


@pytest.mark.asyncio
async def test_negative_and_zero_charges_are_tolerated(monkeypatch):
    """CE can report credits/refunds. They must flow through, not crash or be
    silently dropped."""
    _install(monkeypatch,
             [ce(D1, ACCT_A, "Amazon Bedrock", 100),
              ce(D2, ACCT_A, "Amazon Bedrock", -25),
              ce(D2, ACCT_A, "Amazon Bedrock Credit", 0)],
             [tok(D1, ACCT_A, "runtime", SONNET, 1_000_000, 0),
              tok(D2, ACCT_A, "runtime", SONNET, 1_000_000, 0)])
    a = await cost_alloc.allocate(_fs())
    assert a.scope_total == pytest.approx(75.0)
    assert sum(a.by_model.values()) == pytest.approx(75.0)
    a.check_conserved()
