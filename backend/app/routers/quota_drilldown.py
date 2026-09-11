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

from .. import db, rate_catalog
from ..quota_match import family_hint_from_model_id, resolve_quota

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
    """True if a Service Quotas model_name denotes the same model as a CW modelId.

    Delegates to `model_identity.matches` so this drill-down and the burndown
    table cannot disagree about what a quota row applies to.

    The previous implementation here reduced both sides to an alnum-only string
    and required every distinctive token of the quota name to appear as a
    SUBSTRING. A version number is a substring of a longer version number, so
    "Claude Sonnet 4" matched `anthropic.claude-sonnet-4-5-...`: 80,000 TPM of
    Sonnet 4.5 was then scored against Sonnet 4's 1,000,000 limit and reported 8%
    when the true figure was 160%. Canonical identity compares the version
    exactly, so (4,) no longer matches (4, 5).
    """
    from ..model_identity import matches as _canonical
    return _canonical(model_name, model_id)


# ---------------------------------------------------------------------------
# /api/quota-drilldown/options
# ---------------------------------------------------------------------------
@router.get("/quota-drilldown/options")
async def drilldown_options(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all", description="runtime / mantle / all"),
):
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
    if endpoint not in ("runtime", "mantle", "all"):
        endpoint = "all"
    ep_clause = "" if endpoint == "all" else " AND endpoint = $2"
    params = [days] if endpoint == "all" else [days, endpoint]
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region, endpoint,
               SUM(total_requests)::BIGINT AS total_requests
        FROM f_hourly_peak
        -- N days INCLUSIVE of today: matches filters.parse_filters
        -- (today - (days-1) .. today). Using `- days` scanned N+1
        -- calendar days, so this widget disagreed with every other
        -- one for the same `days=` value (audit finding 17).
        WHERE event_date >= current_date - ($1::int - 1)
          {ep_clause}
        GROUP BY accountId, modelId, region, endpoint
        HAVING SUM(total_requests) > 0
        ORDER BY total_requests DESC
        LIMIT 500
        """,
        *params,
    )
    out = []
    for r in rows:
        acct = r["accountid"] if "accountid" in r else r["accountId"]
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        ep = r["endpoint"]
        out.append({
            "accountId": acct,
            "modelId": mid,
            "region": r["region"],
            "endpoint": ep,
            "total_requests": int(r["total_requests"] or 0),
            "label": f"{acct} · {mid} · {r['region']}" + (f" ({ep})" if ep != "runtime" else ""),
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
    endpoint: str = Query("runtime", description="runtime | mantle"),
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
    if endpoint not in ("runtime", "mantle"):
        endpoint = "runtime"

    # Per-model output-token burndown multiplier (15x Opus 4.8 / 10x Sonnet 5 /
    # 5x other Claude <=4.7 / 1x else). CloudWatch's EstimatedTPMQuotaUsage bakes
    # this in, so the quota-accurate TPM must weight output tokens by it. Passed
    # into the SQL as $6 so the weighting happens per-hour before the peak.
    # Burndown doesn't apply on bedrock-mantle (separate quotas) -> rate 1. See
    # app/burndown.py.
    # Resolved through the runtime-editable catalog so an AWS rate change can be
    # applied from Settings without a redeploy; falls back to the bundled table
    # (and says so via `burndown_rate_source`) when no entry matches.
    _cat = await rate_catalog.snapshot()
    _rate_res = _cat.rate_for(model_id, is_mantle=(endpoint == "mantle"))
    rate = _rate_res.rate

    # 1. Hourly time series — per-minute rates, scoped to the chosen endpoint.
    # The dropdown's option carries an `endpoint` field so the UI passes
    # through the right slice.
    rows = await db.fetch(
        """
        SELECT
          (event_date::timestamp + (hour || ' hours')::interval) AS ts,
          total_requests::float / 60.0                            AS rpm,
          -- Quota-accurate TPM. AWS's own EstimatedTPMQuotaUsage is preferred
          -- when the hour has one: AWS computes it with the real, current policy
          -- (cache-write treatment and the output burndown multiplier already
          -- applied), so it is never re-multiplied by $6. This endpoint
          -- previously ALWAYS reconstructed, so the Quotas tab and
          -- /ops-peak-rpm reported different TPM for the same hour whenever a
          -- native datapoint existed.
          --
          -- Fallback is the documented formula:
          --   InputTokenCount + CacheWriteInputTokens + OutputTokenCount*rate
          -- CacheReadInputTokens do NOT count. total_input_tokens is
          -- InputTokenCount (cache excluded), so add cache-write (COALESCE NULL
          -- ->0) and weight output by the model's burndown multiplier ($6).
          --
          -- NULL (not 0) is the only "no datapoint" — an observed zero is a real
          -- observation and is used as-is.
          COALESCE(
            estimated_tpm_quota_usage::float,
            (COALESCE(total_input_tokens,0) + COALESCE(total_cache_write_input_tokens,0))
                + COALESCE(total_output_tokens, 0) * $6
          ) / 60.0                                                 AS tpm,
          CASE WHEN estimated_tpm_quota_usage IS NOT NULL
               THEN 'aws_estimate' ELSE 'reconstructed'
          END                                                      AS tpm_source,
          (COALESCE(total_input_tokens,0) + COALESCE(total_cache_write_input_tokens,0))::float / 60.0 AS input_tpm,
          COALESCE(total_output_tokens, 0)::float / 60.0           AS output_tpm,
          COALESCE(status_429_count, 0)::float    / 60.0           AS error_rpm
        FROM f_hourly_peak
        WHERE accountId = $1 AND modelId = $2 AND region = $3
          AND endpoint = $5
          AND event_date >= current_date - ($4::int - 1)
        ORDER BY ts
        """,
        account_id, model_id, region, days, endpoint, rate,
    )

    # 2. Quota lookup — fuzz-match model_id against the human model_name.
    #    Pick the FIRST matching row's traffic_type and use both its TPM
    #    and RPM values. Same heuristic as the existing Quotas tab.
    quota_rows = db.rows_to_dicts(await db.fetch(
        """
        SELECT accountId, region, model_name, traffic_type, metric,
               quota_code, applied_value, default_value
        FROM f_quotas
        WHERE accountId = $1 AND region = $2
        """,
        account_id, region,
    ))
    # Resolution goes through the ONE shared resolver (app/quota_match.py) so
    # this drill-down and the burndown-risk widget can never again report
    # different limits for the same (account, region, model) — the audit found
    # them disagreeing 6,460,459.81 vs 14,959,243.
    #
    # AWS Service Quotas exposes TPM and RPM as separate quota_code rows, and
    # their model_name strings sometimes normalise differently, so each metric
    # is resolved independently. The model id's inference-profile prefix gives
    # the traffic family exactly ("global." -> Global cross-region, "us."/"eu."
    # -> Cross-region, bare -> On-demand).
    #
    # Do NOT pass `matcher=` here. An explicit matcher is a BOOL predicate, so
    # resolve_quota cannot rank candidates and scores every match as exact - which
    # makes a generic quota name ("Mistral AI Mistral Large") indistinguishable
    # from a version-specific one ("Mistral Large 2407"). Two same-rank candidates
    # with different values are then a collision, and the resolver correctly
    # refuses to guess: this endpoint returned an UNKNOWN limit where the shared
    # resolver returned 50K and 160% for the same Mistral 24.07 input. Letting it
    # use its ranked default keeps the two paths in agreement.
    family_hint = family_hint_from_model_id(model_id)
    tpm_res = resolve_quota(quota_rows, account_id, region, model_id,
                            metric="TPM", family_hint=family_hint)
    rpm_res = resolve_quota(quota_rows, account_id, region, model_id,
                            metric="RPM", family_hint=family_hint)
    tpm_limit, rpm_limit = tpm_res.value, rpm_res.value
    # Surface the traffic family that actually matched. If TPM and RPM disagree,
    # prefer TPM's — that's what oncalls look at first.
    matched_traffic = tpm_res.family or rpm_res.family

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
    tpm_sources: set[str] = set()
    peak_tpm_source: str | None = None
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
        row_source = r["tpm_source"] if tpm is not None else None
        series.append({
            "ts": ts.isoformat() if ts else None,
            "tpm": tpm,
            "rpm": rpm,
            "input_tpm": input_tpm,
            "output_tpm": float(r["output_tpm"] or 0),
            "error_rpm": float(r["error_rpm"] or 0),
            # Per-bucket provenance: AWS's own estimate, or our reconstruction.
            "tpm_source": row_source,
        })
        sum_rpm += rpm
        n += 1
        if tpm is not None:
            sum_tpm += tpm
            n_tpm += 1
            tpm_sources.add(row_source)
            if tpm > peak_tpm:
                peak_tpm = tpm
                peak_tpm_at = ts
                peak_tpm_source = row_source
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
        "burndown_rate_source": _rate_res.source,
        "burndown_rate_verified": _rate_res.verified,
        "burndown_rate_label": _rate_res.label,
        "matched_quota_traffic_type": matched_traffic,
        # Uncertainty travels with the number: which families could apply, and
        # whether the one we used was inferred rather than known.
        "quota_ambiguous": tpm_res.ambiguous or rpm_res.ambiguous,
        "quota_tpm": tpm_res.as_dict(),
        "quota_rpm": rpm_res.as_dict(),
        # Which source produced the TPM series: AWS's EstimatedTPMQuotaUsage, our
        # reconstruction from the burndown table, or a mix of both across hours.
        # A single native hour must not let the whole series claim to be measured
        # by AWS, so this is derived from every bucket that contributed.
        "quota_tpm_source": ("mixed" if len(tpm_sources) > 1
                             else (next(iter(tpm_sources)) if tpm_sources
                                   else "unavailable")),
        "kpis": {
            "peak_tpm":     peak_tpm,
            "peak_tpm_at":  peak_tpm_at.isoformat() if peak_tpm_at else None,
            # The peak is one real hour, so its own provenance is unambiguous
            # even when the series as a whole is mixed.
            "peak_tpm_source": peak_tpm_source,
            "avg_tpm":      avg_tpm,
            "util_pct_tpm": util_tpm,
            "peak_rpm":     peak_rpm,
            "peak_rpm_at":  peak_rpm_at.isoformat() if peak_rpm_at else None,
            "avg_rpm":      avg_rpm,
            "util_pct_rpm": util_rpm,
        },
    }
