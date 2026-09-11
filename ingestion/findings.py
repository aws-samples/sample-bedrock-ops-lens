"""Threshold evaluator + notification dispatch (Notifications Phase 1+2).

Runs at the end of every ingest (lambda_handler calls evaluate_and_notify).
Reads the SAME tables the dashboard reads, applies admin-configured
thresholds, and maintains f_findings with a dedup/lifecycle model:

  - detected & new        -> INSERT state='active'   -> notify "new"
  - detected & known      -> refresh last_seen        (no notification)
  - no longer detected    -> state='resolved'         -> notify "resolved"

Delivery is pluggable: notification_channels rows each name a channel type;
_deliver_sns is the only Phase-1 adapter. The SNS message carries BOTH a
human-readable digest (email subscribers) and the structured findings JSON
(Lambda/webhook/EventBridge subscribers), so customers can wire Slack or
ticketing themselves via SNS subscriptions before first-class connectors
exist.

Everything here is read-only against monitored accounts. The one write —
sns:Publish — targets a topic in the CENTRAL account that the admin
explicitly configured. recommended_action is Phase 2: a ready-to-run CLI
command + console deep link; the customer executes it, we never do.

Thresholds (ingestion_meta keys, admin-set via /api/notifications/config):
  notify_quota_warn_pct    default 80   (peak TPM/RPM as % of applied limit)
  notify_quota_crit_pct    default 95
  notify_throttle_warn_pct default 2    (429s as % of requests, last full day)
  notify_cost_jump_pct     default 40   (day-over-7-day-avg spend jump)
  notify_eol_days          default 90   (model EOL/legacy within N days, with traffic)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# Reuse the backend's burndown table AND its quota resolver, so the notification
# evaluator reports exactly what the Quotas tab shows. This file used to carry
# its own private copy of both: a substring model matcher that, on a tie,
# preferred the LARGER limit. That is the defect the shared resolver exists to
# prevent ("Claude Sonnet 4" matches `claude-sonnet-4-5` because "4" is a
# substring of "4-5"), and here it meant an account at 160% of its real Sonnet
# 4.5 limit was scored at 8% of Sonnet 4's and never alerted at all.
try:
    from app.burndown import output_burndown_rate  # Lambda image layout
    from app.quota_match import resolve_quota
    from app.rate_catalog import snapshot_with_conn
except ImportError:  # pragma: no cover - local dev layout
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
    from app.burndown import output_burndown_rate  # type: ignore
    from app.quota_match import resolve_quota  # type: ignore
    from app.rate_catalog import snapshot_with_conn  # type: ignore


def quota_consumption(native: int | float | None,
                      input_quota_tokens: int | float | None,
                      output_tokens: int | float | None,
                      rate: int | float) -> tuple[float | None, str]:
    """Choose ONE quota-consumption source for a single time bucket.

    Returns (value, source) where source is `aws_estimate`, `reconstructed`, or
    `unavailable`. This is deliberately a choice and not `max(native, formula)`:
    taking the larger lets a stale local multiplier override a live AWS
    observation, which is backwards — the whole point of preferring the native
    metric is that AWS computed it with the real, current policy.

    `native` is AWS's EstimatedTPMQuotaUsage for the bucket. It ALREADY has
    burndown and cache-write treatment baked in, so it is never multiplied again.
    A native 0 is a real observation and is used; only None means "no datapoint".

    The reconstruction is the documented formula:
        uncached input + cache-write + output * burndown_rate
    Cache READS are excluded — they do not consume quota.

    Callers must apply this PER BUCKET before taking any peak. Selecting a max
    input and a max output independently and adding them invents an hour that
    never happened.
    """
    if native is not None:
        return float(native), "aws_estimate"
    if input_quota_tokens is None and output_tokens is None:
        return None, "unavailable"
    return (float(input_quota_tokens or 0)
            + float(output_tokens or 0) * float(rate)), "reconstructed"


DEFAULTS = {
    "notify_quota_warn_pct": 80.0,
    "notify_quota_crit_pct": 95.0,
    "notify_throttle_warn_pct": 2.0,
    "notify_cost_jump_pct": 40.0,
    "notify_eol_days": 90.0,
}

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}


async def _thresholds(conn) -> dict:
    out = dict(DEFAULTS)
    rows = await conn.fetch(
        "SELECT key, value FROM ingestion_meta WHERE key = ANY($1::text[])",
        list(DEFAULTS.keys()))
    for r in rows:
        try:
            out[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            pass
    return out


def _quota_console_url(region: str, quota_code: str) -> str:
    return (f"https://{region}.console.aws.amazon.com/servicequotas/home/"
            f"services/bedrock/quotas/{quota_code}")


def _quota_cli(region: str, quota_code: str, desired: float) -> str:
    return (f"aws service-quotas request-service-quota-increase "
            f"--service-code bedrock --quota-code {quota_code} "
            f"--desired-value {int(desired)} --region {region}")


# --------------------------------------------------------------------------- #
# Detectors — each returns a list of finding dicts (no DB writes here).
# --------------------------------------------------------------------------- #
async def _detect_quota_utilization(conn, th: dict) -> list[dict]:
    """Peak TPM (burndown-weighted) and RPM per (account, model, region) over
    the last 7 days vs the applied Service Quotas limit. Same math as the
    Quotas tab: input + cache-write + output*burndown; cache-read excluded."""
    # Per-HOUR rows, not pre-aggregated maxima. The previous query selected
    # MAX(input) and MAX(output) independently and the caller added them, which
    # fabricated an hour that never happened: an account whose input peaked at
    # 09:00 and whose output peaked at 17:00 got input_peak + output_peak*rate as
    # its "peak", always >= the true peak, firing critical alerts for load that
    # never existed. Consumption is now chosen and computed per bucket, and only
    # then reduced to a peak.
    rows = await conn.fetch(
        """
        SELECT accountId, modelId, region, event_date, hour,
               total_requests::float / 60.0                      AS rpm,
               estimated_tpm_quota_usage                         AS native_hour,
               (COALESCE(total_input_tokens,0)
                + COALESCE(total_cache_write_input_tokens,0))    AS input_quota_tokens,
               COALESCE(total_output_tokens,0)                   AS output_tokens
        FROM f_hourly_peak
        WHERE event_date >= current_date - 7 AND endpoint = 'runtime'
          AND total_requests > 0
        """)
    quotas = await conn.fetch(
        """
        SELECT accountId, region, quota_code, model_name, metric, traffic_type,
               applied_value, default_value,
               COALESCE(applied_value, default_value) AS limit_value
        FROM f_quotas
        WHERE COALESCE(applied_value, default_value) > 0
        """)
    quota_rows = [dict(q) for q in quotas]
    # One catalog read for the whole job, over this job's own connection (the
    # ingester Lambda never initialises the API's pool). An admin's rate edit is
    # therefore picked up by the next scheduled run with no redeploy.
    cat = await snapshot_with_conn(conn)

    # Reduce per-hour consumption to a peak per (account, model, region), keeping
    # track of which source(s) produced it so the alert can disclose them.
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r["accountid"], r["modelid"], r["region"])
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"peak_tpm": None, "peak_rpm": 0.0,
                            "sources": set(), "unknown_hours": 0}
        rate = cat.rate_for(r["modelid"], on_date=r.get("event_date")).rate
        value, source = quota_consumption(
            r["native_hour"], r["input_quota_tokens"], r["output_tokens"], rate)
        a["peak_rpm"] = max(a["peak_rpm"], r["rpm"] or 0.0)
        if value is None:
            a["unknown_hours"] += 1
            continue
        a["sources"].add(source)
        tpm = value / 60.0        # hourly Sum -> hourly-average per-minute rate
        if a["peak_tpm"] is None or tpm > a["peak_tpm"]:
            a["peak_tpm"] = tpm

    findings = []
    for (acct, model, region), a in agg.items():
        # Mixed-source disclosure: one native hour must not relabel a series that
        # was mostly reconstructed, and vice versa.
        srcs = a["sources"]
        source_label = ("mixed" if len(srcs) > 1
                        else (next(iter(srcs)) if srcs else "unavailable"))
        for metric_name, peak in (("TPM", a["peak_tpm"]), ("RPM", a["peak_rpm"])):
            if peak is None:
                continue          # no usable consumption source for any hour
            # The SHARED resolver — ranked canonical identity, account- and
            # region-scoped, ambiguity reported rather than resolved by taking
            # the larger limit.
            res = resolve_quota(quota_rows, acct, region, model,
                                metric=metric_name)
            if not res.known or res.ambiguous:
                continue          # unknown limit is not a finding, it is a gap
            limit_value = float(res.value)
            util = 100.0 * peak / limit_value
            if util < th["notify_quota_warn_pct"]:
                continue
            sev = ("critical" if util >= th["notify_quota_crit_pct"] else "warning")
            desired = limit_value * 2
            basis = (f"{metric_name} source: {source_label}"
                     if metric_name == "TPM" else "request count")
            coverage = (f" {a['unknown_hours']} hour(s) had no usable "
                        f"consumption source and were excluded."
                        if a["unknown_hours"] else "")
            findings.append({
                "finding_id": f"quota-{metric_name.lower()}-{acct}-{model}-{region}",
                "type": "quota_utilization",
                "severity": sev,
                "accountId": acct, "model": model, "region": region,
                # One decimal below 10%: "{:.0f}" rendered 0.2% as "at 0% of
                # quota", which reads as a bug rather than a finding. Only
                # reachable when an operator lowers the threshold, but a title
                # that contradicts its own metric is not worth shipping.
                "title": (f"{metric_name} at "
                          f"{util:.1f}% of quota — {model} in {region}"
                          if util < 10 else
                          f"{metric_name} at {util:.0f}% of quota — {model} in {region}"),
                "detail": (f"Peak {metric_name} {peak:,.0f} vs applied limit "
                           f"{limit_value:,.0f} (7-day window, {basis}). "
                           f"Hourly-average per-minute rate, not a measured "
                           f"minute peak.{coverage} Account {acct}."),
                "metric": {"value": round(util, 1),
                           "threshold": th["notify_quota_warn_pct"],
                           "unit": f"{metric_name.lower()}_utilization_pct",
                           "window": "7d",
                           "consumption_source": source_label,
                           "quota_family": res.family},
                "recommended_action": {
                    "kind": "quota_increase",
                    "summary": (f"Request a {metric_name} quota increase for "
                                f"{model} in {region} "
                                f"(suggested: {desired:,.0f})"),
                    "cli": _quota_cli(region, res.quota_code, desired),
                    "console_url": _quota_console_url(region, res.quota_code),
                },
            })
    return findings


async def _detect_throttle_rate(conn, th: dict) -> list[dict]:
    """429 rate per (account, model, region), last complete day."""
    rows = await conn.fetch(
        """
        SELECT accountId, modelId, region,
               SUM(total_requests)  AS reqs,
               SUM(COALESCE(status_429_count,0)) AS throttles
        FROM f_hourly_peak
        WHERE event_date = current_date - 1
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 100
        """)
    findings = []
    for r in rows:
        rate = 100.0 * float(r["throttles"] or 0) / max(1.0, float(r["reqs"]))
        if rate < th["notify_throttle_warn_pct"]:
            continue
        acct, model, region = r["accountid"], r["modelid"], r["region"]
        findings.append({
            "finding_id": f"throttle-{acct}-{model}-{region}",
            "type": "throttle_rate",
            "severity": "critical" if rate >= 3 * th["notify_throttle_warn_pct"] else "warning",
            "accountId": acct, "model": model, "region": region,
            "title": f"Throttle rate {rate:.1f}% — {model} in {region}",
            "detail": (f"{r['throttles']:,} of {r['reqs']:,} requests returned "
                       f"429 yesterday. Account {acct}."),
            "metric": {"value": round(rate, 2),
                       "threshold": th["notify_throttle_warn_pct"],
                       "unit": "throttle_rate_pct", "window": "1d"},
            "recommended_action": {
                "kind": "investigate_throttling",
                "summary": ("Check quota headroom for this model/region; verify "
                            "SDK retries are configured and max_tokens is close "
                            "to actual output size."),
                "cli": "", "console_url": "",
            },
        })
    return findings


async def _detect_cost_jump(conn, th: dict) -> list[dict]:
    """Yesterday's spend per account vs its prior-7-day daily average."""
    rows = await conn.fetch(
        """
        WITH daily AS (
          SELECT accountId, event_date, SUM(total_cost) AS cost
          FROM f_daily_cost
          WHERE event_date >= current_date - 9
          GROUP BY accountId, event_date
        )
        SELECT accountId,
               MAX(cost) FILTER (WHERE event_date = current_date - 1)  AS yesterday,
               AVG(cost) FILTER (WHERE event_date <  current_date - 1) AS base_avg
        FROM daily
        GROUP BY accountId
        """)
    findings = []
    for r in rows:
        y, base = float(r["yesterday"] or 0), float(r["base_avg"] or 0)
        if base < 1.0 or y < 10.0:      # ignore noise on tiny spend
            continue
        jump = 100.0 * (y - base) / base
        if jump < th["notify_cost_jump_pct"]:
            continue
        acct = r["accountid"]
        findings.append({
            "finding_id": f"costjump-{acct}",
            "type": "cost_jump",
            "severity": "warning",
            "accountId": acct, "model": None, "region": None,
            "title": f"Spend up {jump:.0f}% day-over-average — account {acct}",
            "detail": (f"${y:,.2f} yesterday vs ${base:,.2f} prior-7-day daily "
                       f"average."),
            "metric": {"value": round(jump, 1),
                       "threshold": th["notify_cost_jump_pct"],
                       "unit": "cost_jump_pct", "window": "1d_vs_7d_avg"},
            "recommended_action": {
                "kind": "review_cost",
                "summary": "Review the Cost Insights tab for the driving model/usage type.",
                "cli": "", "console_url": "",
            },
        })
    return findings


