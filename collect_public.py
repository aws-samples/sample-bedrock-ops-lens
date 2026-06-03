#!/usr/bin/env python3
"""Collect Bedrock operational data using public AWS APIs (boto3).
Produces JSON files compatible with analyze.py — drop-in replacement for dante_collect.py.
"""
import boto3, json, os, argparse, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

def _log(msg):
    """Log to stderr so MCP stdio protocol isn't corrupted."""
    print(msg, file=sys.stderr)


def get_session(region, profile=None):
    return boto3.Session(region_name=region, profile_name=profile)


# ── 1. Quotas (service-quotas API) ──────────────────────────────────────────

BEDROCK_SERVICE_CODE = "bedrock"

def collect_quotas(session, account_id, region):
    """Fetch all Bedrock service quotas. Returns (by_model_list, other_list)."""
    sq = session.client("service-quotas", region_name=region)
    by_model, other = [], []

    # Applied (non-default) quotas
    applied = {}
    paginator = sq.get_paginator("list_service_quotas")
    for page in paginator.paginate(ServiceCode=BEDROCK_SERVICE_CODE):
        for q in page["Quotas"]:
            applied[q["QuotaCode"]] = q

    # Default quotas (to fill in anything not explicitly set)
    defaults = {}
    paginator = sq.get_paginator("list_aws_default_service_quotas")
    for page in paginator.paginate(ServiceCode=BEDROCK_SERVICE_CODE):
        for q in page["Quotas"]:
            defaults[q["QuotaCode"]] = q

    # Merge: applied overrides defaults
    all_quotas = {**defaults, **applied}

    for code, q in all_quotas.items():
        name = q.get("QuotaName", "")
        value = q.get("Value", 0)
        is_model_quota = any(k in name.lower() for k in ["tokens per minute", "requests per minute"])

        entry = {
            "Quota Name": name,
            "Quota Code": code,
            "Value": f"{int(value):,}" if value == int(value) else str(value),
            "Region": region,
            "Account ID": account_id,
            "Adjustable": q.get("Adjustable", False),
            "Applied At Level": "ACCOUNT" if code in applied else "DEFAULT",
        }

        if is_model_quota:
            # Extract model name and type from quota name
            # e.g. "Tokens per minute for Claude 3 Haiku on-demand" 
            model_name, inf_type = _parse_quota_name(name)
            entry["Model Name"] = model_name
            entry["Inference Type"] = inf_type
            if "tokens per minute" in name.lower():
                entry["TPM"] = entry["Value"]
                entry["RPM"] = "---"
            else:
                entry["RPM"] = entry["Value"]
                entry["TPM"] = "---"
            by_model.append(entry)
        else:
            other.append(entry)

    # Merge TPM+RPM for same model/region/account/inference_type
    by_model = _merge_tpm_rpm(by_model)
    return by_model, other


def _parse_quota_name(name):
    """Extract model name and inference type from quota name string."""
    name_lower = name.lower()
    inf_type = "On-demand model inference"
    if "cross-region" in name_lower or "cris" in name_lower:
        inf_type = "Cross-region (CRIS) model inference"
    elif "global cross-region" in name_lower:
        inf_type = "Global Cross-region (CRIS) model inference"
    elif "provisioned" in name_lower:
        inf_type = "Provisioned model inference"

    # Strip the metric part to get model name
    for strip in ["tokens per minute for ", "requests per minute for ",
                   " on-demand", " cross-region", " provisioned", " cris",
                   " global cross-region"]:
        name = name.replace(strip, "").replace(strip.title(), "")
    return name.strip(), inf_type


def _merge_tpm_rpm(entries):
    """Merge separate TPM and RPM entries for the same model into one row."""
    key_fn = lambda e: (e.get("Model Name"), e.get("Account ID"), e.get("Region"), e.get("Inference Type"))
    grouped = defaultdict(dict)
    for e in entries:
        k = key_fn(e)
        merged = grouped[k]
        merged.update(e)
        if e.get("TPM") and e["TPM"] != "---":
            merged["TPM"] = e["TPM"]
        if e.get("RPM") and e["RPM"] != "---":
            merged["RPM"] = e["RPM"]
    return list(grouped.values())


