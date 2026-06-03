#!/usr/bin/env python3
"""MetaGen Bedrock Operational Analysis — ETL + numerical stats from Dante output."""

import json, statistics, os, argparse
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path

SEP = "=" * 100

def num(v):
    if v is None or v is False or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None

def quota_val(entry, key):
    v = entry.get(key, False)
    return v.get("quota") if isinstance(v, dict) else None

def default_quota_val(entry, key):
    v = entry.get(key, False)
    return v.get("defaultQuota") if isinstance(v, dict) else None

def pct(used, limit):
    return round(used / limit * 100, 1) if used and limit and limit > 0 else None

def fmt(n, decimals=1):
    if n is None:
        return "N/A"
    if abs(n) >= 1e9:
        return f"{n/1e9:,.{decimals}f}B"
    if abs(n) >= 1e6:
        return f"{n/1e6:,.{decimals}f}M"
    if abs(n) >= 1e3:
        return f"{n/1e3:,.{decimals}f}K"
    return f"{n:,.{decimals}f}"

def dante_label(v):
    """Extract display label from a Dante nested object or return string as-is."""
    if isinstance(v, dict):
        return v.get("options", {}).get("label", v.get("value", ""))
    return str(v) if v else ""


def load_data(input_dir):
    base = Path(input_dir)
    # Map internal keys to possible filenames (new Dante format first, then legacy)
    FILE_MAP = {
        "QuotasByModel":      ["QuotasByModelTable.json", "Unformatted Service Quota.json", "Unformatted Service Quota Data.json"],
        "OtherQuotas":        ["OtherQuotasTable.json", "Service Quota.json"],
        "InferenceProfiles":  ["InferenceProfilesSystemDefinedTable.json", "Inference Profiles.json"],
        "FoundationModels":   ["FoundationModelsTable.json", "Foundation Model Availability.json"],
        "AccountSettings":    ["AccountAndLogSettingsTable.json"],
        "_metrics":           ["_metrics.json"],
    }
    data = {}
    for key, candidates in FILE_MAP.items():
        data[key] = []
        for fname in candidates:
            path = base / fname
            if path.exists():
                data[key] = json.loads(path.read_text())
                break
    return data


def analyze_quota(data, out):
    # Try new Dante format first, fall back to legacy
    quota_by_model = data.get("QuotasByModel", [])
    other_quotas = data.get("OtherQuotas", [])
    profiles = data.get("InferenceProfiles", [])
    models = data.get("FoundationModels", [])
    acct_settings = data.get("AccountSettings", [])

    # Detect schema: new format has "Model Name" (nested), legacy has "Model ID" (string)
    is_new_schema = quota_by_model and isinstance(quota_by_model[0].get("Model Name"), dict)
    is_legacy = quota_by_model and "Model ID" in quota_by_model[0]

    if not quota_by_model:
        out.append("No quota data found. Skipping quota analysis.")
        return

    if is_legacy:
        _analyze_legacy(quota_by_model, data.get("OtherQuotas", []), profiles, models, out)
        return

    # ── New Dante schema: quota config only (no CloudWatch metrics) ──
    out.append(f"Data: {len(quota_by_model)} model quotas, {len(other_quotas)} other quotas, "
               f"{len(profiles)} inference profiles, {len(models)} foundation models")

    # ── Model lifecycle (CRITICAL — check first) ──
    _analyze_lifecycle(models, quota_by_model, out, metrics=data.get("_metrics", []))

    # ── Quota asymmetry (CRIS gaps) ──
    _analyze_asymmetry(quota_by_model, out)

    # ── Per-model quota summary ──
    _analyze_new_quotas(quota_by_model, out)

    # ── Model inventory ──
    _analyze_models(models, out)

    # ── Account & Logging Settings (LOW priority — compliance/debugging only) ──
    if acct_settings:
        out.append(f"\n{SEP}")
        out.append("  SECTION 6: ACCOUNT & LOGGING SETTINGS")
        out.append(SEP)
        out.append(f"\n  {'Account':<18} {'Region':<12} {'CW Logging':<14} {'S3 Logging':<14} {'Text':<8} {'Image':<8} {'Embedding':<10}")
        out.append(f"  {'─'*18} {'─'*12} {'─'*14} {'─'*14} {'─'*8} {'─'*8} {'─'*10}")
        for s in acct_settings:
            out.append(f"  {s.get('Account Id','?'):<18} {s.get('Region','?'):<12} "
                       f"{'ON' if s.get('CloudWatch Logging') else 'OFF':<14} "
                       f"{'ON' if s.get('S3 Logging') else 'OFF':<14} "
                       f"{'ON' if s.get('Text') else 'OFF':<8} "
                       f"{'ON' if s.get('Image') else 'OFF':<8} "
                       f"{'ON' if s.get('Embedding') else 'OFF':<10}")
        out.append(f"\n  Note: Invocation logging captures request/response content for compliance/debugging.")
        out.append(f"  All operational metrics (invocations, throttles, latency, tokens) are available without logging.")