async def _detect_model_eol(conn, th: dict) -> list[dict]:
    """Models with live traffic (7d) whose legacy/EOL date is within N days."""
    rows = await conn.fetch(
        """
        SELECT l.modelId, l.region, l.model_name,
               l.legacy_time, l.end_of_life_time,
               SUM(p.total_requests) AS reqs
        FROM dim_model_lifecycle l
        JOIN f_hourly_peak p
             -- CloudWatch records cross-region inference-profile traffic with a
             -- geo prefix (us./eu./apac./us-gov./global.), but dim_model_lifecycle
             -- stores the base modelId from ListFoundationModels. Strip the prefix
             -- so profiled models still match their lifecycle row.
             -- Known limitation: CloudWatch occasionally emits a short-form id
             -- (e.g. anthropic.claude-haiku-4-5) without the version suffix
             -- (-20251001-v1:0); those still won't match and are not handled here.
          ON regexp_replace(p.modelId, '^(us|eu|apac|us-gov|global)\\.', '') = l.modelId
         AND p.region = l.region
         AND p.event_date >= current_date - 7
        WHERE COALESCE(l.end_of_life_time, l.legacy_time) IS NOT NULL
        GROUP BY 1,2,3,4,5
        HAVING SUM(p.total_requests) > 0
        """)
    now = datetime.now(timezone.utc)
    findings = []
    for r in rows:
        milestone = r["end_of_life_time"] or r["legacy_time"]
        kind = "EOL" if r["end_of_life_time"] else "Legacy"
        days_left = (milestone - now).days
        if days_left > th["notify_eol_days"]:
            continue
        model, region = r["modelid"], r["region"]
        findings.append({
            "finding_id": f"eol-{model}-{region}",
            "type": "model_eol",
            "severity": "critical" if days_left <= 30 else "warning",
            "accountId": None, "model": model, "region": region,
            "title": (f"{kind} in {max(days_left,0)} days — {model} still has "
                      f"traffic in {region}"),
            "detail": (f"{r['reqs']:,} requests in the last 7 days. "
                       f"{kind} date: {milestone.date().isoformat()}."),
            "metric": {"value": days_left, "threshold": th["notify_eol_days"],
                       "unit": "days_to_milestone", "window": "7d_traffic"},
            "recommended_action": {
                "kind": "migrate_model",
                "summary": "Plan migration to a current model before the milestone.",
                "cli": "",
                "console_url": ("https://docs.aws.amazon.com/bedrock/latest/"
                                "userguide/model-lifecycle.html"),
            },
        })
    return findings


