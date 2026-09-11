"""One Cost Explorer allocation, computed once per request scope.

Audit findings 05, 06, 07. Cost widgets used three different bases and
lost money in two places:

  05  /cost-by-model returned BEFORE any endpoint slicing whenever CE reported
      a per-model "... (Amazon Bedrock Edition)" service, so the model chart and
      the concentration table showed COMBINED spend under a Mantle headline —
      $96,699.54 of chart against an $11,428.82 headline (8.46x), with a
      concentration row ($24,638 of Nova Lite) exceeding the whole headline.
  06  Two paths dropped charges: any Edition row suppressed the consolidated
      "Amazon Bedrock" service entirely ($30 + $70 -> a $30 chart under a $100
      headline), and the fallback iterated TOKEN rows, so a billed account-day
      with no token rows produced no output bucket at all.
  07  The headline allocated endpoints on whole-window token weights while the
      daily chart allocated per day. Those are not additive: $90 at 90% + $10 at
      10% is $82 by day but $50 by window. Prior-period values also reused the
      CURRENT window's endpoint fraction.

CONTRACT
  * ONE basis: every figure is allocated per (event_date, accountId), then
    aggregated. Whatever a caller groups by, it is summing the same numbers.
  * Money is conserved. For any scope:
        sum(by_model) + unattributed_selected == selected-endpoint total
        sum(by_endpoint.values()) + unattributed == scope_total
  * CE dollars carry no endpoint dimension (AWS does not bill per endpoint), so
    the endpoint split is an ESTIMATE derived from each endpoint's priced-token
    weight. `basis` says so, and callers surface it.
  * Charges with no token basis in their (date, account) are NOT redistributed
    onto other accounts or models: they land in `unattributed`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from . import db
from .filters import FilterSet
from .routers.model_insights import BEDROCK_PRICING

ALLOCATION_BASIS = "per_account_day_priced_token_share"
UNATTRIBUTED_LABEL = "__unattributed__"

# Vendor / marketing words that appear in CE service names but not model ids.
_SERVICE_NOISE = {
    "amazon", "bedrock", "edition", "aws", "anthropic", "meta", "mistral",
    "ai21", "cohere", "stability", "labs", "inc", "the", "for", "model",
    "models", "service", "services", "openai",
}


def _provider_of(model_id: str | None) -> str:
    if not model_id:
        return "other"
    return model_id.split(".")[0].lower() if "." in model_id else "other"


def _priced_weight(in_tok: int, out_tok: int, model_id: str | None) -> float:
    """Dollar-shaped weight for a token bundle, so allocation tracks spend mix
    rather than raw token counts (an embedding call and an Opus call are not
    worth the same per token)."""
    price = BEDROCK_PRICING.get(_provider_of(model_id),
                                {"input": 0.50, "output": 1.50})
    return (in_tok / 1_000_000) * price["input"] + (out_tok / 1_000_000) * price["output"]


def _canon(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def service_model_tokens(service: str) -> list[str]:
    """Distinctive tokens of a CE service name, for matching against modelIds.

    "Claude Sonnet (Amazon Bedrock Edition)" -> ['claude', 'sonnet']
    "Amazon Nova (Amazon Bedrock Edition)"   -> ['nova']
    "Amazon Bedrock"                         -> []   (consolidated: no model)
    """
    cleaned = (service or "").replace("(", " ").replace(")", " ").replace("-", " ")
    toks = [t for t in cleaned.lower().split() if t and t not in _SERVICE_NOISE]
    return toks


def service_matches_model(service_tokens: list[str], model_id: str) -> bool:
    if not service_tokens:
        return False
    mid = _canon(model_id)
    return all(_canon(t) in mid for t in service_tokens)


@dataclass
class CostAllocation:
    """Result of allocating one scope's CE charges."""
    basis: str = ALLOCATION_BASIS
    currency: str = "USD"
    scope_total: float = 0.0
    # Endpoint split of the whole scope. 'unknown' = charges with no token basis.
    by_endpoint: dict = field(default_factory=lambda: {"runtime": 0.0, "mantle": 0.0,
                                                       "unknown": 0.0})
    # Everything below is for the SELECTED endpoint (or the whole scope when the
    # selection is 'all').
    selected_total: float = 0.0
    unattributed: float = 0.0
    by_model: dict = field(default_factory=dict)
    by_date: dict = field(default_factory=dict)
    by_date_model: dict = field(default_factory=dict)
    by_account: dict = field(default_factory=dict)
    by_account_model: dict = field(default_factory=dict)
    # True when at least one charge had to be allocated rather than read
    # directly from a per-model CE service.
    derived: bool = False
    # True when at least one charge could not be attributed to any model.
    has_unattributed: bool = False

    def check_conserved(self, tol: float = 0.01) -> None:
        """Assert money conservation; raises AssertionError on a leak."""
        alloc = sum(self.by_model.values()) + self.unattributed
        assert abs(alloc - self.selected_total) <= tol, (
            f"model allocations {alloc} + unattributed != selected total "
            f"{self.selected_total}")
        ep = sum(self.by_endpoint.values())
        assert abs(ep - self.scope_total) <= tol, (
            f"endpoint split {ep} != scope total {self.scope_total}")