def _analyze_new_quotas(quota_by_model, out):
    """Section 2: Per-model quota config from new Dante schema."""
    out.append(f"\n{SEP}")
    out.append("  SECTION 2: PER-MODEL QUOTA CONFIGURATION")
    out.append(SEP)

    # Group by model name
    by_model = defaultdict(list)
    for e in quota_by_model:
        label = dante_label(e.get("Model Name", ""))
        by_model[label].append(e)

    # Show key models (Claude, Nova, Llama, Mistral) with non-default or interesting quotas
    KEY_MODELS = ['claude', 'nova', 'llama', 'mistral', 'titan']
    for model_name in sorted(by_model.keys()):
        entries = by_model[model_name]
        if not any(k in model_name.lower() for k in KEY_MODELS):
            continue
        out.append(f"\n  {model_name}")
        out.append(f"  {'Account':<18} {'Region':<12} {'Inference Type':<50} {'TPM':>14} {'RPM':>10}")
        out.append(f"  {'─'*18} {'─'*12} {'─'*50} {'─'*14} {'─'*10}")
        for e in sorted(entries, key=lambda x: (x.get('Account ID',''), x.get('Region',''), x.get('Inference Type',''))):
            tpm = e.get('TPM', '---')
            rpm = e.get('RPM', '---')
            out.append(f"  {e.get('Account ID','?'):<18} {e.get('Region','?'):<12} "
                       f"{e.get('Inference Type','?'):<50} {tpm:>14} {rpm:>10}")


