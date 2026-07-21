"""Health, freshness, and metadata endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import db
from ..config import settings

router = APIRouter()


@router.get("/me")
async def me(request: Request):
    """Identity + group membership for the calling user. Drives the user
    dropdown and admin-gated Settings link in the frontend."""
    user = getattr(request.state, "user", None) or {}
    return {
        "sub":          user.get("sub", "default"),
        "email":        user.get("email", "local@dev"),
        "groups":       user.get("groups", []),
        "auth_enabled": settings.auth_enabled,
    }


@router.get("/system-config")
async def system_config():
    """Read-only view of the deploy-time config so the Settings page can
    surface what's currently active."""
    try:
        from ingestion.config import load_config
        cfg = load_config()
        return {
            "deploy_region":            cfg.deploy_region,
            "monitored_accounts_mode":  cfg.monitored_accounts.mode,
            "monitored_accounts_ids":   list(cfg.monitored_accounts.ids),
            "monitored_regions_preset": cfg.monitored_regions.preset,
            "resolved_regions":         cfg.resolved_regions(),
            "invocation_logging_enabled": cfg.invocation_logging.enabled,
            "bedrock_region":           cfg.ops_review.bedrock_region,
            "bedrock_model_id":         cfg.ops_review.bedrock_model_id,
            "reader_role_name":         cfg.iam.reader_role_name,
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/accounts")
async def list_accounts():
    """All accounts in scope for the dashboard, with volume and last-seen.

    Returns the UNION of:
      1. Accounts with at least one row in f_daily over the last 30 days
         (= accounts where the ingester found traffic).
      2. Accounts the ingester is configured to monitor (from
         config.yaml — could be the org list, an explicit list, or just
         the central account). These appear with zero counts when they
         have no traffic yet, so the customer can SEE that those accounts
         are in scope even though they're idle.

    This way the dropdown shows the user's complete fleet picture, not
    just the active subset.
    """
    # Active accounts (have data).
    active_rows = await db.fetch(
        """
        SELECT accountId,
               SUM(total_requests)::BIGINT      AS total_requests,
               COUNT(DISTINCT modelId)::BIGINT  AS model_count,
               MAX(event_date)                  AS last_seen
        FROM f_daily
        WHERE event_date >= current_date - INTERVAL '30 days'
        GROUP BY accountId
        """,
    )
    active = {}
    for r in active_rows:
        aid = r["accountid"] if "accountid" in r else r["accountId"]
        active[aid] = {
            "accountId": aid,
            "total_requests": int(r["total_requests"] or 0),
            "model_count": int(r["model_count"] or 0),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "name": "",
            "in_scope": True,
        }

    # Configured accounts (whether active or not).
    try:
        # Lazy-import — config + accounts modules pull boto3, no need if
        # the API is just listing what's already in the DB.
        from ingestion.config import load_config
        from ingestion.accounts import discover_from_org, MonitoredAccount
        cfg = load_config()
        configured: list[MonitoredAccount] = []
        if cfg.monitored_accounts.mode == "discover-org":
            try:
                configured = discover_from_org()
            except Exception:
                configured = []
        elif cfg.monitored_accounts.mode == "explicit":
            configured = [MonitoredAccount(accountId=a) for a in cfg.monitored_accounts.ids]
        # mode=single → caller's account, but we don't know its ID server-side
        # without an extra STS call; the active set covers it.
    except Exception:
        configured = []

    for m in configured:
        if m.accountId in active:
            if m.name:
                active[m.accountId]["name"] = m.name
            continue
        active[m.accountId] = {
            "accountId":      m.accountId,
            "name":           m.name,
            "total_requests": 0,
            "model_count":    0,
            "last_seen":      None,
            "in_scope":       True,
        }

    # Sort: highest-volume first, idle accounts last (alphabetical within tier).
    out = list(active.values())
    out.sort(key=lambda a: (-a["total_requests"], a["accountId"]))
    return out


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/distinct-filters")
async def distinct_filters():
    """Drives the FilterBar's Provider and Region dropdowns from the actual
    data the customer has — not a hardcoded list. Each Bedrock customer's
    fleet lives in a different mix of regions / providers, so a fixed
    enum either misses options or shows ones they never use.

    Returns:
      providers: ['anthropic', 'amazon', ...]   distinct first-segment of modelId
      regions:   ['us-east-1', 'eu-west-1', ...]
    Both ordered by request volume DESC so the most-used options are at top."""
    rows = await db.fetch(
        """
        SELECT
          CASE
            WHEN split_part(modelId, '.', 1) IN
                 ('us','eu','apac','jp','au','ca','amer','global')
              THEN split_part(modelId, '.', 2)
            ELSE split_part(modelId, '.', 1)
          END AS provider,
          region,
          SUM(total_requests)::BIGINT AS reqs
        FROM f_daily
        WHERE event_date >= current_date - INTERVAL '90 days'
        GROUP BY provider, region
        """,
    )
    prov_totals: dict[str, int] = {}
    region_totals: dict[str, int] = {}
    for r in rows:
        p = r["provider"]
        rg = r["region"]
        rq = int(r["reqs"] or 0)
        if p:
            prov_totals[p] = prov_totals.get(p, 0) + rq
        if rg:
            region_totals[rg] = region_totals.get(rg, 0) + rq
    providers = [p for p, _ in sorted(prov_totals.items(),  key=lambda x: -x[1])]
    regions   = [r for r, _ in sorted(region_totals.items(), key=lambda x: -x[1])]

    # Which Bedrock endpoints actually have data, per source table. The UI
    # uses this to decide whether to SHOW the bedrock-mantle sub-tab on each
    # page: show Mantle wherever data is obtainable by any means, hide it
    # only when a table genuinely has zero Mantle rows (never render a blank
    # Mantle view). Latency is the notable case — Mantle publishes no CW
    # latency, so f_latency_daily only has a 'mantle' slice when the customer
    # enabled invocation logging (invocation_logs.py derives it).
    mantle = {}
    for key, table in (("volumetric", "f_daily"),
                       ("peak", "f_hourly_peak"),
                       ("errors", "f_hourly_errors"),
                       ("latency", "f_latency_daily")):
        try:
            row = await db.fetch(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE endpoint = 'mantle') AS has_mantle"
            )
            mantle[key] = bool(row and row[0]["has_mantle"])
        except Exception:
            mantle[key] = False
    return {"providers": providers, "regions": regions,
            "mantle_available": mantle}


@router.get("/ingestion-status")
async def ingestion_status():
    """Replaces the reference's /mirror-status. Tells the UI when data was
    last refreshed and whether tag-attributed data is available."""
    meta_rows = await db.fetch("SELECT key, value, updated_at FROM ingestion_meta")
    meta = {r["key"]: {"value": r["value"], "updated_at": r["updated_at"].isoformat()}
            for r in meta_rows}

    counts = await db.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM f_daily)         AS f_daily_rows,
          (SELECT COUNT(*) FROM f_daily_tagged)  AS f_daily_tagged_rows,
          (SELECT COUNT(*) FROM f_hourly_peak)   AS f_hourly_peak_rows,
          (SELECT COUNT(*) FROM f_hourly_errors) AS f_hourly_errors_rows,
          (SELECT COUNT(*) FROM f_latency_daily) AS f_latency_daily_rows,
          (SELECT COUNT(*) FROM dim_tags)        AS dim_tags_rows
        """
    )

    return {
        "meta": meta,
        "row_counts": dict(counts),
        "tags_available": counts["f_daily_tagged_rows"] > 0,
    }