# ── 2. Foundation Models (bedrock API) ──────────────────────────────────────

def collect_foundation_models(session, account_id, region):
    """Fetch foundation model inventory."""
    br = session.client("bedrock", region_name=region)
    resp = br.list_foundation_models()
    models = []
    for m in resp.get("modelSummaries", []):
        models.append({
            "Model": m.get("modelName", ""),
            "Provider": m.get("providerName", ""),
            "Model ID": m.get("modelId", ""),
            "Lifecycle": m.get("modelLifecycle", {}).get("status", "ACTIVE"),
            "Input Modalities": ", ".join(m.get("inputModalities", [])),
            "Output Modalities": ", ".join(m.get("outputModalities", [])),
            "Streaming": m.get("responseStreamingSupported", False),
            "Region": region,
            "Account ID": account_id,
        })
    return models


# ── 3. Inference Profiles (bedrock API) ─────────────────────────────────────

def collect_inference_profiles(session, account_id, region):
    """Fetch system-defined inference profiles (CRIS routing)."""
    br = session.client("bedrock", region_name=region)
    profiles = []
    try:
        paginator = br.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
            for p in page.get("inferenceProfileSummaries", []):
                profiles.append({
                    "Profile Name": p.get("inferenceProfileName", ""),
                    "Profile ID": p.get("inferenceProfileId", ""),
                    "Type": p.get("type", ""),
                    "Status": p.get("status", ""),
                    "Models": ", ".join(m.get("modelArn", "").split("/")[-1] for m in p.get("models", [])),
                    "Region": region,
                    "Account ID": account_id,
                })
    except Exception:
        pass  # list_inference_profiles may not be available in all regions
    return profiles


# ── 4. Logging Config (bedrock API) ─────────────────────────────────────────

def collect_logging_config(session, account_id, region):
    """Check model invocation logging configuration."""
    br = session.client("bedrock", region_name=region)
    try:
        resp = br.get_model_invocation_logging_configuration()
        cfg = resp.get("loggingConfig", {})
        return {
            "Region": region,
            "Account Id": account_id,
            "S3 Logging": cfg.get("s3Config") is not None,
            "CloudWatch Logging": cfg.get("cloudWatchConfig") is not None,
            "Text": cfg.get("textDataDeliveryEnabled", False),
            "Image": cfg.get("imageDataDeliveryEnabled", False),
            "Embedding": cfg.get("embeddingDataDeliveryEnabled", False),
        }
    except Exception:
        return {
            "Region": region, "Account Id": account_id,
            "S3 Logging": False, "CloudWatch Logging": False,
            "Text": False, "Image": False, "Embedding": False,
        }


# ── 5. CloudWatch Metrics (cloudwatch API) ──────────────────────────────────

CW_METRICS = [
    ("Invocations", "Sum"),
    ("InvocationThrottles", "Sum"),
    ("InvocationLatency", "Average"),
    ("InvocationLatency", "p95"),  # extended stat
    ("InputTokenCount", "Sum"),
    ("OutputTokenCount", "Sum"),
    ("InputTextTokenCount", "Sum"),
    ("OutputTextTokenCount", "Sum"),
    ("InputSpeechTokenCount", "Sum"),
    ("OutputSpeechTokenCount", "Sum"),
    ("CacheReadInputTokenCount", "Sum"),
    ("CacheWriteInputTokenCount", "Sum"),
    ("EstimatedTPMQuotaUsage", "Average"),
    ("InvocationClientErrors", "Sum"),
    ("LegacyModelInvocations", "Sum"),
    ("TimeToFirstToken", "Average"),
]