# --------------------------------------------------------------------------- #
# Lifecycle reconcile + delivery
# --------------------------------------------------------------------------- #
async def _reconcile(conn, detected: list[dict]) -> tuple[list[dict], list[dict]]:
    """Upsert detections; resolve gone findings. Returns (new, resolved)."""
    detected_ids = {f["finding_id"] for f in detected}
    existing = {r["finding_id"]: r for r in await conn.fetch(
        "SELECT finding_id, state FROM f_findings")}

    new_findings = []
    for f in detected:
        prior = existing.get(f["finding_id"])
        is_new = prior is None or prior["state"] == "resolved"
        await conn.execute(
            """
            INSERT INTO f_findings (finding_id, type, severity, accountId,
                model, region, title, detail, metric, recommended_action,
                state, last_seen, resolved_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active', now(), NULL)
            ON CONFLICT (finding_id) DO UPDATE SET
                severity = EXCLUDED.severity,
                title = EXCLUDED.title, detail = EXCLUDED.detail,
                metric = EXCLUDED.metric,
                recommended_action = EXCLUDED.recommended_action,
                state = 'active', last_seen = now(), resolved_at = NULL,
                acked = CASE WHEN f_findings.state = 'resolved'
                             THEN FALSE ELSE f_findings.acked END
            """,
            f["finding_id"], f["type"], f["severity"], f.get("accountId"),
            f.get("model"), f.get("region"), f["title"], f["detail"],
            json.dumps(f["metric"]), json.dumps(f["recommended_action"]))
        if is_new:
            new_findings.append(f)

    resolved_rows = await conn.fetch(
        """
        UPDATE f_findings
        SET state = 'resolved', resolved_at = now()
        WHERE state = 'active' AND NOT (finding_id = ANY($1::text[]))
        RETURNING finding_id, type, severity, title
        """, list(detected_ids))
    resolved = [dict(r) for r in resolved_rows]
    return new_findings, resolved


