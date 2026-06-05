"""Ops Review tab.

Two endpoints:
  GET  /api/ops-review              — full structured findings JSON
  POST /api/ops-review/synthesize   — Bedrock LLM synthesis (Claude Opus)

Findings shape mirrors the internal reference exactly:
  capacity_health, growth_signal, burndown_risk, request_shape,
  engagement_opportunities, lifecycle_alerts, lifecycle_meta,
  recommended_actions.

The customer-facing prompt is a from-scratch rewrite with public AWS
references only (no internal tools, no codenames). See ../ops_review/prompt.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import db
from ..config import settings
from ..filters import FilterSet, build_where, parse_filters
from ..ops_review.prompt import SYSTEM_PROMPT
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
    """Public Claude 4+ family check. We use a simple substring match against
    the public model IDs we know are Claude 4 generation. The reference's
    codename-based check (`anthropic.coffee/fern/...`) is NOT relevant for
    the customer build."""
    needles = (
        "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
        "claude-opus-4-1", "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7",
        "claude-sonnet-4-5", "claude-sonnet-4-6",
        "claude-haiku-4-5",
    )
    m = model_id.lower()
    return any(n in m for n in needles)


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
          (SELECT MAX(total_requests) * 60::BIGINT FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date)
            AS peak_rpm_observed,
          (SELECT MAX(total_input_tokens + total_output_tokens) FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date)
            AS peak_tpm_observed
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 1000
        ORDER BY throttle_pct DESC NULLS LAST
        LIMIT 25
        """,
        *w.params,
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
            "peak_rpm_observed": int(r["peak_rpm_observed"] or 0),
            "peak_tpm_observed": int(r["peak_tpm_observed"] or 0),
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
            return await db.fetch(
                """
                SELECT accountId,
                       (SUM(total_input_tokens + total_output_tokens) / GREATEST(($2::date - $1::date + 1), 1))::BIGINT AS tokens_per_day
                FROM f_daily
                WHERE event_date BETWEEN $1::date AND $2::date
                GROUP BY accountId
                """,
                start_d, end_d,
            )

        recent = {(r["accountid"] if "accountid" in r else r["accountId"]): int(r["tokens_per_day"] or 0)
                  for r in await _avg_tokens(recent_start, f.end)}
        older = {(r["accountid"] if "accountid" in r else r["accountId"]): int(r["tokens_per_day"] or 0)
                 for r in await _avg_tokens(older_start, older_end)}
        for acct, recent_v in recent.items():
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
    bd_rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          (SUM(total_input_tokens) / GREATEST(SUM(total_requests), 1))::BIGINT AS avg_input,
          (SUM(total_output_tokens) / GREATEST(SUM(total_requests), 1))::BIGINT AS avg_output,
          (SELECT MAX(total_input_tokens + total_output_tokens) FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date)
            AS peak_tpm_observed,
          (SELECT MAX(total_output_tokens) FROM f_hourly_peak h
            WHERE h.accountId = f_daily.accountId AND h.modelId = f_daily.modelId
              AND h.region = f_daily.region AND h.event_date BETWEEN $1::date AND $2::date)
            AS peak_output_tpm
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) >= 100
        """,
        *w.params,
    )
    burndown = []
    for r in bd_rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        if not _is_claude_4_plus(mid):
            continue
        peak_tpm = int(r["peak_tpm_observed"] or 0)
        peak_out = int(r["peak_output_tpm"] or 0)
        if peak_tpm == 0:
            continue
        eff = peak_tpm + 4 * peak_out  # the "5x" burndown
        overhead_pct = 100.0 * (eff - peak_tpm) / max(peak_tpm, 1)
        if overhead_pct < 30.0:
            continue
        sev = "critical" if overhead_pct >= 100 else "warning"
        burndown.append({
            "accountId": r["accountid"] if "accountid" in r else r["accountId"],
            "modelId":   mid,
            "region":    r["region"],
            "avg_output_tokens":      int(r["avg_output"] or 0),
            "peak_tpm_observed":      peak_tpm,
            "effective_peak_tpm_5x":  eff,
            "burndown_overhead_pct":  round(overhead_pct, 1),
            "severity": sev,
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
            sev, note = "info", "Input-heavy — high prompt-caching potential"
        elif ratio < 2:
            sev, note = "warning", "Output-heavy — Claude 4+ burndown amplifier"
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
    # Cache hit rate uses the corrected denominator: cached / (cached + fresh).
    # CloudWatch's InputTokenCount excludes cache reads, so summing the two
    # gives the true total prompt tokens billed.
    caching_gap_rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_input_tokens)::BIGINT AS total_input_tokens,
          SUM(total_cache_read_input_tokens)::BIGINT AS cache_read_tokens,
          ROUND((100.0 * COALESCE(SUM(total_cache_read_input_tokens), 0)
                  / NULLIF(COALESCE(SUM(total_cache_read_input_tokens), 0)
                           + COALESCE(SUM(total_input_tokens), 0), 0))::numeric, 2)
            AS cache_hit_pct
        FROM f_daily
        WHERE {w.sql} AND modelId LIKE 'anthropic.claude-%'
        GROUP BY modelId
        HAVING SUM(total_input_tokens) > 100000000
           AND COALESCE(SUM(total_cache_read_input_tokens), 0)
               < (COALESCE(SUM(total_cache_read_input_tokens), 0)
                  + COALESCE(SUM(total_input_tokens), 0)) * 0.05
        ORDER BY total_input_tokens DESC LIMIT 10
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
            "note": f"100% on-demand for a Claude model with a CRIS variant available. Migrate to `us.`/`eu.`/`global.` prefix for ~2x quota at no cost.",
        })
    for r in caching_gap_rows:
        engagement.append({
            "type": "caching_gap",
            "modelId":  r["modelid"] if "modelid" in r else r["modelId"],
            "total_input_tokens": int(r["total_input_tokens"]),
            "cache_hit_pct":      float(r["cache_hit_pct"] or 0),
            "severity": "info",
            "note": f"<5% cache hit rate on >100M daily input tokens. Enable prompt caching on stable system prompts for ~90% cost / ~85% TTFT reduction on cached portions.",
        })

    # ---- lifecycle_alerts ----
    lifecycle = _load_lifecycle()
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
            "title": "CRIS migration available",
            "detail": "Switch on-demand Claude calls to a CRIS profile (`us.` / `eu.` / `global.` prefix) for ~2x quota at no extra cost.",
        })
    if any(e["type"] == "caching_gap" for e in engagement):
        actions.append({
            "priority": "info",
            "title": "Prompt caching opportunity",
            "detail": "Enable caching on stable system prompts for high-volume Claude models — ~90% cost reduction on cached portions.",
        })
    if any(b["severity"] == "critical" for b in burndown):
        actions.append({
            "priority": "warning",
            "title": "Claude 4+ burndown overhead > 100%",
            "detail": "Tune `max_tokens` close to actual expected output (not the model maximum) to prevent phantom quota burndown.",
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
            "detail": "Throttling, lifecycle, CRIS, caching, burndown, and request shape are all within healthy ranges in this window.",
        })

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
        "recommended_actions": actions,
    }


# ---------------------------------------------------------------------------
# /api/ops-review/synthesize — Bedrock LLM call
# ---------------------------------------------------------------------------
_NARRATIVE_CACHE: dict[str, dict] = {}


def _findings_cache_key(findings: dict) -> str:
    """Hash the structural identity of a findings blob (keys + counts +
    severities) so trivial timestamp re-orders don't bust the cache."""
    skeleton = {
        "window": findings.get("window"),
        "account_count": findings.get("account_count"),
        "capacity_n": len(findings.get("capacity_health") or []),
        "growth_n": len(findings.get("growth_signal") or []),
        "burndown_n": len(findings.get("burndown_risk") or []),
        "shape_n": len(findings.get("request_shape") or []),
        "engagement_n": len(findings.get("engagement_opportunities") or []),
        "lifecycle_n": len(findings.get("lifecycle_alerts") or []),
        "summary_total": (findings.get("summary") or {}).get("total_requests"),
    }
    # MD5 here is a non-cryptographic fingerprint of the skeleton dict, used
    # solely as a cache key. usedforsecurity=False tells bandit/scanners this
    # is not a security-sensitive use; the hash never gates auth or integrity.
    return hashlib.md5(
        json.dumps(skeleton, sort_keys=True).encode(),
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


@router.post("/ops-review/synthesize")
async def ops_review_synthesize(
    f: FilterSet = Depends(parse_filters),
    force: bool = False,
):
    """Synthesizes the findings via Bedrock InvokeModel (non-streaming —
    matches the reference)."""
    findings = await ops_review_findings(f)
    cache_key = _findings_cache_key(findings)
    if not force and cache_key in _NARRATIVE_CACHE:
        cached = _NARRATIVE_CACHE[cache_key]
        return {**cached, "cached": True}

    try:
        import boto3  # local import keeps the dep optional for non-Bedrock paths
        from botocore.config import Config as _BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise HTTPException(503, detail="boto3 not installed")

    findings_json = json.dumps(findings, default=str, indent=2)
    if len(findings_json) > 180_000:
        findings_json = findings_json[:180_000] + "\n... (truncated)"
    prompt = SYSTEM_PROMPT.replace("{findings_json}", findings_json)

    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.bedrock_region,
        config=_BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"},
                           read_timeout=900),
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        resp = client.invoke_model(
            modelId=settings.bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
    except (BotoCoreError, ClientError) as e:
        raise HTTPException(502, detail=f"Bedrock call failed: {type(e).__name__}: {e}")

    payload = json.loads(resp["body"].read())
    parts = payload.get("content") or []
    narrative = "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    narrative = _scrub_punctuation(narrative)
    narrative = _strip_note_preamble(narrative)
    narrative = _strip_lifecycle_gantt(narrative)

    out = {
        "narrative": narrative,
        "model_id": settings.bedrock_model_id,
        "input_tokens":  (payload.get("usage") or {}).get("input_tokens"),
        "output_tokens": (payload.get("usage") or {}).get("output_tokens"),
        "cached": False,
    }
    _NARRATIVE_CACHE[cache_key] = out
    return out