def collect_cloudwatch_metrics(session, account_id, region, days=14):
    """Fetch Bedrock CloudWatch metrics for the last N days."""
    cw = session.client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    metrics = []

    # Discover which models have metrics (paginate to get ALL)
    try:
        paginator = cw.get_paginator("list_metrics")
        model_ids = set()
        for page in paginator.paginate(Namespace="AWS/Bedrock", MetricName="Invocations"):
            for m in page.get("Metrics", []):
                for d in m.get("Dimensions", []):
                    if d["Name"] == "ModelId":
                        model_ids.add(d["Value"])
    except Exception:
        return metrics

    for model_id in model_ids:
        model_metrics = {
            "Model ID": model_id,
            "Region": region,
            "Account ID": account_id,
            "Metric Period": f"{days} days",
            "Metric Start": start.strftime("%Y-%m-%d"),
            "Metric End": end.strftime("%Y-%m-%d"),
        }
        for metric_name, stat in CW_METRICS:
            try:
                kwargs = {
                    "Namespace": "AWS/Bedrock",
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    "StartTime": start,
                    "EndTime": end,
                    "Period": 3600,  # 1-hour granularity, aggregate in code
                }
                if stat == "p95":
                    kwargs["ExtendedStatistics"] = ["p95"]
                else:
                    kwargs["Statistics"] = [stat]

                resp = cw.get_metric_statistics(**kwargs)
                dps = resp.get("Datapoints", [])
                if dps:
                    if stat == "p95":
                        vals = [dp.get("ExtendedStatistics", {}).get("p95", 0) for dp in dps]
                        val = max(vals) if vals else 0
                    elif stat == "Sum":
                        val = sum(dp.get("Sum", 0) for dp in dps)
                    elif stat == "Average":
                        vals = [dp.get("Average", 0) for dp in dps]
                        val = sum(vals) / len(vals) if vals else 0
                    model_metrics[f"{metric_name}_{stat}"] = round(val, 2)
            except Exception:
                pass
        metrics.append(model_metrics)
    return metrics


# ── 6. Cost Estimation from Token Counts ────────────────────────────────────

