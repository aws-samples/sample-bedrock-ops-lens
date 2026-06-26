"""Quota drill-down tab. Per-(account, model, region) hourly TPM/RPM time
series joined to the applied Service Quotas limits, plus headline KPIs
(Peak / Avg / Util %).

Source data:
  - f_hourly_peak — hourly per (account, model, region) totals. Already
    populated by the CW metrics ingester. Hourly is the finest resolution
    we have today; we normalise to per-minute by dividing by 60 so the
    chart shape matches the reference example a colleague shared.
  - f_quotas — applied + default RPM/TPM limits per (account, region,
    model_name, traffic_type). model_name is human-friendly text
    ("Anthropic Claude Opus 4.7"), so we fuzz-match it against the
    technical modelId — same approach as QuotasTab.jsx.

Endpoints:
  GET /api/quota-drilldown/options  — distinct accounts/models/regions
                                       with at least one row in window
  GET /api/quota-drilldown          — TPM + RPM series + KPIs for a
                                       specific (account, model, region)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..burndown import output_burndown_rate

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_seps(s: str) -> str:
    """Strip every separator that AWS uses in either model names or IDs so
    that tokens like '4.7' match modelId fragments like '4-7'."""
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def _matches(model_name: str, model_id: str) -> bool:
    """True if a Service Quotas model_name matches a CW modelId.

    Both sides are reduced to alnum-only strings before comparison, so
    '4.7' (in name) and '4-7' (in id) collapse to the same '47'.

    Example:
      model_name = 'Anthropic Claude Opus 4.7'
      modelId    = 'us.anthropic.claude-opus-4-7-v1:0'
      → tokens ['claude','opus','47'] all appear in 'usanthropicclaudeopus47v10'
    """
    if not model_name or not model_id:
        return False
    mid_canon = _strip_seps(model_id)
    name_lower = model_name.lower()
    if _strip_seps(name_lower) and _strip_seps(name_lower) in mid_canon:
        return True
    # Drop noise words; keep version tokens.
    noise = {"anthropic", "amazon", "meta", "mistral", "ai21", "cohere",
             "stability", "deepseek", "openai", "ai", "labs", "the", "for",
             "model", "version"}
    tokens = [t for t in name_lower.replace(",", " ").replace("-", " ").split()
              if t and t not in noise]
    if not tokens:
        return False
    return all(_strip_seps(t) in mid_canon for t in tokens)


# ---------------------------------------------------------------------------
# /api/quota-drilldown/options
# ---------------------------------------------------------------------------
@router.get("/quota-drilldown/options")
async def drilldown_options(days: int = Query(14, ge=1, le=90)):
    """Selector data for the drill-down tab. Returns:
      {
        "options": [
          {"accountId":"...", "modelId":"...", "region":"...",
           "label":"...", "total_requests": 12345 },
          ...
        ]
      }

    Pre-joined so the UI can render a single combo selector ordered by
    volume — the most-used (account, model, region) shows first."""
    rows = await db.fetch(
        """
        SELECT accountId, modelId, region,
               SUM(total_requests)::BIGINT AS total_requests
        FROM f_hourly_peak
        WHERE event_date >= current_date - $1::int
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) > 0
        ORDER BY total_requests DESC
        LIMIT 500
        """,
        days,
    )
    out = []
    for r in rows:
        acct = r["accountid"] if "accountid" in r else r["accountId"]
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        out.append({
            "accountId": acct,
            "modelId": mid,
            "region": r["region"],
            "total_requests": int(r["total_requests"] or 0),
            "label": f"{acct} · {mid} · {r['region']}",
        })
    return {"options": out}


# ---------------------------------------------------------------------------
# /api/quota-drilldown
# ---------------------------------------------------------------------------
@router.get("/quota-drilldown")
async def quota_drilldown(
    account_id: str = Query(..., min_length=12, max_length=12),
    model_id: str = Query(..., min_length=1, max_length=200),
    region: str = Query(..., min_length=1, max_length=40),
    days: int = Query(14, ge=1, le=90),
):
    """Hourly TPM/RPM time series for one (account, model, region) over
    the last N days, normalised to per-minute, joined with the applied
    Service Quotas limits.

    Per-minute conversion: each row represents an hour-aligned bucket
    holding the SUM of requests/tokens in that hour. Dividing by 60 gives
    an average-per-minute rate that's directly comparable to the RPM/TPM
    quota values, matching how AWS CloudWatch displays the same data.

    Returns:
      {
        "series": [
          {"ts": "...", "tpm": 1.2e6, "rpm": 12.5,
           "input_tpm": 0.8e6, "output_tpm": 0.4e6, "error_rpm": 0.1},
          ...
        ],
        "tpm_limit": 30_000_000.0 | None,
        "rpm_limit": 10_000.0 | None,
        "kpis": {
          "peak_tpm": ..., "peak_tpm_at": "...", "avg_tpm": ...,
          "util_pct_tpm": ...,
          "peak_rpm": ..., "peak_rpm_at": "...", "avg_rpm": ...,
          "util_pct_rpm": ...,
        },
        "matched_quota_traffic_type": "On-demand" | "Cross-region" | ...
      }
    """
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be 12 digits")

    # Per-model output-token burndown multiplier (15x Opus 4.8 / 5x other
    # Claude 3.7+ / 1x else). CloudWatch's EstimatedTPMQuotaUsage bakes this in,
    # so the quota-accurate TPM must weight output tokens by it. Passed into the
    # SQL as $5 so the weighting happens per-hour before the peak. See
    # app/burndown.py.
    rate = output_burndown_rate(model_id)

    # 1. Hourly time series — per-minute rates.
    rows = await db.fetch(
        """
        SELECT
          (event_date::timestamp + (hour || ' hours')::interval) AS ts,
          total_requests::float / 60.0                            AS rpm,
          -- Quota-accurate TPM: cache-read input tokens don't count toward
          -- the TPM rate-limit quota, so subtract them (clamp at 0); output
          -- tokens are weighted by the model's burndown multiplier ($5). When
          -- cache-read is NULL (pre-migration rows CloudWatch retention has
          -- aged out — un-backfillable), return NULL so the chart shows a gap
          -- instead of a falsely-inflated spike, and Python excludes it from
          -- the peak/avg.
          (CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL
                ELSE GREATEST(COALESCE(total_input_tokens,0) - total_cache_read_input_tokens, 0)
                     + COALESCE(total_output_tokens, 0) * $5 END)::float / 60.0  AS tpm,
          (CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL
                ELSE GREATEST(COALESCE(total_input_tokens,0) - total_cache_read_input_tokens, 0) END)::float / 60.0 AS input_tpm,
          COALESCE(total_output_tokens, 0)::float / 60.0           AS output_tpm,
          COALESCE(status_429_count, 0)::float    / 60.0           AS error_rpm
        FROM f_hourly_peak
        WHERE accountId = $1 AND modelId = $2 AND region = $3
          AND event_date >= current_date - $4::int
        ORDER BY ts
        """,
        account_id, model_id, region, days, rate,
    )

    # 2. Quota lookup — fuzz-match model_id against the human model_name.
    #    Pick the FIRST matching row's traffic_type and use both its TPM
    #    and RPM values. Same heuristic as the existing Quotas tab.
    quota_rows = await db.fetch(
        """
        SELECT model_name, traffic_type, metric, applied_value, default_value
        FROM f_quotas
        WHERE accountId = $1 AND region = $2
        """,
        account_id, region,
    )
    tpm_limit: float | None = None
    rpm_limit: float | None = None
    matched_traffic: str | None = None

    # AWS Service Quotas exposes TPM and RPM as separate quota_code rows
    # — sometimes with subtly different model_name strings (one might say
    # "Anthropic Claude" while the other says just "Claude"). Picking a
    # single (name, traffic_type) family and reading both metrics off of
    # it loses the metric whose name didn't normalise the same way.
    #
    # Match TPM and RPM independently. To keep them consistent, prefer
    # rows whose traffic_type matches the modelId's CRIS prefix
    # convention: "global." → 'Global cross-region', other regional
    # prefixes ("us." / "eu." / etc.) → 'Cross-region', everything else
    # → 'On-demand'.
    if model_id.startswith("global."):
        prefer_tt = "Global cross-region"
    elif any(model_id.startswith(p + ".") for p in ("us", "eu", "apac", "jp", "au", "ca", "amer")):
        prefer_tt = "Cross-region"
    else:
        prefer_tt = "On-demand"

    def _pick(metric: str) -> tuple[float | None, str | None]:
        candidates = []
        for q in quota_rows:
            if q["metric"] != metric:
                continue
            if not _matches(q["model_name"], model_id):
                continue
            val = q["applied_value"] if q["applied_value"] is not None else q["default_value"]
            if val is None:
                continue
            candidates.append((q["traffic_type"], float(val), q["model_name"]))
        if not candidates:
            return None, None
        # Prefer the matching traffic-type, then the highest applied_value
        # (a customer-requested increase trumps the default row).
        candidates.sort(key=lambda c: (c[0] != prefer_tt, -c[1]))
        return candidates[0][1], candidates[0][0]

    tpm_limit, tpm_traffic = _pick("TPM")
    rpm_limit, rpm_traffic = _pick("RPM")
    # Surface the traffic_type that actually matched. If TPM and RPM
    # disagree, prefer TPM's since that's what oncalls usually look at first.
    matched_traffic = tpm_traffic or rpm_traffic

    # 3. KPIs — peak / avg / util % for both metrics.
    series = []
    peak_tpm = 0.0
    peak_tpm_at: datetime | None = None
    sum_tpm = 0.0
    peak_rpm = 0.0
    peak_rpm_at: datetime | None = None
    sum_rpm = 0.0
    n = 0
    n_tpm = 0  # count only rows with a known cache split (non-NULL tpm)
    for r in rows:
        ts = r["ts"]
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # tpm/input_tpm are NULL for pre-migration rows whose cache-read is
        # un-backfillable. Keep them as None in the series so the chart draws
        # a gap (not a false 0 or inflated spike) and exclude them from
        # peak/avg so historical rows can't corrupt the quota-accurate numbers.
        tpm = None if r["tpm"] is None else float(r["tpm"])
        input_tpm = None if r["input_tpm"] is None else float(r["input_tpm"])
        rpm = float(r["rpm"] or 0)
        series.append({
            "ts": ts.isoformat() if ts else None,
            "tpm": tpm,
            "rpm": rpm,
            "input_tpm": input_tpm,
            "output_tpm": float(r["output_tpm"] or 0),
            "error_rpm": float(r["error_rpm"] or 0),
        })
        sum_rpm += rpm
        n += 1
        if tpm is not None:
            sum_tpm += tpm
            n_tpm += 1
            if tpm > peak_tpm:
                peak_tpm = tpm
                peak_tpm_at = ts
        if rpm > peak_rpm:
            peak_rpm = rpm
            peak_rpm_at = ts

    avg_tpm = (sum_tpm / n_tpm) if n_tpm else 0.0
    avg_rpm = (sum_rpm / n) if n else 0.0
    util_tpm = (peak_tpm / tpm_limit * 100.0) if tpm_limit else None
    util_rpm = (peak_rpm / rpm_limit * 100.0) if rpm_limit else None

    # Derived RPM ceiling: if AWS doesn't publish a per-model RPM quota
    # for this model (common for some Claude SKUs), the workload's
    # *effective* request ceiling is still constrained by TPM. Compute
    # `tpm_limit / avg_tokens_per_request` so users see a meaningful
    # "RPM you'd hit before TPM caps you" number instead of an empty chart.
    rpm_limit_derived: float | None = None
    if rpm_limit is None and tpm_limit and peak_rpm > 0:
        # Use overall window averages for tokens-per-request — peak-period
        # average gets stable answers regardless of idle hours.
        avg_tokens_per_req = (sum_tpm / sum_rpm) if sum_rpm > 0 else 0.0
        if avg_tokens_per_req > 0:
            rpm_limit_derived = tpm_limit / avg_tokens_per_req
            util_rpm = (peak_rpm / rpm_limit_derived * 100.0)

    return {
        "series": series,
        "tpm_limit": tpm_limit,
        "rpm_limit": rpm_limit,
        "rpm_limit_derived": rpm_limit_derived,
        "burndown_rate": rate,
        "matched_quota_traffic_type": matched_traffic,
        "kpis": {
            "peak_tpm":     peak_tpm,
            "peak_tpm_at":  peak_tpm_at.isoformat() if peak_tpm_at else None,
            "avg_tpm":      avg_tpm,
            "util_pct_tpm": util_tpm,
            "peak_rpm":     peak_rpm,
            "peak_rpm_at":  peak_rpm_at.isoformat() if peak_rpm_at else None,
            "avg_rpm":      avg_rpm,
            "util_pct_rpm": util_rpm,
        },
    }
