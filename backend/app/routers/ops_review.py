"""Ops Review tab.

Two endpoints:
  GET  /api/ops-review              — full structured findings JSON
  POST /api/ops-review/synthesize   — Bedrock LLM synthesis (Claude Opus)

Findings shape:
  capacity_health, growth_signal, burndown_risk, request_shape,
  engagement_opportunities, lifecycle_alerts, lifecycle_meta,
  prompt_caching, recommended_actions.

Prompt caching is evaluated deterministically (../ops_review/caching.py) and
inserted into the report; the report-writing model never sees or writes it.

The customer-facing prompt is a from-scratch rewrite with public AWS
references only (no internal tools, no codenames). See ../ops_review/prompt.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import db, rate_catalog
from ..config import settings
from ..filters import FilterSet, build_where, parse_filters
from ..ops_review import caching
from ..ops_review.prompt import SYSTEM_PROMPT
from ..units import PER_MINUTE_BASIS, hourly_total_to_per_minute
from .extras import _load_lifecycle

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _severity_for_throttle(pct: float) -> str:
    if pct >= 5.0:
        return "critical"
    if pct >= 1.0:
        return "warning"
    if pct > 0.0:
        return "info"
    return "success"


def _is_claude_4_plus(model_id: str) -> bool:
    """Does this model carry an output-token burndown multiplier?

    Gates the Ops Review burndown-risk section. The old implementation was an
    enumerated substring list of Claude 4 SKUs, which meant every Claude 5 model
    answered False — so Opus 5, Sonnet 5 and Fable 5.1 were silently dropped from
    burndown risk despite each burning output tokens at 10x, the highest rates in
    the fleet. An allowlist of known names cannot help but go stale the moment a
    generation ships; that is the bug, not a missing entry.

    Parse the generation instead, and defer the rate itself to burndown.py so
    there is one source of truth. Anything with a multiplier above 1:1 qualifies —
    which today means Claude 4.x and 5.x plus the 10x OpenAI GPT-5.6 SKUs, and
    tomorrow means whatever the rate catalog says without editing this function.
    """
    from ..burndown import output_burndown_rate
    return output_burndown_rate(model_id) > 1


# ---------------------------------------------------------------------------
# /api/ops-review — structured findings
# ---------------------------------------------------------------------------
@router.get("/ops-review")
async def ops_review_findings(f: FilterSet = Depends(parse_filters)):
    """Build the structured findings JSON the synthesis prompt consumes.

    Severity grading mirrors the reference dashboard's thresholds. Filters
    out low-signal rows (< 1000 requests for capacity, < 1M tokens/day for
    growth, etc.) so the report stays actionable."""
    w = build_where(f)
    hourly_where = build_where(f, has_traffic_type=False)
    days = (f.end - f.start).days + 1

    # ---- summary ----
    summary = await db.fetchrow(
        f"""
        SELECT COALESCE(SUM(total_requests), 0)::BIGINT  AS total_requests,
               COALESCE(SUM(failed_requests), 0)::BIGINT AS failed_requests,
               COALESCE(SUM(status_429_count), 0)::BIGINT AS throttled,
               COALESCE(SUM(total_input_tokens), 0)::BIGINT AS total_input_tokens,
               COALESCE(SUM(total_output_tokens), 0)::BIGINT AS total_output_tokens,
               COUNT(DISTINCT accountId)::BIGINT          AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        """,
        *w.params,
    )
    accounts = await db.fetch(
        f"SELECT DISTINCT accountId FROM f_daily WHERE {w.sql} ORDER BY accountId",
        *w.params,
    )
    account_ids = [r["accountid"] if "accountid" in r else r["accountId"] for r in accounts]

    # ---- capacity_health ----
    cap_rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          SUM(total_requests)::BIGINT     AS total_requests,
          SUM(status_429_count)::BIGINT   AS throttled,
          ROUND((100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0))::numeric, 2) AS throttle_pct,
          -- f_hourly_peak stores CloudWatch Period=3600 SUMS. The busiest
          -- hour's hourly-average per-minute rate is total/60. Multiplying by
          -- 60 (the old code) overstated RPM by 3,600x: 73 requests in an hour
          -- became 4,380 "RPM" instead of 1.22. No minute-resolution source
          -- exists here, so this is a lower bound on the true minute peak and
          -- is named accordingly.
          (SELECT MAX(total_requests) / 60.0 FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date
              AND (${len(w.params) + 1}::text = 'all' OR h.endpoint = ${len(w.params) + 1}))
            AS busiest_hour_avg_rpm,
          (SELECT MAX((total_input_tokens + COALESCE(total_cache_write_input_tokens,0)) + total_output_tokens) / 60.0
             FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date
              AND (${len(w.params) + 1}::text = 'all' OR h.endpoint = ${len(w.params) + 1}))
            AS busiest_hour_avg_tpm
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 1000
        ORDER BY throttle_pct DESC NULLS LAST
        LIMIT 25
        """,
        *w.params, f.endpoint,
    )
    capacity_health = []
    for r in cap_rows:
        pct = float(r["throttle_pct"] or 0)
        if pct == 0 and (r["throttled"] or 0) == 0:
            continue
        capacity_health.append({
            "accountId": r["accountid"] if "accountid" in r else r["accountId"],
            "modelId":   r["modelid"] if "modelid" in r else r["modelId"],
            "region":    r["region"],
            "total_requests": int(r["total_requests"]),
            "throttled":      int(r["throttled"] or 0),
            "throttle_pct":   pct,
            # Hourly-average per-minute rates for the busiest hour (see SQL
            # comment). Kept under both the new explicit names and the old keys
            # so existing clients get the corrected values, not stale ones.
            "busiest_hour_avg_rpm": round(float(r["busiest_hour_avg_rpm"] or 0), 2),
            "busiest_hour_avg_tpm": round(float(r["busiest_hour_avg_tpm"] or 0), 2),
            "rate_basis": PER_MINUTE_BASIS,
            "peak_rpm_observed": round(float(r["busiest_hour_avg_rpm"] or 0), 2),
            "peak_tpm_observed": round(float(r["busiest_hour_avg_tpm"] or 0), 2),
            "severity": _severity_for_throttle(pct),
        })

    # ---- growth_signal ----
    growth = []
    if days >= 8:
        split_days = max(1, days // 4)
        recent_start = f.end - timedelta(days=split_days - 1)
        older_end = recent_start - timedelta(days=1)
        older_start = max(f.start, older_end - timedelta(days=split_days - 1))

        async def _avg_tokens(start_d, end_d):
            # Audit finding 10: this used to filter on DATES ONLY, so a review
            # scoped to one account could report a DIFFERENT account's growth.
            # Reuse the request's full filter set for both periods.
            scoped = replace(f, start=start_d, end=end_d)
            ww = build_where(scoped)
            return await db.fetch(
                f"""
                SELECT accountId,
                       (SUM(COALESCE(total_input_tokens, 0) + COALESCE(total_output_tokens, 0))
                        / GREATEST(($2::date - $1::date + 1), 1))::BIGINT AS tokens_per_day
                FROM f_daily
                WHERE {ww.sql}
                GROUP BY accountId
                """,
                *ww.params,
            )

        recent = {(r["accountid"] if "accountid" in r else r["accountId"]): int(r["tokens_per_day"] or 0)
                  for r in await _avg_tokens(recent_start, f.end)}
        older = {(r["accountid"] if "accountid" in r else r["accountId"]): int(r["tokens_per_day"] or 0)
                 for r in await _avg_tokens(older_start, older_end)}
        # Accounts that VANISHED in the recent period are real decline signals;
        # iterating only `recent` hid them entirely.
        for acct in set(recent) | set(older):
            recent_v = recent.get(acct, 0)
            older_v = older.get(acct, 0)
            if recent_v < 1_000_000 and older_v < 1_000_000:
                continue
            if older_v == 0:
                pct = 999.0
            else:
                pct = (recent_v - older_v) / older_v * 100.0
            if pct >= 50:
                trend, sev = "HIGH GROWTH", "warning"
            elif pct >= 20:
                trend, sev = "GROWING", "info"
            elif pct <= -30:
                trend, sev = "DECLINING", "info"
            else:
                continue
            growth.append({
                "accountId": acct,
                "growth_pct": round(pct, 1),
                "recent_avg_tokens_per_day": recent_v,
                "older_avg_tokens_per_day": older_v,
                "trend_label": trend,
                "severity": sev,
            })
        growth.sort(key=lambda r: -abs(r["growth_pct"]))
        growth = growth[:20]

    # ---- burndown_risk ----
    # Rewritten for audit finding 08. The previous version had three defects:
    #   1. hardcoded a 5x multiplier, ignoring the shared endpoint-aware helper
    #      (Opus 4.8 is 15x, Sonnet 5 / Opus 5 are 10x) — up to 66% low;
    #   2. combined MAX(raw_tokens) and MAX(output_tokens) taken from possibly
    #      DIFFERENT hours, inventing an "effective peak" that no single hour
    #      ever had (raw 1,100/out 100 and raw 1,000/out 1,000 -> 5,100, while
    #      the real per-hour effective peaks are 1,500 and 5,000);
    #   3. left the values as hourly SUMS while calling them TPM.
    # Now: pull per-hour rows, weight output by the model's own rate WITHIN each
    # hour, take the max of that, then convert to a per-minute rate once.
    bd_avg_rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          (SUM(total_input_tokens) / GREATEST(SUM(total_requests), 1))::BIGINT AS avg_input,
          (SUM(total_output_tokens) / GREATEST(SUM(total_requests), 1))::BIGINT AS avg_output
        FROM f_daily
        WHERE {w.sql} AND endpoint = 'runtime'
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 100
        """,
        *w.params,
    )
    avg_by_key = {
        ((r["accountid"] if "accountid" in r else r["accountId"]),
         (r["modelid"] if "modelid" in r else r["modelId"]),
         r["region"]): r
        for r in bd_avg_rows
    }

    bd_hourly = await db.fetch(
        f"""
        SELECT accountId, modelId, region, event_date,
               (total_input_tokens + COALESCE(total_cache_write_input_tokens, 0)) AS input_quota_tokens,
               total_output_tokens
        FROM f_hourly_peak
        WHERE {hourly_where.sql} AND endpoint = 'runtime'
        """,
        *hourly_where.params,
    )
    # key -> {raw_hour_max, effective_hour_max}
    bd_peaks: dict[tuple, dict] = {}
    _cat = await rate_catalog.snapshot()
    for r in db.rows_to_dicts(bd_hourly):
        mid = r.get("modelid") or r.get("modelId")
        key = (r.get("accountid") or r.get("accountId"), mid, r["region"])
        if key not in avg_by_key:
            continue
        inp = int(r["input_quota_tokens"] or 0)
        out = int(r["total_output_tokens"] or 0)
        rate = _cat.rate_for(mid, on_date=r.get("event_date")).rate
        raw_hour = inp + out
        eff_hour = inp + out * rate          # weighted INSIDE the hour
        p = bd_peaks.setdefault(key, {"raw": 0, "eff": 0, "rate": rate})
        p["raw"] = max(p["raw"], raw_hour)
        if eff_hour > p["eff"]:
            p["eff"], p["rate"] = eff_hour, rate

    burndown = []
    for key, p in bd_peaks.items():
        acct, mid, region = key
        if not _is_claude_4_plus(mid):
            continue
        if p["raw"] <= 0:
            continue
        raw_tpm = hourly_total_to_per_minute(p["raw"])
        eff_tpm = hourly_total_to_per_minute(p["eff"])
        overhead_pct = 100.0 * (eff_tpm - raw_tpm) / raw_tpm
        if overhead_pct < 30.0:
            continue
        avg_r = avg_by_key.get(key)
        burndown.append({
            "accountId": acct,
            "modelId":   mid,
            "region":    region,
            "burndown_rate":          p["rate"],
            "avg_output_tokens":      int((avg_r or {}).get("avg_output") or 0),
            "busiest_hour_avg_raw_tpm":       round(raw_tpm, 2),
            "busiest_hour_avg_effective_tpm": round(eff_tpm, 2),
            "rate_basis": PER_MINUTE_BASIS,
            # Back-compat keys, now carrying corrected per-minute values.
            "peak_tpm_observed":      round(raw_tpm, 2),
            "effective_peak_tpm_5x":  round(eff_tpm, 2),
            "burndown_overhead_pct":  round(overhead_pct, 1),
            "severity": "critical" if overhead_pct >= 100 else "warning",
        })
    burndown.sort(key=lambda r: -r["burndown_overhead_pct"])
    burndown = burndown[:20]

    # ---- request_shape ----
    shape_rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          ((SUM(total_input_tokens) / GREATEST(SUM(total_requests), 1)))::BIGINT AS avg_input,
          ((SUM(total_output_tokens) / GREATEST(SUM(total_requests), 1)))::BIGINT AS avg_output,
          ROUND((SUM(total_input_tokens)::numeric
                 / NULLIF(SUM(total_output_tokens), 0)), 1) AS ratio,
          SUM(total_requests)::BIGINT AS total_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 1000
        """,
        *w.params,
    )
    shape = []
    for r in shape_rows:
        ratio = float(r["ratio"]) if r["ratio"] is not None else None
        if ratio is None:
            continue
        if ratio > 50:
            # Request shape alone says nothing about prompt reuse or whether the
            # model documents prompt caching; see the prompt_caching block.
            sev, note = "info", "Input-heavy - more input than output tokens; repeated prefixes are not established"
        elif ratio < 2:
            sev, note = "warning", "Output-heavy - check the model's output-token quota multiplier"
        else:
            continue
        shape.append({
            "accountId": r["accountid"] if "accountid" in r else r["accountId"],
            "modelId":   r["modelid"] if "modelid" in r else r["modelId"],
            "region":    r["region"],
            "avg_input_tokens":  int(r["avg_input"] or 0),
            "avg_output_tokens": int(r["avg_output"] or 0),
            "ratio":             ratio,
            "severity":          sev,
            "note":              note,
        })
    shape.sort(key=lambda r: ({"critical": 0, "warning": 1, "info": 2}.get(r["severity"], 3),))

    # ---- engagement_opportunities ----
    cris_gap_rows = await db.fetch(
        f"""
        SELECT accountId, modelId,
          SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
                   THEN total_requests ELSE 0 END)::BIGINT AS od_requests,
          SUM(CASE WHEN traffic_type IN
                   ('CROSS_REGION_OD_INFERENCE_REQUEST',
                    'SOURCE_REGION_OD_INFERENCE_REQUEST')
                   THEN total_requests ELSE 0 END)::BIGINT AS cris_requests
        FROM f_daily
        WHERE {w.sql} AND modelId LIKE 'anthropic.claude-%'
        GROUP BY accountId, modelId
        HAVING SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
                       THEN total_requests ELSE 0 END) > 10000
           AND SUM(CASE WHEN traffic_type IN
                  ('CROSS_REGION_OD_INFERENCE_REQUEST',
                   'SOURCE_REGION_OD_INFERENCE_REQUEST')
                  THEN total_requests ELSE 0 END) = 0
        ORDER BY od_requests DESC LIMIT 10
        """,
        *w.params,
    )
    engagement = []
    for r in cris_gap_rows:
        engagement.append({
            "type": "cris_gap",
            "accountId": r["accountid"] if "accountid" in r else r["accountId"],
            "modelId":   r["modelid"] if "modelid" in r else r["modelId"],
            "od_requests": int(r["od_requests"]),
            "severity": "warning",
            "note": "Only on-demand traffic was observed for this model. Check whether "
                    "a system cross-Region inference profile is available in the source "
                    "Region, then compare its applied quota and routing requirements.",
        })

    # ---- prompt_caching ----
    # Cache counters come only from bedrock-runtime rows: bedrock-mantle
    # publishes no cache metrics and is stored as zeros, which must not read as
    # "no cache activity". NULL counters are missing observations.
    cache_rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT AS requests,
          COUNT(DISTINCT accountId)::BIGINT AS accounts,
          COALESCE(SUM(total_requests) FILTER (WHERE endpoint = 'runtime'), 0)::BIGINT
            AS runtime_requests,
          COUNT(*) FILTER (WHERE endpoint = 'runtime')::BIGINT AS runtime_rows,
          COUNT(*) FILTER (WHERE endpoint = 'runtime'
                             AND (total_cache_read_input_tokens IS NULL
                                  OR total_cache_write_input_tokens IS NULL))::BIGINT
            AS rows_missing_cache,
          COUNT(*) FILTER (WHERE endpoint = 'runtime' AND total_input_tokens IS NULL)::BIGINT
            AS rows_missing_input,
          COUNT(*) FILTER (WHERE endpoint = 'runtime' AND
              (total_input_tokens < 0 OR total_cache_read_input_tokens < 0
               OR total_cache_write_input_tokens < 0))::BIGINT AS invalid_rows,
          COALESCE(SUM(total_input_tokens) FILTER (WHERE endpoint = 'runtime'), 0)::BIGINT
            AS input_tokens,
          COALESCE(SUM(total_cache_read_input_tokens) FILTER (WHERE endpoint = 'runtime'), 0)::BIGINT
            AS cache_read_tokens,
          COALESCE(SUM(total_cache_write_input_tokens) FILTER (WHERE endpoint = 'runtime'), 0)::BIGINT
            AS cache_write_tokens,
          COALESCE(SUM(total_requests) FILTER (WHERE endpoint = 'mantle'), 0)::BIGINT
            AS mantle_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_requests) > 0
        """,
        *w.params,
    )
    prompt_caching = caching.evaluate_models([
        caching.ModelCacheMetrics(
            model_id=r.get("modelid") or r.get("modelId"),
            requests=int(r["requests"] or 0), accounts=int(r["accounts"] or 0),
            runtime_requests=int(r["runtime_requests"] or 0),
            runtime_rows=int(r["runtime_rows"] or 0),
            rows_missing_cache=int(r["rows_missing_cache"] or 0),
            rows_missing_input=int(r["rows_missing_input"] or 0),
            input_tokens=int(r["input_tokens"] or 0),
            cache_read_tokens=int(r["cache_read_tokens"] or 0),
            cache_write_tokens=int(r["cache_write_tokens"] or 0),
            mantle_requests=int(r["mantle_requests"] or 0),
            invalid_rows=int(r["invalid_rows"] or 0))
        for r in db.rows_to_dicts(cache_rows)])

    # ---- lifecycle_alerts ----
    lifecycle = await _load_lifecycle()
    models_meta = lifecycle.get("models", {}) or {}
    today = date.today()
    fleet_models = await db.fetch(
        f"""
        SELECT modelId,
               SUM(total_requests)::BIGINT AS total_requests,
               COUNT(DISTINCT accountId)::BIGINT AS account_count,
               array_agg(DISTINCT region) AS regions
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        """,
        *w.params,
    )
    lifecycle_alerts = []
    for r in fleet_models:
        mid_raw = r["modelid"] if "modelid" in r else r["modelId"]
        bare = mid_raw
        for pfx in ("us.", "eu.", "global.", "apac.", "amer.", "jp.", "au.", "ca."):
            if bare.startswith(pfx):
                bare = bare[len(pfx):]
                break
        meta = models_meta.get(bare)
        if not meta:
            continue
        legacy_str = meta.get("legacy_date")
        eol_str = meta.get("eol_date")
        try:
            legacy_d = date.fromisoformat(legacy_str) if legacy_str else None
            eol_d = date.fromisoformat(eol_str) if eol_str else None
        except ValueError:
            continue
        if eol_d and today >= eol_d:
            sev = "critical"
        elif legacy_d and today >= legacy_d:
            sev = "warning"
        elif legacy_d and (legacy_d - today).days <= 90:
            sev = "info"
        else:
            continue
        regions = sorted([rr for rr in (r["regions"] or []) if rr])
        if int(r["total_requests"]) < 1000:
            continue
        lifecycle_alerts.append({
            "modelId":               mid_raw,
            "base_modelId":          bare,
            "severity":              sev,
            "legacy_date":           legacy_str,
            "eol_date":              eol_str,
            "extended_access_date":  meta.get("extended_access_date"),
            "total_requests":        int(r["total_requests"]),
            "account_count":         int(r["account_count"]),
            "regions":               regions,
        })
    lifecycle_alerts.sort(key=lambda x: ({"critical": 0, "warning": 1, "info": 2}.get(x["severity"], 3),
                                          -x["total_requests"]))

    # ---- recommended_actions ----
    actions = []
    if any(c["severity"] == "critical" for c in capacity_health):
        actions.append({
            "priority": "critical",
            "title": "Throttling > 5% on one or more workloads",
            "detail": "File a Service Quotas increase request for affected (account, model, region) tuples; verify CRIS is enabled first.",
        })
    if any(a["severity"] == "critical" for a in lifecycle_alerts):
        actions.append({
            "priority": "critical",
            "title": "Models past End-of-Life",
            "detail": "Migrate fleet usage off EOL models within the week. Bedrock can stop accepting requests at any time after EOL.",
        })
    if any(e["type"] == "cris_gap" for e in engagement):
        actions.append({
            "priority": "warning",
            "title": "Evaluate cross-Region inference",
            "detail": "Check profile availability, destination Regions, applied quotas "
                      "and current pricing before changing on-demand calls. A model ID "
                      "prefix alone does not establish availability or additional capacity.",
        })
    actions.extend(caching.recommended_actions(prompt_caching))
    if any(b["severity"] == "critical" for b in burndown):
        actions.append({
            "priority": "warning",
            "title": "Claude 4+ burndown overhead > 100%",
            # Finding 08: the up-front deduction is (input tokens + max_tokens) per the token-burndown
            # doc - the burndown rate applies to tokens actually generated, not to the reservation.
            "detail": "Set `max_tokens` close to the output you actually expect rather than leaving "
                      "it at the model maximum: Bedrock deducts (input tokens + max_tokens) from the "
                      "TPM quota when the request starts and replenishes the unused remainder only "
                      "after it completes.",
        })
    if any(g["trend_label"] == "HIGH GROWTH" for g in growth):
        actions.append({
            "priority": "info",
            "title": "Account(s) on +50% growth trajectory",
            "detail": "Pre-emptively file Service Quotas requests for the high-growth accounts before they hit a quota wall.",
        })
    if not actions:
        actions.append({
            "priority": "success",
            "title": "No urgent actions",
            "detail": "Throttling, lifecycle, CRIS, prompt caching, burndown and request shape show nothing to act on in this window.",
        })

    # Name the accounts in every per-account row from the ingester-resolved name
    # map, keyed by the account IDs the scoped queries returned.
    named_rows = capacity_health + growth + burndown + shape
    names = await _account_names({r["accountId"] for r in named_rows})
    for r in named_rows:
        r["account_name"] = names.get(r["accountId"])

    return {
        "window": {"start": f.start.isoformat(), "end": f.end.isoformat(), "days": days},
        "summary": dict(summary),
        "account_ids": account_ids,
        "account_count": len(account_ids),
        "capacity_health":         capacity_health,
        "growth_signal":           growth,
        "burndown_risk":           burndown,
        "request_shape":           shape,
        "engagement_opportunities": engagement,
        "lifecycle_alerts":        lifecycle_alerts,
        "lifecycle_meta": {
            "source":  lifecycle.get("_source"),
            "updated": lifecycle.get("_updated"),
            "model_count": len(models_meta),
        },
        "prompt_caching": prompt_caching,
        "recommended_actions": actions,
    }


async def _account_names(account_ids: set[str]) -> dict[str, str]:
    """Account names from dim_account, where the ingester lands its resolved
    names. A failed lookup leaves rows unnamed; it never fails the review, and an
    unknown name stays unknown."""
    ids = sorted(a for a in account_ids if a)
    if not ids:
        return {}
    try:
        rows = await db.fetch(
            "SELECT accountId, account_name FROM dim_account WHERE accountId = ANY($1::text[])",
            ids)
    except Exception:  # noqa: BLE001 - names are labels, not data the review depends on
        return {}
    out: dict[str, str] = {}
    for r in db.rows_to_dicts(rows):
        aid, name = r.get("accountid") or r.get("accountId"), r.get("account_name")
        if aid and name:
            out[str(aid)] = str(name)
    return out


# ---------------------------------------------------------------------------
# /api/ops-review/synthesize — Bedrock LLM call
# ---------------------------------------------------------------------------
_NARRATIVE_CACHE: dict[str, dict] = {}
_NARRATIVE_CACHE_LIMIT = 64


def _findings_cache_key(findings: dict) -> str:
    """Cache only when the model's complete input and prompt are unchanged.

    Counts alone collided for different accounts/models and changing metrics.
    Cache measurements are still rendered fresh without another model call.
    """
    catalog = caching.load_catalog()
    skeleton = {
        "caching_policy": caching.POLICY_VERSION,
        "caching_render": caching.RENDER_VERSION,
        "caching_catalog": f"{catalog.version}:{catalog.digest}",
        "findings": _report_findings(findings),
        "system_prompt": SYSTEM_PROMPT,
        "model": (settings.ops_review_use_mantle, settings.ops_review_model,
                  settings.bedrock_model_id),
    }
    # MD5 here is a non-cryptographic fingerprint of the skeleton dict, used
    # solely as a cache key. usedforsecurity=False tells bandit/scanners this
    # is not a security-sensitive use; the hash never gates auth or integrity.
    return hashlib.md5(
        json.dumps(skeleton, sort_keys=True, default=str).encode(),
        usedforsecurity=False,
    ).hexdigest()


_EM_DASH_TABLE = {
    ord("—"): "-", ord("–"): "-",
    ord("“"): '"', ord("”"): '"',
    ord("‘"): "'", ord("’"): "'",
}


def _scrub_punctuation(s: str) -> str:
    return s.translate(_EM_DASH_TABLE)


def _strip_note_preamble(s: str) -> str:
    """Strip any leading 'Note: ...' line or '> Note: ...' blockquote that
    sits above the first ## heading. The UI already shows a directional-
    findings alert, so this preamble is redundant."""
    return re.sub(
        r"^[ \t]*(?:>\s*)?(?:Note|Important)\s*:[^\n]*\n+",
        "",
        s,
        count=1,
        flags=re.IGNORECASE,
    )


_MERMAID_BLOCK = re.compile(r"(```mermaid\s*\n)([\s\S]*?)(```)", re.IGNORECASE)
# A node definition: <id><open-bracket><label><close-bracket>. Covers the
# common shapes [ ], ( ), { }. The label is anything up to the matching close
# bracket that isn't itself a bracket.
_MERMAID_NODE_LABEL = re.compile(r"(\b[A-Za-z0-9_]+)(\[|\(|\{)([^\[\]\(\)\{\}]+?)(\]|\)|\})")


def _fix_mermaid_labels(s: str) -> str:
    """Auto-quote Mermaid node labels that contain characters Mermaid can't
    parse bare (`:` `%` `,` `<br/>` `/` etc.). Models routinely emit
    `od1[OD: Nova Lite<br/>42-60%]`, which fails to render ("Diagram too large"
    is a red herring — this is a parse error). Wrapping the label in double
    quotes — `od1["OD: Nova Lite<br/>42-60%"]` — is the documented fix and is
    model-independent (same output whichever LLM produced the diagram)."""
    def _quote(m: "re.Match") -> str:
        ident, ob, label, cb = m.groups()
        lab = label.strip()
        if lab.startswith('"') and lab.endswith('"'):
            return m.group(0)                      # already quoted — leave it
        if not re.search(r'[:%,()<>/#;]|<br', lab):
            return m.group(0)                      # plain label — no need to quote
        lab = lab.replace('"', "'")                # inner double-quotes -> single
        return f'{ident}{ob}"{lab}"{cb}'
    def _fix_block(bm: "re.Match") -> str:
        head, body, tail = bm.groups()
        return head + _MERMAID_NODE_LABEL.sub(_quote, body) + tail
    return _MERMAID_BLOCK.sub(_fix_block, s)


def _strip_lifecycle_gantt(s: str) -> str:
    """Even with the prompt rule in place, models occasionally emit a
    `## Lifecycle timeline ... ```mermaid gantt ... ```` block. The UI
    renders its own horizontal lifecycle component from the structured
    data, so strip any model-emitted version."""
    pattern = re.compile(
        r"##+\s*Lifecycle\s+timeline.*?```mermaid\s+gantt[\s\S]*?```",
        re.IGNORECASE,
    )
    return pattern.sub("", s)


def _report_findings(findings: dict) -> dict:
    """What the report-writing model sees: everything except prompt caching,
    which is evaluated and rendered deterministically instead."""
    view = {k: v for k, v in findings.items() if k != "prompt_caching"}
    view["recommended_actions"] = [a for a in findings.get("recommended_actions") or []
                                   if a.get("topic") != "prompt_caching"]
    return view


def _assemble_report(narrative: str, findings: dict) -> str:
    """The model's narrative with the deterministic prompt-caching section."""
    section = caching.render_markdown(findings.get("prompt_caching") or {})
    return caching.insert_section(narrative, section)