def _render_digest(new: list[dict], resolved: list[dict], dashboard_url: str) -> str:
    lines = ["Bedrock Ops Lens — findings update", ""]
    if new:
        lines.append(f"NEW ({len(new)}):")
        for f in sorted(new, key=lambda x: -_SEV_RANK.get(x["severity"], 0)):
            lines.append(f"  [{f['severity'].upper()}] {f['title']}")
            act = f.get("recommended_action") or {}
            if act.get("summary"):
                lines.append(f"      Action: {act['summary']}")
            if act.get("cli"):
                lines.append(f"      CLI:    {act['cli']}")
            if act.get("console_url"):
                lines.append(f"      Console: {act['console_url']}")
        lines.append("")
    if resolved:
        lines.append(f"RESOLVED ({len(resolved)}):")
        for f in resolved:
            lines.append(f"  {f['title']}")
        lines.append("")
    if dashboard_url:
        lines.append(f"Dashboard: {dashboard_url}")
    return "\n".join(lines)


async def _deliver_sns(conn, channel, new: list[dict], resolved: list[dict]) -> None:
    import boto3
    cfg = channel["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg or "{}")
    topic_arn = (cfg or {}).get("topic_arn", "")
    if not topic_arn:
        raise ValueError("channel has no topic_arn")
    min_rank = _SEV_RANK.get(channel["min_severity"], 1)
    new_f = [f for f in new if _SEV_RANK.get(f["severity"], 0) >= min_rank]
    if not new_f and not resolved:
        return
    region = topic_arn.split(":")[3]
    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    subject = f"Bedrock Ops Lens: {len(new_f)} new finding(s)" if new_f \
        else f"Bedrock Ops Lens: {len(resolved)} finding(s) resolved"
    payload = json.dumps({
        "source": "bedrock-ops-lens",
        "new": new_f, "resolved": resolved,
        "dashboard_url": dashboard_url,
    }, default=str)
    boto3.client("sns", region_name=region).publish(
        TopicArn=topic_arn,
        Subject=subject[:99],
        Message=json.dumps({
            "default": _render_digest(new_f, resolved, dashboard_url),
            "email": _render_digest(new_f, resolved, dashboard_url),
            "lambda": payload, "https": payload, "sqs": payload,
        }),
        MessageStructure="json",
    )


async def evaluate_and_notify(conn) -> dict:
    """Entry point — call after every ingest with an open asyncpg connection."""
    # Table may predate 009 on an un-migrated DB: create defensively.
    exists = await conn.fetchval("SELECT to_regclass('f_findings')")
    if exists is None:
        return {"skipped": "f_findings missing (run migrations)"}

    th = await _thresholds(conn)
    detected: list[dict] = []
    for det in (_detect_quota_utilization, _detect_throttle_rate,
                _detect_cost_jump, _detect_model_eol):
        try:
            detected.extend(await det(conn, th))
        except Exception as e:  # noqa: BLE001 — one bad detector never kills the rest
            print(f"[findings] {det.__name__} failed: {type(e).__name__}: {e}")

    new, resolved = await _reconcile(conn, detected)

    delivered, errors = 0, []
    if new or resolved:
        channels = await conn.fetch(
            "SELECT * FROM notification_channels WHERE enabled")
        for ch in channels:
            try:
                if ch["type"] == "sns":
                    await _deliver_sns(conn, ch, new, resolved)
                    delivered += 1
                    await conn.execute(
                        "UPDATE notification_channels SET last_delivery_at = now(), "
                        "last_delivery_status = 'ok' WHERE id = $1", ch["id"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"channel {ch['id']}: {e}")
                await conn.execute(
                    "UPDATE notification_channels SET last_delivery_status = $2 "
                    "WHERE id = $1", ch["id"], str(e)[:500])
        if new:
            await conn.execute(
                "UPDATE f_findings SET notified_at = now() "
                "WHERE finding_id = ANY($1::text[])",
                [f["finding_id"] for f in new])

    return {"detected": len(detected), "new": len(new),
            "resolved": len(resolved), "channels_delivered": delivered,
            "errors": errors}