def _analyze_asymmetry(quota_by_model, out):
    """Section 3: Flag quota asymmetries between accounts/regions and CRIS vs Global CRIS gaps."""
    out.append(f"\n{SEP}")
    out.append("  SECTION 3: QUOTA ASYMMETRY & CRIS GAP ANALYSIS")
    out.append(SEP)

    # Build lookup: (model, inference_type) -> {(account, region): tpm}
    lookup = defaultdict(dict)
    for e in quota_by_model:
        label = dante_label(e.get("Model Name", ""))
        itype = e.get("Inference Type", "")
        tpm = num(e.get("TPM"))
        key = (label, itype)
        lookup[key][(e.get("Account ID",""), e.get("Region",""))] = tpm

    # Find CRIS vs Global CRIS gaps
    out.append(f"\n  CRIS vs Global CRIS gaps (where Global < Regional by >2x):")
    out.append(f"  {'Model':<40} {'Account':<18} {'Region':<12} {'CRIS TPM':>14} {'Global TPM':>14} {'Gap':>8}")
    out.append(f"  {'─'*40} {'─'*18} {'─'*12} {'─'*14} {'─'*14} {'─'*8}")
    found_gap = False
    for e in quota_by_model:
        if "Cross-region" not in e.get("Inference Type", "") or "Global" in e.get("Inference Type", ""):
            continue
        label = dante_label(e.get("Model Name", ""))
        acct, region = e.get("Account ID",""), e.get("Region","")
        cris_tpm = num(e.get("TPM"))
        # Find matching Global CRIS
        global_tpm = lookup.get((label, "Global cross-region model inference"), {}).get((acct, region))
        if cris_tpm and global_tpm and cris_tpm > global_tpm * 2:
            ratio = f"{cris_tpm/global_tpm:.0f}x"
            out.append(f"  {label:<40} {acct:<18} {region:<12} {fmt(cris_tpm):>14} {fmt(global_tpm):>14} {ratio:>8}")
            found_gap = True
    if not found_gap:
        out.append("  No significant CRIS vs Global CRIS gaps found.")

    # Find cross-account asymmetry for same model+region+type
    accounts = list(set(e.get("Account ID","") for e in quota_by_model))
    if len(accounts) >= 2:
        out.append(f"\n  Cross-account quota differences (>5x) for same model/region/type:")
        out.append(f"  {'Model':<40} {'Region':<12} {'Type':<30} {'Acct1 TPM':>14} {'Acct2 TPM':>14} {'Ratio':>8}")
        out.append(f"  {'─'*40} {'─'*12} {'─'*30} {'─'*14} {'─'*14} {'─'*8}")
        found_asym = False
        seen = set()
        for (label, itype), acct_map in lookup.items():
            vals = [(ar, t) for ar, t in acct_map.items() if t]
            for i, (ar1, t1) in enumerate(vals):
                for ar2, t2 in vals[i+1:]:
                    if ar1[1] != ar2[1]:  # same region only
                        continue
                    hi, lo = max(t1, t2), min(t1, t2)
                    if lo > 0 and hi / lo > 5:
                        k = (label, ar1[1], itype)
                        if k not in seen:
                            seen.add(k)
                            out.append(f"  {label:<40} {ar1[1]:<12} {itype[:30]:<30} {fmt(t1):>14} {fmt(t2):>14} {hi/lo:>7.0f}x")
                            found_asym = True
        if not found_asym:
            out.append("  No significant cross-account asymmetries found.")


def _analyze_models(models, out):
    """Section 4: Model inventory from FoundationModelsTable."""
    if not models:
        return
    out.append(f"\n{SEP}")
    out.append("  SECTION 4: MODEL INVENTORY")
    out.append(SEP)

    def _lifecycle(m):
        lc = m.get("Lifecycle", {})
        return lc.get("value", "") if isinstance(lc, dict) else str(lc)

    lifecycle = Counter(_lifecycle(m) for m in models)
    providers = Counter(m.get("Provider", "") for m in models)
    unique = len(set(m.get("Model ID", "") for m in models))
    out.append(f"\n  Total entries: {len(models)}  |  Unique models: {unique}  |  Lifecycle: {dict(lifecycle)}")
    out.append(f"  Providers: {', '.join(f'{p}({c})' for p, c in sorted(providers.items(), key=lambda x: -x[1])[:10])}")