@router.post("/ops-review/synthesize")
async def ops_review_synthesize(
    f: FilterSet = Depends(parse_filters),
    force: bool = False,
):
    """Synthesizes the findings via Bedrock InvokeModel (non-streaming), then
    inserts the deterministic prompt-caching section. Chat clients (the MCP
    ops_review tool) and the UI both receive this same report."""
    findings = await ops_review_findings(f)
    cache_key = _findings_cache_key(findings)
    if not force and cache_key in _NARRATIVE_CACHE:
        cached = _NARRATIVE_CACHE[cache_key]
        return {**cached, "narrative": _assemble_report(cached["narrative"], findings),
                "cached": True}

    # Keep the prompt compact: this endpoint is fronted by CloudFront, whose
    # origin response timeout is 120s. Trim the findings blob to ~60KB (plenty
    # for a good narrative) so the model has less to read and responds well
    # inside the window.
    findings_json = json.dumps(_report_findings(findings), default=str, indent=2)
    if len(findings_json) > 60_000:
        findings_json = findings_json[:60_000] + "\n... (truncated)"
    prompt = SYSTEM_PROMPT.replace("{findings_json}", findings_json)

    # PRIMARY: synthesize via the bedrock-mantle endpoint (Anthropic Messages
    # API) — the dashboard dogfoods the OpenAI-compatible endpoint it recommends
    # to customers. Sonnet 5, flagship-but-fast, finishes inside the 120s budget.
    # FALLBACK: if mantle errors OR returns an empty narrative (e.g. reasoning
    # consumed the whole token budget, or a transient mantle issue), fall back to
    # bedrock-runtime invoke_model so Ops Review still produces a report. The
    # response label records which path actually served it.
    def _extract(payload):
        parts = payload.get("content") or []
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    # Primary/fallback order is toggleable. OPS_REVIEW_USE_MANTLE=true tries the
    # bedrock-mantle endpoint first (to dogfood it) with runtime as fallback;
    # false (default) uses bedrock-runtime as primary with mantle as fallback.
    # Default is runtime-first because us-west-2 mantle is currently unhealthy
    # (500s) and runtime Sonnet 5 is fast + reliable (~20s). Flip to mantle-first
    # once the regional mantle endpoint is stable.
    if settings.ops_review_use_mantle:
        primary = ("bedrock-mantle", _synthesize_via_mantle, settings.ops_review_model)
        secondary = ("bedrock-runtime (fallback)", _synthesize_via_runtime, settings.bedrock_model_id)
    else:
        primary = ("bedrock-runtime", _synthesize_via_runtime, settings.bedrock_model_id)
        secondary = ("bedrock-mantle (fallback)", _synthesize_via_mantle, settings.ops_review_model)

    source, model_used = primary[0], primary[2]
    payload, narrative, first_err = None, "", None
    try:
        payload = primary[1](prompt)
        narrative = _extract(payload)
    except Exception as e:  # noqa: BLE001 — remember, then try the other endpoint
        first_err = f"{primary[0]}: {type(e).__name__}: {e}"

    if not narrative.strip():
        try:
            payload = secondary[1](prompt)
            narrative = _extract(payload)
            source, model_used = secondary[0], secondary[2]
        except Exception as e:  # noqa: BLE001 — both failed → clean 502
            detail = f"Secondary ({secondary[0]}) failed: {type(e).__name__}: {e}"
            if first_err:
                detail = f"Primary {first_err}; {detail}"
            raise HTTPException(502, detail=detail)

    if not narrative.strip():
        raise HTTPException(502, detail="Synthesis returned an empty narrative from both endpoints.")

    narrative = _scrub_punctuation(narrative)
    narrative = _strip_note_preamble(narrative)
    narrative = _strip_lifecycle_gantt(narrative)
    # Whatever the model wrote about prompt caching goes; the deterministic
    # section below is the only caching content in the report.
    narrative = caching.strip_caching_content(narrative)
    narrative = _fix_mermaid_labels(narrative)

    out = {
        "narrative": narrative,
        "model_id": f"{model_used} ({source})",
        "input_tokens":  (payload.get("usage") or {}).get("input_tokens"),
        "output_tokens": (payload.get("usage") or {}).get("output_tokens"),
        "prompt_caching_policy": caching.POLICY_VERSION,
        "cached": False,
    }
    # Cache the model's text only; the caching section is rendered from the
    # current findings on every response. Distinct metric updates now get
    # distinct keys, so bound retention in a long-running backend process.
    _NARRATIVE_CACHE[cache_key] = out
    while len(_NARRATIVE_CACHE) > _NARRATIVE_CACHE_LIMIT:
        del _NARRATIVE_CACHE[next(iter(_NARRATIVE_CACHE))]
    return {**out, "narrative": _assemble_report(narrative, findings)}


