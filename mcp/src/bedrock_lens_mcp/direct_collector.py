"""Tier-A fallback: collect Bedrock telemetry directly via boto3.

Used when no deployed dashboard is available. Replicates roughly the same
shape of data the deployed REST API returns, but live from AWS APIs:

  * cost summary           → Cost Explorer GetCostAndUsage
  * quotas                 → Service Quotas list_service_quotas
  * model lifecycle        → Bedrock list_foundation_models
  * volumetric overview    → CloudWatch Metrics AWS/Bedrock
  * model insights         → CloudWatch Metrics + bedrock list_foundation_models

Calls live AWS APIs; bounded by what those APIs natively return (no
multi-account aggregation, no per-tag attribution — that's what Tier B+
unlocks).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3


def _session():
    """boto3 Session honouring AWS_PROFILE / AWS_REGION as set by the user."""
    return boto3.Session()


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def cost_summary(days: int = 30) -> dict:
    """Total Bedrock spend over the last `days` days. Cost Explorer is global
    (us-east-1). Returns a single number + per-day breakdown."""
    ce = _session().client("ce", region_name="us-east-1")
    end = date.today()
    start = end - timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
    )
    total = 0.0
    daily = []
    for r in resp["ResultsByTime"]:
        amt = float(r["Total"]["UnblendedCost"]["Amount"])
        total += amt
        daily.append({"date": r["TimePeriod"]["Start"], "cost": round(amt, 2)})
    return {
        "total_cost": round(total, 2),
        "currency": "USD",
        "window_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daily": daily,
    }


def cost_by_account(days: int = 30) -> dict:
    """Bedrock spend grouped by account. Returns top 20 by cost."""
    ce = _session().client("ce", region_name="us-east-1")
    end = date.today()
    start = end - timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
    )
    rows: dict[str, float] = {}
    for r in resp["ResultsByTime"]:
        for g in r.get("Groups", []):
            acct = g["Keys"][0]
            amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
            rows[acct] = rows.get(acct, 0) + amt
    sorted_rows = sorted(rows.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "rows": [{"accountId": a, "total_cost": round(c, 2)} for a, c in sorted_rows],
        "currency": "USD",
        "window_days": days,
    }


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------
def quotas(region: str | None = None) -> dict:
    """Service Quotas snapshot for AWS::Bedrock for the calling account."""
    region = region or os.environ.get("AWS_REGION") or "us-east-1"
    sq = _session().client("service-quotas", region_name=region)
    out = []
    for service_code in ("bedrock", "bedrock-runtime"):
        paginator = sq.get_paginator("list_service_quotas")
        try:
            for page in paginator.paginate(ServiceCode=service_code):
                for q in page.get("Quotas", []):
                    out.append({
                        "service": service_code,
                        "quota_code": q.get("QuotaCode"),
                        "quota_name": q.get("QuotaName"),
                        "value": q.get("Value"),
                        "unit": q.get("Unit"),
                        "adjustable": q.get("Adjustable"),
                        "global_quota": q.get("GlobalQuota"),
                    })
        except Exception as e:
            out.append({"service": service_code, "error": str(e)})
    return {"region": region, "rows": out}


# ---------------------------------------------------------------------------
# Model lifecycle (Bedrock ListFoundationModels)
# ---------------------------------------------------------------------------
def model_lifecycle(region: str | None = None) -> dict:
    """List foundation models the caller has access to, with lifecycle dates."""
    region = region or os.environ.get("AWS_REGION") or "us-east-1"
    bedrock = _session().client("bedrock", region_name=region)
    models = bedrock.list_foundation_models().get("modelSummaries", [])
    out = []
    for m in models:
        # ListFoundationModels doesn't return modelLifecycle; need GetFoundationModel
        try:
            full = bedrock.get_foundation_model(modelIdentifier=m["modelId"])["modelDetails"]
            lifecycle = full.get("modelLifecycle", {})
        except Exception:
            lifecycle = {}
        out.append({
            "modelId": m["modelId"],
            "providerName": m.get("providerName"),
            "modelName": m.get("modelName"),
            "inputModalities": m.get("inputModalities", []),
            "outputModalities": m.get("outputModalities", []),
            "status": lifecycle.get("status"),
            "endOfLifeDate": str(lifecycle.get("endOfLifeDate", "")),
            "extendedAccessDate": str(lifecycle.get("extendedAccessDate", "")),
        })
    return {"region": region, "models": out}


# ---------------------------------------------------------------------------
# Volumetric (CloudWatch Metrics, AWS/Bedrock)
# ---------------------------------------------------------------------------
def overview_summary(days: int = 14, region: str | None = None) -> dict:
    """Total invocations + tokens + errors via AWS/Bedrock CW namespace."""
    region = region or os.environ.get("AWS_REGION") or "us-east-1"
    cw = _session().client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    def _sum(metric_name: str) -> float:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Bedrock",
            MetricName=metric_name,
            StartTime=start, EndTime=end,
            Period=86400, Statistics=["Sum"],
        )
        return sum(d["Sum"] for d in resp.get("Datapoints", []))

    return {
        "window_days": days,
        "region": region,
        "total_invocations":          _sum("Invocations"),
        "total_input_tokens":          _sum("InputTokenCount"),
        "total_output_tokens":         _sum("OutputTokenCount"),
        "total_invocation_errors":     _sum("InvocationClientErrors") + _sum("InvocationServerErrors"),
        "total_throttles":             _sum("InvocationThrottles"),
    }


def model_insights(days: int = 14, region: str | None = None,
                    top_n: int = 12) -> dict:
    """Per-model invocations/tokens/errors via CW Metrics."""
    region = region or os.environ.get("AWS_REGION") or "us-east-1"
    cw = _session().client("cloudwatch", region_name=region)
    bedrock = _session().client("bedrock", region_name=region)
    models = bedrock.list_foundation_models().get("modelSummaries", [])

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    rows = []
    for m in models[:top_n]:
        modelId = m["modelId"]
        try:
            inv = cw.get_metric_statistics(
                Namespace="AWS/Bedrock", MetricName="Invocations",
                Dimensions=[{"Name": "ModelId", "Value": modelId}],
                StartTime=start, EndTime=end,
                Period=86400, Statistics=["Sum"],
            )
            tok = cw.get_metric_statistics(
                Namespace="AWS/Bedrock", MetricName="OutputTokenCount",
                Dimensions=[{"Name": "ModelId", "Value": modelId}],
                StartTime=start, EndTime=end,
                Period=86400, Statistics=["Sum"],
            )
            invocations = sum(d["Sum"] for d in inv.get("Datapoints", []))
            output_tokens = sum(d["Sum"] for d in tok.get("Datapoints", []))
            if invocations == 0 and output_tokens == 0:
                continue
            rows.append({
                "modelId": modelId,
                "providerName": m.get("providerName"),
                "invocations": int(invocations),
                "output_tokens": int(output_tokens),
            })
        except Exception:
            pass

    rows.sort(key=lambda r: r.get("invocations", 0), reverse=True)
    return {"window_days": days, "region": region, "rows": rows}


# ---------------------------------------------------------------------------
# Self-account helper
# ---------------------------------------------------------------------------
def whoami() -> dict:
    sts = _session().client("sts")
    return sts.get_caller_identity()