# ── MODEL LIFECYCLE UPGRADE MAP ──
MODEL_UPGRADE_MAP = {
    "anthropic.claude-instant": ("Claude 3.5 Haiku", "anthropic.claude-3-5-haiku-20241022-v1:0", "$0.80/$2.40 → $0.80/$4.00"),
    "anthropic.claude-v2": ("Claude 3.5 Sonnet V2", "anthropic.claude-3-5-sonnet-20241022-v2:0", "$8.00/$24.00 → $3.00/$15.00 (SAVES 63%/38%)"),
    "anthropic.claude-3-sonnet-20240229": ("Claude Sonnet 4", "us.anthropic.claude-sonnet-4-v1:0", "$3.00/$15.00 → $3.00/$15.00 (same cost)"),
    "anthropic.claude-3-haiku-20240307": ("Claude 3.5 Haiku", "anthropic.claude-3-5-haiku-20241022-v1:0", "$0.25/$1.25 → $0.80/$4.00 (+220%)"),
    "anthropic.claude-3-opus": ("Claude Opus 4", "us.anthropic.claude-opus-4-v1:0", "$15.00/$75.00 → $15.00/$75.00 (same cost)"),
    "anthropic.claude-3-5-sonnet-20240620": ("Claude 3.5 Sonnet V2", "anthropic.claude-3-5-sonnet-20241022-v2:0", "$3.00/$15.00 → $3.00/$15.00 (same)"),
    "anthropic.claude-3-5-haiku-20241022": ("Claude Haiku 4.5 (CRIS)", "us.anthropic.claude-haiku-4-5-v1:0", "$0.80/$4.00 → $0.80/$4.00 (same)"),
    "anthropic.claude-3-7-sonnet-20250219": ("Claude Sonnet 4.5 (CRIS)", "us.anthropic.claude-sonnet-4-5-v1:0", "$3.00/$15.00 → $3.00/$15.00 (same)"),
    "anthropic.claude-sonnet-4-20250514": ("Claude Sonnet 4.5 (CRIS)", "us.anthropic.claude-sonnet-4-5-v1:0", "$3.00/$15.00 → $3.00/$15.00 (same)"),
    "anthropic.claude-opus-4-20250514": ("Claude Opus 4.5 (CRIS)", "us.anthropic.claude-opus-4-5-v1:0", "$15.00/$75.00 → $15.00/$75.00 (same)"),
    "meta.llama2": ("Llama 3.3 70B", "meta.llama3-3-70b-instruct-v1:0", "$0.75/$1.00 → $0.72/$0.72 (SAVES)"),
    "meta.llama3-8b-instruct": ("Llama 3.1 8B", "meta.llama3-1-8b-instruct-v1:0", "$0.22/$0.22 → $0.22/$0.22 (same)"),
    "meta.llama3-70b-instruct": ("Llama 3.3 70B", "meta.llama3-3-70b-instruct-v1:0", "$0.72/$0.72 → $0.72/$0.72 (same)"),
    "meta.llama3-1-405b-instruct": ("Llama 4 Maverick", "meta.llama4-maverick-v1:0", "check current pricing"),
    "meta.llama3-2-11b-instruct": ("Llama 4 Scout", "meta.llama4-scout-v1:0", "$0.16/$0.16 → check pricing"),
    "meta.llama3-2-90b-instruct": ("Llama 4 Maverick", "meta.llama4-maverick-v1:0", "$0.72/$0.72 → check pricing"),
    "cohere.command-r-v1": ("Command R+", "cohere.command-r-plus-v1:0", "$0.50/$1.50 → $3.00/$15.00 (+500%)"),
    "cohere.command-r-plus-v1": ("Migrate to Claude/Nova", None, "Cohere phasing out — consider Claude 3.5 Sonnet or Nova Pro"),
    "amazon.titan-text-express": ("Nova Micro", "amazon.nova-micro-v1:0", "$0.20/$0.60 → $0.035/$0.14 (SAVES 82%/77%)"),
    "amazon.titan-text-lite": ("Nova Micro", "amazon.nova-micro-v1:0", "$0.15/$0.20 → $0.035/$0.14 (SAVES 77%/30%)"),
    "amazon.titan-text-premier": ("Nova Lite", "amazon.nova-lite-v1:0", "$0.50/$1.50 → $0.06/$0.24 (SAVES 88%/84%)"),
    "amazon.titan-image-generator-v2": ("Nova Canvas", "amazon.nova-canvas-v1:0", "check image gen pricing"),
    "amazon.nova-premier-v1": ("Nova Pro / Nova 2 Pro", "amazon.nova-pro-v1:0", "check current pricing"),
    "mistral.mistral-7b-instruct": ("Mistral Small", "mistral.mistral-small-2402-v1:0", "$0.15/$0.20 → $0.10/$0.30"),
    "mistral.mixtral-8x7b-instruct": ("Mistral Large", "mistral.mistral-large-2407-v1:0", "$0.45/$0.70 → $2.00/$6.00"),
}

def _find_upgrade(model_id):
    for prefix, info in MODEL_UPGRADE_MAP.items():
        if model_id.startswith(prefix):
            return info
    return None