def _synthesize_via_mantle(prompt: str) -> dict:
    """Call the bedrock-mantle endpoint's Anthropic Messages API with SigV4.

    Host  : bedrock-mantle.<region>.api.aws
    Path  : POST /anthropic/v1/messages
    Auth  : SigV4, service name 'bedrock' (same creds/role as bedrock-runtime)
    Body  : {"model", "max_tokens", "messages", "anthropic_version"}
    Returns the parsed JSON (content[].text + usage), matching the shape the
    caller already expects from invoke_model. read_timeout bounded to 110s so a
    slow model surfaces a clean 502 before CloudFront's 120s origin cap fires."""
    import urllib.request
    import urllib.error
    import botocore.session
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = settings.ops_review_mantle_region
    host = f"bedrock-mantle.{region}.api.aws"
    url = f"https://{host}/anthropic/v1/messages"
    # NB: no `temperature` — Sonnet 5 (and newer models) reject it as deprecated
    # ("temperature is deprecated for this model"). Omit it entirely.
    # thinking disabled + max_tokens 8192: matches the runtime path. Unbounded
    # extended thinking on the complex findings prompt exhausts the token budget
    # (truncated narrative) and the 120s origin cap. The report is ~2000 output
    # tokens; with thinking off it finishes fast with the full text.
    body = json.dumps({
        "model": settings.ops_review_model,
        "max_tokens": 8192,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    creds = botocore.session.get_session().get_credentials().get_frozen_credentials()
    sig_req = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json", "Host": host,
                                  "anthropic-version": "2023-06-01"})
    SigV4Auth(creds, "bedrock", region).add_auth(sig_req)

    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in sig_req.headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=110) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


