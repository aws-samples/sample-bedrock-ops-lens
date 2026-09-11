"""One quota resolver for proxy/client-telemetry quota utilization.

Audit follow-up to T08. Two endpoints computed "TPM quota utilization per
workload" from f_proxy_dim_hourly with two separate, differently-broken copies of
the lookup, and the Workloads tab called the one that had not been fixed:

    /attribution/quota      <- WorkloadsTab.jsx (was entirely unfixed)
    /workload-usage/quota   <- fixed for accounts + direct-provider, but kept the
                               same broken model matcher

Both matchers accepted a quota row if ANY distinctive token of its model_name
appeared in the model id, then took min() across the matches. Since every Claude
quota name contains "claude", a Sonnet model matched the Haiku row and inherited
the SMALLER limit:

    Sonnet peak 100,000 TPM, Sonnet limit 1,000,000  -> should read 10%
    borrowed the 50,000 Haiku limit                  -> read 200%

Both also fell back to `min(limit for every quota in the region)` when nothing
matched, so an unrecognised model silently inherited the smallest ceiling in the
region - and direct-provider traffic, which consumes no AWS quota at all, was
scored against AWS limits.

This module is the single implementation. Rules:

  * The model match is `model_identity.matches`, which compares canonical
    (words, version) identities. Requiring every token to appear as a SUBSTRING
    was still substring matching: "Claude Sonnet 4" matched a Sonnet 4.5 id,
    because "4" is a substring of "4-5".
  * A family named by the model id (a `us.`/`eu.`/`global.` CRIS prefix) is not
    a hint that may be overridden. If that family has no published quota, the
    limit is unknown - On-demand and Cross-region ceilings differ.
  * Quotas are per (account, region, model, family), so the account is part of
    the key. Proxy events frequently carry no account; in that case we resolve
    across accounts and accept the limit only if every candidate agrees. If they
    disagree we report the utilization as UNKNOWN rather than picking one.
  * No match, no limit: `tpm_limit` and `utilization_pct` are None. We never
    borrow another model's or another region's ceiling.
  * Direct-provider endpoints (anthropic-api / openai-api / unknown) are not
    scored at all. They come back in their own list with a null limit.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from . import db
from .burndown import output_burndown_rate
from .quota_match import family_hint_from_model_id, resolve_quota
from .units import hourly_total_to_per_minute

# Endpoints AWS bills and enforces quotas for. Everything else in the proxy
# stream goes straight to a provider (or is an unrecognised path), so no AWS
# quota applies to it.
AWS_BILLED_ENDPOINTS = ("runtime", "mantle")

QUOTA_SOURCE_AWS = "aws_service_quotas"
QUOTA_SOURCE_PROVIDER = "provider"


@dataclass
class QuotaVerdict:
    limit: float | None = None
    family: str | None = None
    account_known: bool = False
    model_matched: bool = False
    # True when the account was unknown AND candidate limits disagreed, so no
    # single ceiling can be attributed.
    ambiguous: bool = False
    # True when the model id NAMED its family (a CRIS prefix) but that family has
    # no published quota here, so another family's limit would be the wrong one.
    family_missing: bool = False

    @property
    def known(self) -> bool:
        return self.limit is not None

    @property
    def reason(self) -> str:
        """Why the limit is unknown, in the words the UI shows."""
        if self.known:
            return ""
        if self.family_missing:
            return (f"this model id specifies the {self.family} quota family, "
                    "which has no published limit in this account and region; "
                    "another family's limit is deliberately not substituted")
        if self.ambiguous:
            return ("different accounts publish different limits for this model "
                    "and the telemetry does not say which account was billed")
        return ("no Service Quotas entry matches this model in this account and "
                "region; another model's limit is deliberately not substituted")


async def load_tpm_quota_rows() -> list[dict]:
    return db.rows_to_dicts(await db.fetch(
        "SELECT accountId, region, quota_code, quota_name, model_name, "
        "traffic_type, metric, applied_value, default_value "
        "FROM f_quotas WHERE metric = 'TPM'"))


def resolve(rows: list[dict], model_id: str, region: str,
            account: str | None) -> QuotaVerdict:
    """Resolve the TPM ceiling for one (model, region, account)."""
    hint = family_hint_from_model_id(model_id)
    acct = (account or "").strip()
    if acct and acct not in ("__none__", "__unknown__"):
        res = resolve_quota(rows, acct, region, model_id, "TPM", hint)
        if res.value is not None:
            return QuotaVerdict(limit=float(res.value), family=res.family,
                                account_known=True, model_matched=True)
        # The account is named but has no usable quota row. Distinguish "the
        # family this model declares has no limit here" from "nothing matches
        # this model at all" - they call for different operator action.
        return QuotaVerdict(account_known=True,
                            model_matched=bool(res.candidates),
                            family=res.family if res.family_missing else None,
                            family_missing=res.family_missing)

    # No account on the event. Resolve per account and accept only unanimity -
    # which is the common case, since most accounts sit on the published default.
    accounts = {str(q.get("accountid") or q.get("accountId") or "")
                for q in rows if q.get("region") == region}
    values: set[float] = set()
    family = None
    any_family_missing = False
    any_candidates = False
    for a in accounts:
        if not a:
            continue
        res = resolve_quota(rows, a, region, model_id, "TPM", hint)
        any_candidates = any_candidates or bool(res.candidates)
        any_family_missing = any_family_missing or res.family_missing
        if res.value is not None:
            values.add(float(res.value))
            family = family or res.family
    if not values:
        return QuotaVerdict(model_matched=any_candidates,
                            family=hint if any_family_missing else None,
                            family_missing=any_family_missing)
    if len(values) > 1:
        # Different accounts have different ceilings for this model and the event
        # does not say which account was billed. Any single number would be a
        # guess, so report it as unknown and let the UI say why.
        return QuotaVerdict(family=family, model_matched=True, ambiguous=True)
    return QuotaVerdict(limit=values.pop(), family=family, model_matched=True)


def _rank(d: dict) -> tuple:
    """Ordering for "worst" row: a known utilization always beats an unknown one,
    then the higher utilization wins. Ranking on peak_tpm instead is exactly the
    bug that hid a 160% breach behind a 10% row with more traffic."""
    return (d["utilization_pct"] is not None, d["utilization_pct"] or -1)


async def score(rows: list[dict], group_key: str = "dim_value") -> dict:
    """Turn per-hour proxy rows into per-value quota utilization.

    `rows` must carry: group_key, modelId, endpoint, region, accountId,
    input_tokens, output_tokens (already summed per hour).
    """
    quota_rows = await load_tpm_quota_rows()

    # Peak quota-TPM per (value, model, ACCOUNT, REGION). Burndown is applied per
    # hour BEFORE the peak, since the busiest quota-hour is not always the
    # busiest raw-token one.
    #
    # Audit follow-up #1: this used to key on (value, model) only, so the row with
    # the highest TRAFFIC won and the limit was then resolved for that winner's
    # account. Utilization is traffic ÷ limit, and the limit varies per account
    # and region, so the highest traffic is not the highest utilization:
    #
    #   account A: 100,000 TPM against a 1,000,000 limit ->  10%
    #   account B:  80,000 TPM against a    50,000 limit -> 160%   (a BREACH)
    #
    # A won on traffic, so the output said 10% and B's breach disappeared. The
    # quota key is (account, region, model, family), so the peak has to be taken
    # per quota key and the WORST UTILIZATION selected - never the worst traffic.
    peak: dict[tuple, float] = defaultdict(float)
    meta: dict[tuple, tuple] = {}
    direct_peak: dict[tuple, float] = defaultdict(float)
    direct_meta: dict[tuple, tuple] = {}

    for r in rows:
        val = r.get(group_key) or r.get(group_key.lower())
        mid = r.get("modelid") or r.get("modelId")
        ep = r.get("endpoint")
        region = r.get("region")
        acct = str(r.get("accountid") or r.get("accountId") or "")
        in_tok = int(r.get("input_tokens") or 0)
        out_tok = int(r.get("output_tokens") or 0)
        if ep not in AWS_BILLED_ENDPOINTS:
            # No AWS quota and no burndown concept off Bedrock: raw token rate.
            key = (val, mid, ep)
            tpm = hourly_total_to_per_minute(in_tok + out_tok)
            if tpm > direct_peak[key]:
                direct_peak[key] = tpm
                direct_meta[key] = (ep, region)
            continue
        rate = 1 if ep == "mantle" else output_burndown_rate(mid, is_mantle=False)
        tpm = hourly_total_to_per_minute(in_tok + out_tok * rate)
        key = (val, mid, acct, region, ep)
        if tpm > peak[key]:
            peak[key] = tpm
            meta[key] = (ep, region, acct)

    # One candidate per quota key, each with its OWN limit and utilization.
    candidates: list[dict] = []
    for (val, mid, acct, region, ep), tpm in peak.items():
        v = resolve(quota_rows, mid, region, acct)
        util = (tpm / v.limit * 100.0) if v.known and v.limit else None
        cand = {
            "workload": val,
            "model": mid,
            "region": region,
            "endpoint": ep,
            "accountId": acct or None,
            "peak_tpm": round(tpm, 1),
            "rate_basis": "hourly_average",
            "quota_source": QUOTA_SOURCE_AWS,
            "quota_account_known": v.account_known,
            "quota_model_matched": v.model_matched,
            "quota_ambiguous": v.ambiguous,
            "quota_family": v.family,
            "quota_family_missing": v.family_missing,
            "tpm_limit": round(v.limit, 1) if v.known and v.limit else None,
            "utilization_pct": round(util, 2) if util is not None else None,
        }
        if not v.known:
            cand["limit_unknown_reason"] = v.reason
        candidates.append(cand)

    # Worst UTILIZATION per value, with unknown-limit rows never displacing a row
    # that has a real number.
    best: dict[str, dict] = {}
    for cand in candidates:
        val = cand["workload"]
        cur = best.get(val)
        if cur is None or _rank(cand) > _rank(cur):
            best[val] = cand

    out = sorted(best.values(),
                 key=lambda d: (d["utilization_pct"] is not None,
                                d["utilization_pct"] or 0), reverse=True)
    direct = []
    for key, tpm in sorted(direct_peak.items(), key=lambda kv: kv[1],
                           reverse=True):
        val, mid, _ = key
        ep, region = direct_meta[key]
        direct.append({
            "workload": val, "model": mid, "endpoint": ep, "region": region,
            "peak_tpm": round(tpm, 1), "rate_basis": "hourly_average",
            "tpm_limit": None, "utilization_pct": None,
            "quota_source": QUOTA_SOURCE_PROVIDER,
            "note": ("Called the provider directly, so no AWS quota applies. The "
                     "provider's own rate limits are not visible here."),
        })
    return {
        "is_estimate": True,
        "rows": out,
        # Every (account, region, model) candidate, so a breach in one account is
        # inspectable even when another account's row represents the value.
        "all_candidates": sorted(candidates, key=_rank, reverse=True),
        "breach_count": sum(1 for c in candidates
                            if (c["utilization_pct"] or 0) > 100),
        "direct_provider_rows": direct,
        "quota_scope": "aws_billed_endpoints_only",
        "resolver": "proxy_quota.resolve",
        "any_account_unknown": any(not r["quota_account_known"] for r in out),
        "any_limit_unknown": any(r["tpm_limit"] is None for r in out),
    }