def _analyze_lifecycle(models, quota_by_model, out, metrics=None):
    """Section 5: Model Lifecycle — only flags LEGACY models with actual traffic."""
    if not models:
        return

    out.append(f"\n{SEP}")
    out.append("  SECTION 5: MODEL LIFECYCLE & UPGRADE RECOMMENDATIONS")
    out.append(SEP)

    def _lc(m):
        lc = m.get("Lifecycle", {})
        return lc.get("value", "") if isinstance(lc, dict) else str(lc)

    # Build unique model set
    unique = {}
    for m in models:
        mid = m.get("Model ID", "")
        base = mid.split(":0:")[0] if ":0:" in mid else mid
        if base not in unique:
            unique[base] = {"model_id": mid, "base_id": base, "provider": m.get("Provider",""),
                           "lifecycle": _lc(m), "regions": set()}
        unique[base]["regions"].add(m.get("Region",""))

    legacy = {k:v for k,v in unique.items() if v["lifecycle"] == "LEGACY"}
    active = {k:v for k,v in unique.items() if v["lifecycle"] == "ACTIVE"}

    # Find models with actual CloudWatch invocations
    models_with_traffic = set()
    if metrics:
        for m in metrics:
            if m.get("Invocations_Sum", 0) > 0:
                models_with_traffic.add(m.get("Model ID", ""))

    # Build metrics lookup by model ID
    metrics_by_model = {}
    if metrics:
        for m in metrics:
            metrics_by_model[m.get("Model ID", "")] = m

    out.append(f"\n  Total unique models in catalog: {len(unique)}  |  ACTIVE: {len(active)}  |  LEGACY: {len(legacy)}  |  Legacy %: {len(legacy)/max(len(unique),1)*100:.1f}%")

    if not models_with_traffic:
        out.append(f"\n  ⚠ No CloudWatch metrics available — cannot determine which LEGACY models have active traffic.")
        out.append(f"\n  LEGACY models in catalog (may or may not be in use):")
        for base_id, info in sorted(legacy.items(), key=lambda x: x[1]["provider"]):
            upgrade = _find_upgrade(base_id)
            uname = upgrade[0] if upgrade else "Review manually"
            out.append(f"    {info['model_id']:<55} {info['provider']:<12} → {uname}")
    else:
        # Only flag LEGACY models with actual invocations
        legacy_with_traffic = {k:v for k,v in legacy.items()
                               if any(v["model_id"] in mid or k in mid for mid in models_with_traffic)}
        legacy_no_traffic = {k:v for k,v in legacy.items() if k not in legacy_with_traffic}

        if legacy_with_traffic:
            out.append(f"\n  ⚠ LEGACY models WITH active traffic ({len(legacy_with_traffic)}) — migration required:")
            out.append(f"  {'Model ID':<55} {'Invocations':<14} {'Spend (period)':<16} {'Projected/mo':<14} {'Upgrade To':<28} {'Cost After'}")
            out.append(f"  {'─'*55} {'─'*14} {'─'*16} {'─'*14} {'─'*28} {'─'*14}")
            for base_id, info in sorted(legacy_with_traffic.items()):
                mid = info["model_id"]
                # Find metrics for this model (try exact and CRIS variants)
                mm = metrics_by_model.get(mid) or metrics_by_model.get(f"us.{mid}") or {}
                invocations = int(mm.get("Invocations_Sum", 0))
                period_days = mm.get("Cost_Period_Days") or mm.get("Metric Period", "").replace(" days", "") or "14"
                spend = mm.get("Cost_Total", 0)
                monthly = mm.get("Cost_Monthly_Projected", 0)
                spend_str = f"${spend:.2f}/{period_days}d" if spend else "N/A"
                monthly_str = f"${monthly:.2f}/mo" if monthly else "N/A"
                inv_str = f"{invocations:,}" if invocations else "N/A"

                upgrade = _find_upgrade(base_id)
                if upgrade:
                    name, new_id, pricing = upgrade
                    out.append(f"  {mid:<55} {inv_str:<14} {spend_str:<16} {monthly_str:<14} {name:<28} {pricing[:14]}")
                else:
                    out.append(f"  {mid:<55} {inv_str:<14} {spend_str:<16} {monthly_str:<14} {'⚠ Review':<28} {'—'}")

            # Total financial impact
            total_spend = sum(metrics_by_model.get(info["model_id"], metrics_by_model.get(f"us.{info['model_id']}", {})).get("Cost_Total", 0)
                              for info in legacy_with_traffic.values())
            total_monthly = sum(metrics_by_model.get(info["model_id"], metrics_by_model.get(f"us.{info['model_id']}", {})).get("Cost_Monthly_Projected", 0)
                                for info in legacy_with_traffic.values())
            if total_spend > 0:
                out.append(f"\n  💰 Total LEGACY model spend: ${total_spend:.2f} in period  |  Projected: ${total_monthly:.2f}/month  |  ${total_monthly*12:.2f}/year")
        else:
            out.append(f"\n  ✅ No LEGACY models with active traffic detected.")

        if legacy_no_traffic:
            out.append(f"\n  {len(legacy_no_traffic)} additional LEGACY models in catalog with no detected traffic (skipped).")