def _synthesize_via_runtime(prompt: str) -> dict:
    """Fallback: synthesize via bedrock-runtime invoke_model (boto3). Uses the
    runtime model id (BEDROCK_MODEL_ID / bedrock_model_id, CRIS-prefixed) and the
    Anthropic InvokeModel body shape. Same one-user-message prompt; returns the
    parsed JSON (content[].text + usage) so the caller extracts it identically.
    read_timeout bounded under the 120s origin cap."""
    import boto3
    from botocore.config import Config as _BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client(
        "bedrock-runtime", region_name=settings.bedrock_region,
        config=_BotoConfig(retries={"max_attempts": 1, "mode": "standard"},
                           read_timeout=110, connect_timeout=5),
    )
    # Disable extended thinking. On the complex real findings prompt, Sonnet 5's
    # unbounded reasoning would consume the entire output budget (leaving a
    # truncated ~410-char narrative at 8192) AND blow past the 120s Lambda/origin
    # cap (a 120s timeout observed at 16384). The report itself is only ~2000
    # output tokens; with thinking off it completes in ~25-30s with the full text.
    # 8192 is ample headroom for the structured report + diagram.
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8192,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        resp = client.invoke_model(
            modelId=settings.bedrock_model_id,
            contentType="application/json", accept="application/json", body=body,
        )
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")
    return json.loads(resp["body"].read())