# Pricing per 1M tokens: (input, output)
MODEL_PRICING = {
    "claude-instant": (0.80, 2.40),
    "claude-v2": (8.00, 24.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-sonnet": (3.00, 15.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "titan-text-express": (0.20, 0.60),
    "titan-text-lite": (0.15, 0.20),
    "titan-text-premier": (0.50, 1.50),
    "titan-embed-text": (0.10, 0.0),
    "nova-micro": (0.035, 0.14),
    "nova-lite": (0.06, 0.24),
    "nova-pro": (0.80, 3.20),
    "nova-2-sonic": (0.60, 12.00),  # speech: input $0.60/1M, output $12.00/1M tokens
    "nova-sonic": (0.60, 12.00),
    "nova-canvas": (0.04, 0.04),  # image gen: ~$0.04 per image (approximated per 1M tokens)
    "nova-reel": (0.08, 0.08),  # video gen: approximated
    "llama2": (0.75, 1.00),
    "llama3": (0.72, 0.72),
    "llama3-3": (0.72, 0.72),
    "mistral-7b": (0.15, 0.20),
    "mistral-large": (4.00, 12.00),
}

def _match_pricing(model_id):
    """Best-effort match a model ID to pricing."""
    mid = model_id.lower().replace("us.", "").replace("global.", "")
    for key, price in MODEL_PRICING.items():
        if key.replace("-", "") in mid.replace("-", "").replace(".", "").replace("_", ""):
            return key, price
    return None, None

def calculate_costs(metrics, days):
    """Add cost estimates to each model's metrics based on token counts."""
    for m in metrics:
        model_id = m.get("Model ID", "")
        # Sum all input token types (text + speech + generic)
        input_tokens = (m.get("InputTokenCount_Sum", 0) or 0) + \
                       (m.get("InputTextTokenCount_Sum", 0) or 0) + \
                       (m.get("InputSpeechTokenCount_Sum", 0) or 0)
        # Sum all output token types
        output_tokens = (m.get("OutputTokenCount_Sum", 0) or 0) + \
                        (m.get("OutputTextTokenCount_Sum", 0) or 0) + \
                        (m.get("OutputSpeechTokenCount_Sum", 0) or 0)
        name, pricing = _match_pricing(model_id)
        if pricing and (input_tokens > 0 or output_tokens > 0):
            input_cost = (input_tokens / 1_000_000) * pricing[0]
            output_cost = (output_tokens / 1_000_000) * pricing[1]
            total = input_cost + output_cost
            m["Cost_Period_Days"] = days
            m["Cost_Input"] = round(input_cost, 2)
            m["Cost_Output"] = round(output_cost, 2)
            m["Cost_Total"] = round(total, 2)
            m["Cost_Monthly_Projected"] = round((total / days) * 30, 2)
            m["Cost_Annual_Projected"] = round((total / days) * 365, 2)
            m["Pricing_Model"] = name
    return metrics


# ── Main collection pipeline ────────────────────────────────────────────────

def collect_all(accounts, regions, output_dir, profile=None, days=14):
    os.makedirs(output_dir, exist_ok=True)

    all_quotas_by_model = []
    all_other_quotas = []
    all_models = []
    all_profiles = []
    all_settings = []
    all_metrics = []

    for account_id in accounts:
        for region in regions:
            _log(f"  Collecting {account_id} / {region}...")
            session = get_session(region, profile)

            # Quotas
            try:
                by_model, other = collect_quotas(session, account_id, region)
                all_quotas_by_model.extend(by_model)
                all_other_quotas.extend(other)
                _log(f"    Quotas: {len(by_model)} model, {len(other)} other")
            except Exception as e:
                _log(f"    Quotas failed: {e}")

            # Foundation models (only need once per region, not per account)
            try:
                models = collect_foundation_models(session, account_id, region)
                all_models.extend(models)
                _log(f"    Models: {len(models)}")
            except Exception as e:
                _log(f"    Models failed: {e}")

            # Inference profiles
            try:
                profiles = collect_inference_profiles(session, account_id, region)
                all_profiles.extend(profiles)
                _log(f"    Profiles: {len(profiles)}")
            except Exception as e:
                _log(f"    Profiles failed: {e}")

            # Logging config
            try:
                settings = collect_logging_config(session, account_id, region)
                all_settings.append(settings)
            except Exception as e:
                _log(f"    Settings failed: {e}")

            # CloudWatch metrics
            try:
                metrics = collect_cloudwatch_metrics(session, account_id, region, days=days)
                all_metrics.extend(metrics)
                _log(f"    Metrics: {len(metrics)} models with data")
            except Exception as e:
                _log(f"    Metrics failed: {e}")

    # Save in the same format analyze.py expects
    def save(name, data):
        path = os.path.join(output_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _log(f"  Saved {name}.json ({len(data)} entries)")

    save("QuotasByModelTable", all_quotas_by_model)
    save("OtherQuotasTable", all_other_quotas)
    save("FoundationModelsTable", all_models)
    save("InferenceProfilesSystemDefinedTable", all_profiles)
    save("AccountAndLogSettingsTable", all_settings)
    if all_metrics:
        all_metrics = calculate_costs(all_metrics, days)
        save("_metrics", all_metrics)

    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Bedrock data via public AWS APIs")
    parser.add_argument("--accounts", required=True, help="Comma-separated AWS account IDs")
    parser.add_argument("--regions", default="us-east-1,us-west-2", help="Comma-separated regions")
    parser.add_argument("--output", default=None, help="Output directory")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    parser.add_argument("--days", type=int, default=14, help="CloudWatch lookback period in days (default: 14, max: 455)")
    args = parser.parse_args()

    accounts = [a.strip() for a in args.accounts.split(",")]
    regions = [r.strip() for r in args.regions.split(",")]
    days = min(args.days, 455)  # CloudWatch max retention at hourly granularity
    output_dir = args.output or f"review_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    _log(f"Collecting Bedrock data for {len(accounts)} accounts × {len(regions)} regions (metrics: last {days} days)...")
    collect_all(accounts, regions, output_dir, args.profile, days=days)
    _log(f"\nDone. Output: {output_dir}/")