async def allocate(f: FilterSet) -> CostAllocation:
    """Allocate the scope's CE charges once, on the per-(date, account) basis."""
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    where = " AND ".join(parts)

    # 1. Every CE charge in scope — ALL services, not just "Bedrock Edition".
    ce_rows = db.rows_to_dicts(await db.fetch(
        f"""
        SELECT event_date, accountId, service,
               SUM(total_cost)::numeric AS amount,
               MIN(currency) AS currency
        FROM f_daily_cost
        WHERE {where}
        GROUP BY event_date, accountId, service
        """,
        *params,
    ))

    # 2. Priced-token weight per (date, account, endpoint, model).
    tok_rows = db.rows_to_dicts(await db.fetch(
        f"""
        SELECT event_date, accountId, endpoint, modelId,
               SUM(total_input_tokens)::BIGINT  AS in_tok,
               SUM(total_output_tokens)::BIGINT AS out_tok
        FROM f_daily
        WHERE {where}
        GROUP BY event_date, accountId, endpoint, modelId
        """,
        *params,
    ))

    # weights[(date, acct)][(endpoint, model)] = priced weight
    weights: dict[tuple, dict[tuple[str, str], float]] = defaultdict(dict)
    for r in tok_rows:
        d = r["event_date"]
        acct = r.get("accountid") or r.get("accountId")
        mid = r.get("modelid") or r.get("modelId")
        ep = r.get("endpoint") if r.get("endpoint") in ("runtime", "mantle") else "runtime"
        w = _priced_weight(int(r.get("in_tok") or 0), int(r.get("out_tok") or 0), mid)
        if w <= 0:
            continue
        key = (ep, mid)
        weights[(d, acct)][key] = weights[(d, acct)].get(key, 0.0) + w

    sel = f.endpoint if f.endpoint in ("runtime", "mantle") else "all"
    out = CostAllocation()

    for ce in ce_rows:
        d: date = ce["event_date"]
        acct = ce.get("accountid") or ce.get("accountId")
        amount = float(ce["amount"] or 0)
        out.currency = ce.get("currency") or out.currency
        out.scope_total += amount
        if amount == 0:
            continue

        svc_tokens = service_model_tokens(ce.get("service") or "")
        cell = weights.get((d, acct), {})

        # Candidate (endpoint, model) pairs this charge may be attributed to.
        if svc_tokens:
            cands = {k: w for k, w in cell.items()
                     if service_matches_model(svc_tokens, k[1])}
            # A per-model service whose family matched nothing is still real
            # money; fall back to the whole cell so it is not silently dropped,
            # and mark the result as derived.
            if not cands:
                cands = dict(cell)
                out.derived = True
        else:
            # Consolidated / non-model service (e.g. plain "Amazon Bedrock").
            cands = dict(cell)
            out.derived = True

        total_w = sum(cands.values())
        if total_w <= 0:
            # Billed account-day with NO token basis. Previously this money
            # disappeared (the loop iterated token rows). Keep it visible.
            out.by_endpoint["unknown"] += amount
            if sel == "all":
                out.selected_total += amount
                out.unattributed += amount
                out.has_unattributed = True
            continue

        # Endpoint split of this charge, from the candidates' own weights.
        ep_w = defaultdict(float)
        for (ep, _mid), w in cands.items():
            ep_w[ep] += w
        for ep, w in ep_w.items():
            out.by_endpoint[ep] = out.by_endpoint.get(ep, 0.0) + amount * (w / total_w)

        # Selected-endpoint portion, then split across that endpoint's models.
        if sel == "all":
            sel_w = total_w
            sel_cands = cands
        else:
            sel_cands = {k: w for k, w in cands.items() if k[0] == sel}
            sel_w = sum(sel_cands.values())
        if sel_w <= 0:
            continue
        sel_amount = amount * (sel_w / total_w)
        out.selected_total += sel_amount
        out.by_date[d] = out.by_date.get(d, 0.0) + sel_amount
        out.by_account[acct] = out.by_account.get(acct, 0.0) + sel_amount
        for (_ep, mid), w in sel_cands.items():
            part = sel_amount * (w / sel_w)
            out.by_model[mid] = out.by_model.get(mid, 0.0) + part
            out.by_date_model[(d, mid)] = out.by_date_model.get((d, mid), 0.0) + part
            out.by_account_model[(acct, mid)] = \
                out.by_account_model.get((acct, mid), 0.0) + part

    return out
