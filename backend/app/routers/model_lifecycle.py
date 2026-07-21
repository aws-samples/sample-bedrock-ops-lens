"""Model lifecycle endpoint — combines live AWS lifecycle metadata with
this dashboard's usage telemetry.

Data sources:
  - dim_model_lifecycle  (refreshed by ingestion.model_lifecycle from
                          bedrock:ListFoundationModels — 100% live AWS API,
                          no scraping, no bundled JSON)
  - f_daily              (this dashboard's volumetric Bedrock metrics)

The only product opinion in this file is RECOMMENDED_UPGRADES — a small
hand-curated mapping from legacy modelId substring → suggested successor.
That's product judgement, not data; AWS doesn't publish a "what should I
migrate to" API. When new models GA, edit the map and submit a PR.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, parse_filters

router = APIRouter()


# Recommended-upgrade map. Substring-matched against the legacy modelId.
# First match wins, so order from most-specific to most-general. This is
# product opinion, not data — keep it short, reviewable, and obvious.
RECOMMENDED_UPGRADES: list[tuple[str, str]] = [
    # Anthropic ----------------------------------------------------------
    ("claude-3-5-haiku",       "us.anthropic.claude-haiku-4-5-20251001-v1:0 (CRIS)"),
    ("claude-3-haiku",         "us.anthropic.claude-haiku-4-5-20251001-v1:0 (CRIS)"),
    ("claude-3-5-sonnet",      "us.anthropic.claude-sonnet-4-5-20250929-v1:0 (CRIS)"),
    ("claude-3-7-sonnet",      "us.anthropic.claude-sonnet-4-5-20250929-v1:0 (CRIS)"),
    ("claude-3-sonnet",        "us.anthropic.claude-sonnet-4-5-20250929-v1:0 (CRIS)"),
    ("claude-sonnet-4",        "us.anthropic.claude-sonnet-4-5-20250929-v1:0 (CRIS)"),
    ("claude-opus-4",          "us.anthropic.claude-opus-4-5-20251001-v1:0 (CRIS)"),
    # Amazon -------------------------------------------------------------
    ("titan-text-express",     "us.amazon.nova-lite-v1:0 (CRIS)"),
    ("titan-text-lite",        "us.amazon.nova-micro-v1:0 (CRIS)"),
    ("titan-image-generator",  "amazon.nova-canvas-v1:0"),
    ("nova-premier",           "us.amazon.nova-pro-v1:0 (CRIS)"),
    # Meta ---------------------------------------------------------------
    ("llama3-2",               "us.meta.llama3-3-70b-instruct-v1:0 (CRIS)"),
    ("llama3-1-405b",          "us.meta.llama3-3-70b-instruct-v1:0 (CRIS)"),
    # Cohere / AI21 — no AWS-recommended successor today.
    # (Intentionally omitted; the UI shows "Consult model provider" when no
    # match exists, which is honest.)
]


def _recommended_upgrade(model_id: str) -> str | None:
    for needle, successor in RECOMMENDED_UPGRADES:
        if needle in model_id:
            return successor
    return None


def _severity(today: date,
              legacy_d: date | None,
              extended_d: date | None,
              eol_d: date | None) -> str:
    """Map lifecycle dates → severity class consumed by the UI:
        critical  past EOL or past extended-access (active customers paying premium)
        warning   already in Legacy
        info      Legacy starts within the next 90 days
        active    none of the above (don't surface to the user)
    """
    if eol_d and eol_d <= today:
        return "critical"
    if extended_d and extended_d <= today:
        return "critical"
    if legacy_d and legacy_d <= today:
        return "warning"
    if legacy_d and legacy_d <= today + timedelta(days=90):
        return "info"
    return "active"


@router.get("/model-lifecycle")
async def model_lifecycle(f: FilterSet = Depends(parse_filters)):
    """Per-model lifecycle status joined with usage in the selected window.

    Returns:
      models: list of {modelId, public_name, provider, severity,
                       legacy_date, extended_access_date, eol_date,
                       recommended_upgrade, total_requests, unique_accounts,
                       last_accessed, regions, accounts_detail[]}

      meta:   {today, refreshed_at, total_legacy, in_use_count}
    """
    today = date.today()

    # --- 1. Lifecycle dates: collapse (modelId, region) → modelId. Use the
    #         earliest legacy/eol date across regions (most conservative —
    #         "this model goes legacy on date X" is the safe message).
    lc_rows = await db.fetch(
        """
        SELECT modelId,
               MIN(model_name)                          AS model_name,
               MIN(provider)                            AS provider,
               BOOL_OR(status = 'LEGACY')               AS any_legacy,
               MIN(legacy_time)                         AS legacy_time,
               MIN(public_extended_access_time)         AS extended_access_time,
               MIN(end_of_life_time)                    AS end_of_life_time,
               array_agg(DISTINCT region ORDER BY region) AS regions,
               MAX(refreshed_at)                        AS refreshed_at
        FROM dim_model_lifecycle
        WHERE status = 'LEGACY'
        GROUP BY modelId
        """,
    )
    if not lc_rows:
        return {
            "models": [],
            "meta": {
                "today": today.isoformat(),
                "refreshed_at": None,
                "total_legacy": 0,
                "in_use_count": 0,
            },
        }

    refreshed_at = max((r["refreshed_at"] for r in lc_rows if r["refreshed_at"]),
                       default=None)

    # --- 2. Usage in the selected window. Build a WHERE that respects
    #         filters.start/end and accounts (provider/region/traffic don't
    #         apply here — the lifecycle data is per-model, not per-region).
    where_parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        where_parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    where_sql = " AND ".join(where_parts)

    # Per-model usage totals.
    usage_rows = await db.fetch(
        f"""
        SELECT modelId,
               SUM(total_requests)::BIGINT     AS total_requests,
               COUNT(DISTINCT accountId)::INT  AS unique_accounts,
               MAX(event_date)                 AS last_accessed,
               -- gap E: LegacyModelInvocations (calls against models AWS marks
               -- legacy). Confirms migration urgency: legacy AND actively used.
               COALESCE(SUM(legacy_invocations),0)::BIGINT AS legacy_invocations
        FROM f_daily
        WHERE {where_sql}
        GROUP BY modelId
        """,
        *params,
    )
    usage_by_model = {
        (r["modelid"] if "modelid" in r else r["modelId"]): r
        for r in usage_rows
    }

    # Per-(model, account) drill-down for the expandable rows.
    detail_rows = await db.fetch(
        f"""
        SELECT modelId, accountId,
               SUM(total_requests)::BIGINT       AS total_requests,
               array_agg(DISTINCT region ORDER BY region) AS regions,
               MAX(event_date)                   AS last_accessed
        FROM f_daily
        WHERE {where_sql}
        GROUP BY modelId, accountId
        ORDER BY modelId, total_requests DESC
        """,
        *params,
    )
    detail_by_model: dict[str, list] = {}
    for r in detail_rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        aid = r["accountid"] if "accountid" in r else r["accountId"]
        detail_by_model.setdefault(mid, []).append({
            "accountId":      aid,
            "total_requests": int(r["total_requests"] or 0),
            "regions":        list(r["regions"] or []),
            "last_accessed":  r["last_accessed"].isoformat() if r["last_accessed"] else None,
        })

    # --- 3. Match a legacy modelId to its CRIS-prefixed usage rows too.
    #         f_daily logs CRIS calls under modelId=us.anthropic.claude-…
    #         while dim_model_lifecycle reports the bare anthropic.claude-…
    #         modelId. Treat any usage row whose modelId ends with the
    #         lifecycle modelId as the same logical model.
    def _matching_usage(legacy_id: str):
        agg = usage_by_model.get(legacy_id)  # exact match
        agg_total = int(agg["total_requests"]) if agg else 0
        agg_accts = int(agg["unique_accounts"]) if agg else 0
        agg_last = agg["last_accessed"] if agg else None
        agg_legacy = int(agg["legacy_invocations"]) if agg else 0
        details: list = list(detail_by_model.get(legacy_id, []))

        for used_id, u in usage_by_model.items():
            if used_id == legacy_id:
                continue
            # us.anthropic.claude-3-haiku-20240307-v1:0 → ends with the
            # legacy id. Same for eu., global., apac. prefixes.
            if used_id.endswith(legacy_id) or used_id.endswith(legacy_id.split(":")[0]):
                agg_total += int(u["total_requests"] or 0)
                agg_legacy += int(u["legacy_invocations"] or 0)
                # accounts unioned isn't precise, but this is presentation
                agg_accts = max(agg_accts, int(u["unique_accounts"] or 0))
                d = u["last_accessed"]
                if d and (not agg_last or d > agg_last):
                    agg_last = d
                details.extend(detail_by_model.get(used_id, []))
        return agg_total, agg_accts, agg_last, details, agg_legacy

    # --- 4. Assemble per-model rows + severity.
    out_models = []
    in_use = 0
    for r in lc_rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        legacy_d   = r["legacy_time"].date()         if r["legacy_time"]         else None
        extended_d = r["extended_access_time"].date() if r["extended_access_time"] else None
        eol_d      = r["end_of_life_time"].date()    if r["end_of_life_time"]    else None
        sev = _severity(today, legacy_d, extended_d, eol_d)
        if sev == "active":
            continue

        total_req, uniq_accts, last_acc, details, legacy_inv = _matching_usage(mid)
        if total_req > 0:
            in_use += 1

        out_models.append({
            "modelId":              mid,
            "public_name":          r["model_name"],
            "provider":             r["provider"],
            "severity":             sev,
            "legacy_date":          legacy_d.isoformat() if legacy_d else None,
            "extended_access_date": extended_d.isoformat() if extended_d else None,
            "eol_date":             eol_d.isoformat() if eol_d else None,
            "regions":              list(r["regions"] or []),
            "recommended_upgrade":  _recommended_upgrade(mid),
            "total_requests":       total_req,
            "legacy_invocations":   legacy_inv,
            "unique_accounts":      uniq_accts,
            "last_accessed":        last_acc.isoformat() if last_acc else None,
            "accounts_detail":      details,
        })

    # Sort: critical first, then warning, then info; within tier, by EOL date asc.
    sev_rank = {"critical": 0, "warning": 1, "info": 2}
    out_models.sort(key=lambda m: (
        sev_rank.get(m["severity"], 9),
        m["eol_date"] or "9999-12-31",
        -m["total_requests"],
    ))

    return {
        "models": out_models,
        "meta": {
            "today":         today.isoformat(),
            "refreshed_at":  refreshed_at.isoformat() if refreshed_at else None,
            "total_legacy":  len(lc_rows),
            "in_use_count":  in_use,
        },
    }
