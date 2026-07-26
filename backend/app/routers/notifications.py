"""Notifications: findings feed + alert configuration (Phase 1+2).

The ingester's evaluator (ingestion/findings.py) maintains f_findings and
publishes state transitions to configured channels. This router is the UI's
surface onto that pipeline:

  GET  /notifications/findings       — findings feed (any signed-in user)
  POST /notifications/findings/ack   — ack/unack one finding (any user)
  GET  /notifications/config         — thresholds + channels (admin view has
                                       full channel config; users get counts)
  PUT  /notifications/config         — admin: thresholds and/or SNS channel
  POST /notifications/test           — admin: send a test notification NOW
  POST /notifications/subscribe      — any user: subscribe own email to the
                                       configured SNS topic (SNS double-opt-in:
                                       AWS emails a confirmation link)

Design rule: the ONLY AWS write anywhere in this feature is sns:Publish /
sns:Subscribe against the admin-configured topic in the CENTRAL account.
Monitored accounts stay read-only. recommended_action fields are "prepared
actions" — ready-to-run CLI / console links the customer executes themselves.
"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, HTTPException, Query, Request

from .. import db
from ..auth import is_admin

router = APIRouter()

_THRESHOLD_KEYS = {
    "notify_quota_warn_pct":    (50.0, 99.0, 80.0),
    "notify_quota_crit_pct":    (60.0, 100.0, 95.0),
    "notify_throttle_warn_pct": (0.1, 50.0, 2.0),
    "notify_cost_jump_pct":     (10.0, 500.0, 40.0),
    "notify_eol_days":          (7.0, 365.0, 90.0),
}
_ARN_RE = re.compile(r"^arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]+$")


async def _table_ok() -> bool:
    return bool(await db.fetchval("SELECT to_regclass('f_findings') IS NOT NULL"))


# --------------------------------------------------------------------------- #
# Findings feed
# --------------------------------------------------------------------------- #
@router.get("/notifications/findings")
async def findings(state: str = Query("active"), limit: int = Query(200, le=500)):
    if not await _table_ok():
        return {"findings": [], "counts": {"active": 0, "critical": 0}}
    where = "WHERE state = 'active'" if state == "active" else ""
    rows = await db.fetch(
        f"""
        SELECT finding_id, type, severity, accountId, model, region, title,
               detail, metric, recommended_action, state, acked,
               first_seen, last_seen, resolved_at
        FROM f_findings {where}
        ORDER BY (state = 'active') DESC,
                 CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END DESC,
                 last_seen DESC
        LIMIT $1
        """, limit)
    counts = await db.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE state='active')                          AS active,
               COUNT(*) FILTER (WHERE state='active' AND severity='critical')  AS critical,
               COUNT(*) FILTER (WHERE state='active' AND NOT acked)            AS unacked
        FROM f_findings
        """)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("metric", "recommended_action"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    d[k] = {}
        out.append(d)
    return {"findings": out, "counts": dict(counts) if counts else {}}


@router.post("/notifications/findings/ack")
async def ack_finding(request: Request, body: dict):
    fid = (body.get("finding_id") or "").strip()
    acked = bool(body.get("acked", True))
    if not fid:
        raise HTTPException(400, "finding_id required")
    n = await db.fetchval(
        "UPDATE f_findings SET acked = $2 WHERE finding_id = $1 RETURNING 1",
        fid, acked)
    if not n:
        raise HTTPException(404, "finding not found")
    return {"ok": True, "finding_id": fid, "acked": acked}


# --------------------------------------------------------------------------- #
# Config (thresholds + channels)
# --------------------------------------------------------------------------- #
@router.get("/notifications/config")
async def get_config(request: Request):
    admin = is_admin(request)
    thresholds = {}
    rows = await db.fetch(
        "SELECT key, value FROM ingestion_meta WHERE key = ANY($1::text[])",
        list(_THRESHOLD_KEYS.keys()))
    saved = {r["key"]: r["value"] for r in rows}
    for key, (_lo, _hi, default) in _THRESHOLD_KEYS.items():
        try:
            thresholds[key] = float(saved.get(key, default))
        except (TypeError, ValueError):
            thresholds[key] = default

    channels = []
    has_channels = bool(await db.fetchval(
        "SELECT to_regclass('notification_channels') IS NOT NULL"))
    if has_channels:
        for r in await db.fetch(
                "SELECT id, type, config, min_severity, enabled, "
                "last_delivery_at, last_delivery_status "
                "FROM notification_channels ORDER BY id"):
            c = dict(r)
            cfg = c["config"]
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except (TypeError, ValueError):
                    cfg = {}
            if not admin:
                # non-admins see that a channel exists, not its ARN
                cfg = {"configured": bool(cfg.get("topic_arn"))}
            c["config"] = cfg
            channels.append(c)
    return {"thresholds": thresholds, "channels": channels, "is_admin": admin}


@router.put("/notifications/config")
async def put_config(request: Request, body: dict):
    if not is_admin(request):
        raise HTTPException(403, "admin access required")

    # 1. thresholds (validated + clamped)
    for key, raw in (body.get("thresholds") or {}).items():
        if key not in _THRESHOLD_KEYS:
            raise HTTPException(400, f"unknown threshold {key}")
        lo, hi, _default = _THRESHOLD_KEYS[key]
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number")
        if not (lo <= val <= hi):
            raise HTTPException(400, f"{key} must be between {lo} and {hi}")
        await db.fetchval(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            RETURNING key
            """, key, str(val))

    # 2. SNS channel: {topic_arn, min_severity, enabled}. Empty ARN = remove.
    if "sns" in body:
        sns = body["sns"] or {}
        arn = (sns.get("topic_arn") or "").strip()
        min_sev = sns.get("min_severity", "warning")
        enabled = bool(sns.get("enabled", True))
        if min_sev not in ("info", "warning", "critical"):
            raise HTTPException(400, "min_severity must be info|warning|critical")
        if arn and not _ARN_RE.match(arn):
            raise HTTPException(400, "topic_arn is not a valid SNS topic ARN")
        existing = await db.fetchrow(
            "SELECT id FROM notification_channels WHERE type = 'sns' ORDER BY id LIMIT 1")
        if not arn:
            if existing:
                await db.fetchval(
                    "DELETE FROM notification_channels WHERE id = $1 RETURNING 1",
                    existing["id"])
        elif existing:
            await db.fetchval(
                """
                UPDATE notification_channels
                SET config = $2, min_severity = $3, enabled = $4
                WHERE id = $1 RETURNING 1
                """, existing["id"], json.dumps({"topic_arn": arn}), min_sev, enabled)
        else:
            await db.fetchval(
                """
                INSERT INTO notification_channels (type, config, min_severity, enabled)
                VALUES ('sns', $1, $2, $3) RETURNING id
                """, json.dumps({"topic_arn": arn}), min_sev, enabled)

    return await get_config(request)


# --------------------------------------------------------------------------- #
# Test + subscribe (the only AWS calls in this router)
# --------------------------------------------------------------------------- #
def _sns_client(topic_arn: str):
    import boto3
    return boto3.client("sns", region_name=topic_arn.split(":")[3])


async def _sns_channel_arn() -> str:
    row = await db.fetchrow(
        "SELECT config FROM notification_channels "
        "WHERE type = 'sns' AND enabled ORDER BY id LIMIT 1")
    if not row:
        return ""
    cfg = row["config"]
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (TypeError, ValueError):
            cfg = {}
    return (cfg or {}).get("topic_arn", "")


@router.post("/notifications/test")
async def send_test(request: Request):
    if not is_admin(request):
        raise HTTPException(403, "admin access required")
    arn = await _sns_channel_arn()
    if not arn:
        raise HTTPException(400, "no enabled SNS channel configured")
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    body = ("Bedrock Ops Lens — test notification\n\n"
            "This is a test from the Notifications settings page. Real alerts "
            "arrive after each ingest run when a finding is created or "
            "resolved (quota utilization, throttle rate, cost jumps, model "
            "EOL).\n" + (f"\nDashboard: {dashboard_url}" if dashboard_url else ""))
    try:
        _sns_client(arn).publish(
            TopicArn=arn, Subject="Bedrock Ops Lens: test notification",
            Message=body)
    except Exception as e:  # noqa: BLE001 — surface the SNS error verbatim
        raise HTTPException(502, f"SNS publish failed: {e}")
    await db.fetchval(
        "UPDATE notification_channels SET last_delivery_at = now(), "
        "last_delivery_status = 'ok (test)' WHERE type = 'sns' RETURNING 1")
    return {"ok": True}


@router.post("/notifications/subscribe")
async def subscribe(request: Request, body: dict):
    """Subscribe the CALLER's email to the configured topic. SNS sends the
    confirmation email (double opt-in) — we never confirm on their behalf."""
    email = (body.get("email") or "").strip().lower()
    user_email = (getattr(request.state, "user", {}) or {}).get("email", "")
    # users may only subscribe their own signed-in address (admins any address)
    if not email:
        email = user_email
    if not email or "@" not in email:
        raise HTTPException(400, "no email address available")
    if email != user_email.lower() and not is_admin(request):
        raise HTTPException(403, "you can only subscribe your own email")
    arn = await _sns_channel_arn()
    if not arn:
        raise HTTPException(400, "no enabled SNS channel configured — ask an admin")
    try:
        _sns_client(arn).subscribe(
            TopicArn=arn, Protocol="email", Endpoint=email)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SNS subscribe failed: {e}")
    return {"ok": True, "email": email,
            "note": "Check your inbox — AWS sent a confirmation link."}