def _analyze_legacy(quota_raw, quota_fmt, profiles, models, out):
    """Legacy Dante schema with embedded CloudWatch metrics."""
    eval_start = quota_raw[0].get("EvalRangeStart", "")
    eval_end = quota_raw[0].get("EvalRangeEnd", "")
    out.append(f"Evaluation window: {eval_start[:10]} to {eval_end[:10]} (14 days)")
    out.append(f"Data: {len(quota_raw)} model-account combos, {len(profiles)} inference profiles, {len(models)} foundation models")

    out.append(f"\n{SEP}")
    out.append("  SECTION 1: PER-MODEL USAGE & QUOTA SUMMARY")
    out.append(SEP)

    for e in quota_raw:
        mid = e["Model ID"]
        acct = e["Account ID"]
        region = e["Region"]
        max_inv = num(e.get("Max Invocations per minute"))
        inv_p95 = num(e.get("Invocations per minute (P95)"))
        inv_avg = num(e.get("invocationAvg"))
        max_io = num(e.get("Max IOTokens per minute"))
        io_p95 = num(e.get("IOTokens per minute (P95)"))
        io_avg = num(e.get("ioTokenAvg"))
        in_avg = num(e.get("inputTokenAvg"))
        out_avg_tok = num(e.get("outTokenAvg"))
        lat_avg = num(e.get("invocationLatencyAvg"))
        lat_p95 = num(e.get("invocationLatencyP95"))
        lat_max = num(e.get("invocationLatencyMax"))
        throttle_avg = num(e.get("invocationThrottleAvg"))
        throttle_max = num(e.get("Max InvocationThrottle per minute"))

        eff_rpm = quota_val(e, "CRIS_RPM") or quota_val(e, "RPM")
        eff_tpm = quota_val(e, "CRIS_TPM") or quota_val(e, "TPM")
        rpm_src = "CRIS" if quota_val(e, "CRIS_RPM") else ("On-Demand" if quota_val(e, "RPM") else "NONE")
        tpm_src = "CRIS" if quota_val(e, "CRIS_TPM") else ("On-Demand" if quota_val(e, "TPM") else "NONE")
        io_ratio = round(in_avg / out_avg_tok, 1) if in_avg and out_avg_tok and out_avg_tok > 0 else None

        out.append(f"\n{'─'*100}")
        out.append(f"  Model: {mid}")
        out.append(f"  Account: {acct}  |  Region: {region}  |  RPM: {rpm_src}  |  TPM: {tpm_src}")
        out.append(f"{'─'*100}")
        out.append(f"  Invocations:  Avg={fmt(inv_avg)}  P95={fmt(inv_p95)}  Max={fmt(max_inv)}  Quota={fmt(eff_rpm)}  Peak%={pct(max_inv, eff_rpm)}%")
        out.append(f"  IO Tokens:    Avg={fmt(io_avg)}  P95={fmt(io_p95)}  Max={fmt(max_io)}  Quota={fmt(eff_tpm)}  Peak%={pct(max_io, eff_tpm)}%")
        out.append(f"  I/O Ratio:    {f'{io_ratio}:1' if io_ratio else 'N/A'}")
        if lat_avg:
            out.append(f"  Latency:      Avg={lat_avg/1000:,.1f}s  P95={lat_p95/1000:,.1f}s  Max={lat_max/1000:,.1f}s")
        if throttle_avg is not None and inv_avg:
            rate = throttle_avg / (inv_avg + throttle_avg) * 100 if (inv_avg + throttle_avg) > 0 else 0
            out.append(f"  Throttling:   Avg={fmt(throttle_avg)}/min  Max={fmt(throttle_max)}/min  Rate={rate:.1f}%")

    # ── Aggregates ──
    out.append(f"\n{SEP}")
    out.append("  SECTION 2: AGGREGATE STATISTICS")
    out.append(SEP)

    total_io = sum(num(e.get("ioTokenAvg")) or 0 for e in quota_raw)
    total_inv = sum(num(e.get("invocationAvg")) or 0 for e in quota_raw)
    total_thr = sum(num(e.get("invocationThrottleAvg")) or 0 for e in quota_raw if num(e.get("invocationThrottleAvg")))

    out.append(f"\n  Combined avg tokens/min:      {fmt(total_io)}")
    out.append(f"  Combined avg invocations/min: {fmt(total_inv)}")
    out.append(f"  Combined avg throttles/min:   {fmt(total_thr)}")
    if total_inv > 0:
        out.append(f"  Overall throttle rate:        {total_thr / (total_inv + total_thr) * 100:.2f}%")

    out.append(f"\n  Top models by avg IO tokens/min:")
    for i, e in enumerate(sorted(quota_raw, key=lambda x: num(x.get("ioTokenAvg")) or 0, reverse=True)[:5], 1):
        out.append(f"    {i}. {e['Model ID']:<55} {fmt(num(e.get('ioTokenAvg')))} (acct: {e['Account ID']})")

    out.append(f"\n  Top models by avg throttles/min:")
    for i, e in enumerate(sorted(quota_raw, key=lambda x: num(x.get("invocationThrottleAvg")) or 0, reverse=True)[:5], 1):
        t = num(e.get("invocationThrottleAvg"))
        if t and t > 0:
            out.append(f"    {i}. {e['Model ID']:<55} {fmt(t)} (acct: {e['Account ID']})")

    # ── Risk scoring ──
    out.append(f"\n{SEP}")
    out.append("  SECTION 3: RISK SCORING")
    out.append(SEP)

    risks = []
    for e in quota_raw:
        mid, acct = e["Model ID"], e["Account ID"]
        score, findings = 0, []
        max_inv = num(e.get("Max Invocations per minute"))
        max_io = num(e.get("Max IOTokens per minute"))
        eff_rpm = quota_val(e, "CRIS_RPM") or quota_val(e, "RPM")
        eff_tpm = quota_val(e, "CRIS_TPM") or quota_val(e, "TPM")

        rpm_pct = pct(max_inv, eff_rpm)
        if rpm_pct and rpm_pct > 100:
            score += 30; findings.append(f"RPM EXCEEDED: {fmt(max_inv)}/{fmt(eff_rpm)} ({rpm_pct}%)")
        elif rpm_pct and rpm_pct > 80:
            score += 15; findings.append(f"RPM near limit: {rpm_pct}%")

        tpm_pct = pct(max_io, eff_tpm)
        if tpm_pct and tpm_pct > 100:
            score += 30; findings.append(f"TPM EXCEEDED: {fmt(max_io)}/{fmt(eff_tpm)} ({tpm_pct}%)")
        elif tpm_pct and tpm_pct > 80:
            score += 15; findings.append(f"TPM near limit: {tpm_pct}%")

        ta, ia = num(e.get("invocationThrottleAvg")), num(e.get("invocationAvg"))
        if ta and ia and ia > 0:
            rate = ta / (ia + ta) * 100
            if rate > 10:
                score += 25; findings.append(f"Throttle rate: {rate:.1f}%")

        if not quota_val(e, "CRIS_RPM") and not quota_val(e, "CRIS_TPM"):
            if quota_val(e, "RPM") or quota_val(e, "TPM"):
                score += 15; findings.append("On-demand only, no CRIS")

        for qk, label in [("GLOBAL_CRIS_RPM", "Global CRIS RPM"), ("GLOBAL_CRIS_TPM", "Global CRIS TPM")]:
            gq, dq = quota_val(e, qk), default_quota_val(e, qk)
            if gq and dq and gq == dq:
                score += 10; findings.append(f"{label} at default ({fmt(gq)})")

        risks.append((score, mid, acct, findings))

    risks.sort(reverse=True)
    out.append(f"\n  {'Risk':<6} {'Model':<55} {'Account':<15} Findings")
    out.append(f"  {'─'*6} {'─'*55} {'─'*15} {'─'*50}")
    for score, mid, acct, findings in risks:
        sev = "CRIT" if score >= 50 else "HIGH" if score >= 25 else "LOW"
        out.append(f"  {sev:<8} {mid:<55} {acct:<15} {findings[0] if findings else ''}")
        for f in findings[1:]:
            out.append(f"  {'':8} {'':55} {'':15} {f}")

    # ── Time-series from Service Quota (chart data) ──
    if quota_fmt:
        out.append(f"\n{SEP}")
        out.append("  SECTION 4: TIME-SERIES ANALYSIS (from chart data)")
        out.append(SEP)

        for entry in quota_fmt:
            mid, acct = entry["Model ID"], entry["Account ID"]
            out.append(f"\n  {mid} (acct: {acct})")
            for ck in ["UsageQuotaSummaryChartInvocations", "UsageQuotaSummaryChartTokens", "UsageQuotaSummaryChartLatencyTokens"]:
                chart = entry.get(ck, {})
                for ds in chart.get("data", {}).get("datasets", []):
                    label = ds.get("label", "?")
                    if isinstance(label, list):
                        label = " ".join(label)
                    vals = [num(p["y"]) for p in ds.get("data", []) if "y" in p and p["y"] is not None]
                    vals = [v for v in vals if v is not None]
                    if len(vals) <= 2:
                        continue
                    mean_v = statistics.mean(vals)
                    p95 = sorted(vals)[int(len(vals) * 0.95)]
                    cv = statistics.stdev(vals) / mean_v if mean_v > 0 else 0
                    burst = "highly bursty" if cv > 1.5 else "bursty" if cv > 1.0 else "moderate" if cv > 0.5 else "steady"
                    out.append(f"    {label[:50]:<52} Mean={fmt(mean_v):>10}  P95={fmt(p95):>10}  Max={fmt(max(vals)):>10}  CV={cv:.2f} ({burst})")

    # ── Model inventory ──
    if models:
        out.append(f"\n{SEP}")
        out.append("  SECTION 5: MODEL INVENTORY")
        out.append(SEP)
        lifecycle = Counter(m.get("Model Lifecycle", {}).get("Status", "") for m in models)
        providers = Counter(m.get("Provider Name", "") for m in models)
        unique = len(set(m.get("Model ID(s)", "") for m in models))
        out.append(f"\n  Total entries: {len(models)}  |  Unique models: {unique}  |  Lifecycle: {dict(lifecycle)}")
        out.append(f"  Providers: {', '.join(f'{p}({c})' for p, c in sorted(providers.items(), key=lambda x: -x[1])[:10])}")


def run(input_dir, output_file=None):
    data = load_data(input_dir)
    out = []
    out.append(f"\n{SEP}")
    out.append(f"  Bedrock Operational Analysis")
    out.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"  Source: {input_dir}")
    out.append(SEP)

    analyze_quota(data, out)

    out.append(f"\n{SEP}")
    out.append("  END OF ANALYSIS")
    out.append(f"{SEP}\n")

    report = "\n".join(out)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report saved to {output_file}", file=__import__("sys").stderr)
    else:
        print(report, file=__import__("sys").stderr)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze Bedrock Dante output")
    parser.add_argument("--input", required=True, help="Dante output directory")
    parser.add_argument("--output", help="Save report to file")
    args = parser.parse_args()
    run(args.input, args.output)
