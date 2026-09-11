"""One canonical Service Quotas lookup, shared by every consumer.

the audit audit finding 03. Two widgets resolved "the applied TPM limit" for the
same (account, region, model) and disagreed:

  burndown risk   max() across every fuzzy name match  -> 14,959,243 (Global CRIS)
  quota drilldown first match, labelled On-demand      ->  6,460,459.81

Taking the maximum is the worst option: it silently picks the most generous
denominator, so 90 TPM against an applicable 100 TPM limit (90%) can be
reported as 9% of some other traffic family's 1,000 TPM limit, hiding pressure.

The honest constraint: `f_hourly_peak` carries no traffic-family dimension
(its key is date/hour/account/model/region/endpoint), so for hourly peak
utilization we genuinely cannot prove which family a given hour's calls used.
This module therefore returns the whole candidate set plus a deterministic
primary, and marks the result AMBIGUOUS when more than one family could apply.
Callers must surface that ambiguity rather than silently choosing.

Selection rule for the primary, in order:
  1. the caller's explicit family hint, when present in the candidates
  2. "On-demand" — the default inference path, and what the drill-down labels
  3. the single candidate, when only one exists
  4. otherwise the LOWEST limit, so an ambiguous utilization errs toward
     over-reporting pressure instead of hiding it
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Families as they appear in f_quotas.traffic_type.
FAMILY_ON_DEMAND = "On-demand"
FAMILY_CROSS_REGION = "Cross-region"
FAMILY_GLOBAL_CRIS = "Global cross-region"

_PRIMARY_PREFERENCE = (FAMILY_ON_DEMAND, FAMILY_CROSS_REGION, FAMILY_GLOBAL_CRIS)

# Geo prefixes that mark a cross-region inference profile.
_CRIS_PREFIXES = ("us", "eu", "apac", "jp", "au", "ca", "amer", "us-gov")


def family_hint_from_model_id(model_id: str | None) -> str | None:
    """Infer the traffic family from a model id's inference-profile prefix.

    A CRIS-prefixed id is direct evidence of the routing family, so this turns
    an otherwise-ambiguous quota lookup into an exact one:

        global.anthropic.claude-...  -> Global cross-region
        us.anthropic.claude-...      -> Cross-region
        anthropic.claude-...         -> On-demand (no profile prefix)

    Returns None when the id is missing. A bare (unprefixed) id is reported as
    On-demand because that is what calling a model without an inference profile
    means; callers that cannot trust that should pass family_hint=None.
    """
    if not model_id:
        return None
    mid = model_id.lower()
    if mid.startswith("global."):
        return FAMILY_GLOBAL_CRIS
    for p in _CRIS_PREFIXES:
        if mid.startswith(p + "."):
            return FAMILY_CROSS_REGION
    return FAMILY_ON_DEMAND


@dataclass
class QuotaCandidate:
    family: str
    value: float
    quota_code: str | None = None
    model_name: str | None = None
    # 2 = the quota name states this model's version; 1 = a generic name.
    rank: int = 2
    # True when another same-rank quota in this family had a DIFFERENT value.
    collides: bool = False


@dataclass
class QuotaResolution:
    """Result of resolving one (account, region, model, metric) quota."""
    value: float | None = None            # primary limit, per minute
    family: str | None = None             # traffic family of `value`
    quota_code: str | None = None
    ambiguous: bool = False               # >1 family could apply
    candidates: list[QuotaCandidate] = field(default_factory=list)
    # True when the model id NAMED a family (a `us.`/`eu.`/`global.` CRIS prefix)
    # that has no published quota in this account+region. The limit is unknown;
    # another family's limit is deliberately not substituted.
    family_missing: bool = False

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def range(self) -> tuple[float, float] | None:
        """(min, max) across candidate families, for surfacing uncertainty."""
        if not self.candidates:
            return None
        vals = [c.value for c in self.candidates]
        return (min(vals), max(vals))

    def as_dict(self) -> dict:
        lo_hi = self.range
        return {
            "limit_per_minute": self.value,
            "quota_family": self.family,
            "quota_code": self.quota_code,
            "quota_ambiguous": self.ambiguous,
            "quota_candidate_families": [
                {"family": c.family, "limit_per_minute": c.value}
                for c in sorted(self.candidates, key=lambda c: c.value)
            ],
            "quota_limit_range": {"min": lo_hi[0], "max": lo_hi[1]} if lo_hi else None,
        }
    # True when the model id named a family (CRIS prefix) that has no
    # published quota in this account+region. The limit is unknown; another
    # family's limit is deliberately NOT substituted.
    family_missing: bool = False


def resolve_quota(rows,
                  account: str,
                  region: str,
                  model_id: str,
                  metric: str = "TPM",
                  family_hint: str | None = None,
                  matcher=None) -> QuotaResolution:
    """Resolve the applicable per-minute quota from pre-fetched f_quotas rows.

    `rows` are dicts with accountid/accountId, region, model_name, metric,
    traffic_type, applied_value, default_value, quota_code. `matcher` is the
    model_name<->modelId comparison (defaults to the drill-down's `_matches`).
    Applied value is preferred; the AWS-published default is the fallback and is
    still a real limit, so it counts as known.
    """
    ranker = None
    if matcher is None:
        # Canonical identity, NOT substring matching, and RANKED. Substring
        # matching let "Claude Sonnet 4" match anthropic.claude-sonnet-4-5 (a
        # version is a substring of a longer version). Ranking additionally
        # separates a version-specific quota name from a generic one, which is
        # what round 3 showed matters: AWS publishes both "Mistral Large 2407"
        # and "Mistral AI Mistral Large", and treating them as equivalent let the
        # 2402 model inherit the 2407 limit (160% -> 8%).
        from .model_identity import match_rank as ranker
        matcher = lambda name, mid: ranker(name, mid) > 0  # noqa: E731

    by_family: dict[str, QuotaCandidate] = {}
    for q in rows:
        acct = q.get("accountid") or q.get("accountId")
        if acct != account or q.get("region") != region:
            continue
        if (q.get("metric") or "") != metric:
            continue
        rank = ranker(q.get("model_name") or "", model_id) if ranker else (
            2 if matcher(q.get("model_name") or "", model_id) else 0)
        if not rank:
            continue
        raw = q.get("applied_value")
        if raw is None:
            raw = q.get("default_value")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        family = q.get("traffic_type") or "Unknown"
        # Prefer an EXACT (version-specific) name over a generic one. Within the
        # same rank, a differing value is a genuine collision - two distinct
        # quotas that both look applicable - and round 3 showed that keeping the
        # larger one is how a 50K Mistral 24.02 limit became the 1M 24.07 limit.
        # Record the collision instead; the caller reports it as ambiguous.
        prev = by_family.get(family)
        if prev is None or rank > prev.rank:
            by_family[family] = QuotaCandidate(
                family=family, value=val, rank=rank,
                quota_code=q.get("quota_code"),
                model_name=q.get("model_name"),
            )
        elif rank == prev.rank and val != prev.value:
            prev.collides = True

    candidates = list(by_family.values())
    if not candidates:
        return QuotaResolution()

    primary: QuotaCandidate | None = None
    if family_hint:
        # The model id NAMED its family (a `us.` / `eu.` / `global.` prefix is
        # not a hint we may override). If that family has no published quota,
        # the answer is UNKNOWN.
        #
        # This used to fall through to the preference order, so a `us.anthropic…`
        # Cross-region model whose account only had an On-demand entry silently
        # received the On-demand limit with ambiguous=False - a confident number
        # from the wrong quota. Cross-region and On-demand limits differ, so that
        # is a wrong utilization, not a rounding difference.
        if family_hint not in by_family:
            return QuotaResolution(
                candidates=candidates,
                family=family_hint,
                family_missing=True,
            )
        primary = by_family[family_hint]
    elif len(candidates) == 1:
        primary = candidates[0]
    else:
        for fam in _PRIMARY_PREFERENCE:
            if fam in by_family:
                primary = by_family[fam]
                break
        if primary is None:
            primary = min(candidates, key=lambda c: c.value)

    if primary.collides:
        # Two distinct quotas in this family both match this model and disagree.
        # Reporting either is a guess; reporting the larger understates risk.
        return QuotaResolution(
            family=primary.family, candidates=candidates, ambiguous=True)
    return QuotaResolution(
        value=primary.value,
        family=primary.family,
        quota_code=primary.quota_code,
        # Ambiguous whenever more than one family could apply AND the caller
        # could not tell us which one the traffic actually used.
        ambiguous=(
            (len(candidates) > 1 and not (family_hint and family_hint in by_family))
            or primary.collides),
        candidates=candidates,
    )
