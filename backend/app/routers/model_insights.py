"""Model Insights endpoint — per-model deep-dive across the customer's fleet.

Returns one row per modelId with usage volumes, error rate, cache hit %,
average request shape, and an APPROXIMATE cost estimate. The cost number
here is intentionally rough (in-code price table); the Cost tab uses
real Cost Explorer numbers. We label it `cost_estimate_usd` to make the
distinction explicit.

The pricing table is product opinion (per the spec) — kept in code, not
JSON, not scraped. Update when a customer asks about a model not listed.

Cache-hit formula:
    cache_hit_pct = cache_read / (cache_read + total_input_tokens) * 100
NOT cache_read / total_input_tokens — that produces values >100% because
CloudWatch's InputTokenCount excludes cached tokens by definition.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


# ---------------------------------------------------------------------------
# Approximate Bedrock pricing — for relative cost-share visualization only.
# Real $ are in the Cost tab from Cost Explorer.
#
# Numbers are USD per 1M tokens. Provider-level — not per-model — because
# Bedrock prices vary widely WITHIN a provider, and we don't want to
# maintain a 60-model price table that drifts the moment AWS updates rates.
# ---------------------------------------------------------------------------
BEDROCK_PRICING = {
    "anthropic": {"input": 3.00,  "output": 15.00},
    "amazon":    {"input": 0.20,  "output":  0.80},
    "meta":      {"input": 0.50,  "output":  1.50},
    "cohere":    {"input": 0.50,  "output":  1.50},
    "mistral":   {"input": 0.50,  "output":  1.50},
    "ai21":      {"input": 0.50,  "output":  1.50},
    "deepseek":  {"input": 0.50,  "output":  1.50},
    "qwen":      {"input": 0.50,  "output":  1.50},
    "writer":    {"input": 0.50,  "output":  1.50},
    "google":    {"input": 0.50,  "output":  1.50},
    "twelvelabs":{"input": 0.50,  "output":  1.50},
    "nvidia":    {"input": 0.50,  "output":  1.50},
    "moonshotai":{"input": 0.50,  "output":  1.50},
    "forge":     {"input": 0.50,  "output":  1.50},
    # Stability AI is image/video — token-based pricing doesn't really
    # apply, but keep an entry so the cost-share pie still draws and
    # doesn't fall into the generic 0.50/1.50 default that's worse.
    "stability": {"input": 0.04,  "output":  0.04},
}


def _provider_of(model_id: str) -> str:
    """First segment of the modelId, with CRIS prefixes (us./eu./global.)
    stripped — so `us.anthropic.claude-…` resolves to `anthropic`."""
    if not model_id:
        return "unknown"
    parts = model_id.split(".")
    if len(parts) >= 2 and parts[0] in ("us", "eu", "apac", "jp", "au", "ca", "amer", "global"):
        return parts[1] if len(parts) > 1 else "unknown"
    return parts[0]


def _public_name(model_id: str) -> str:
    """Best-effort human name from the modelId. Strips CRIS prefix and
    trailing date/version segments, joins single-digit tokens with dots
    so version numbers read naturally:

        anthropic.claude-3-5-sonnet-20241022-v2:0  →  'Claude 3.5 Sonnet'
        amazon.nova-pro-v1:0                       →  'Nova Pro'
        meta.llama3-1-70b-instruct-v1:0            →  'Llama3 1 70b Instruct'  ← fallback

    Falls back to the bare modelId if anything goes sideways.
    """
    if not model_id:
        return ""
    parts = model_id.split(".")
    if len(parts) >= 2 and parts[0] in ("us", "eu", "apac", "jp", "au", "ca", "amer", "global"):
        parts = parts[1:]
    if len(parts) < 2:
        return model_id
    name_part = parts[1].split(":")[0]
    raw_tokens = []
    for t in name_part.split("-"):
        if t.isdigit() and len(t) >= 6:
            break  # date stamp
        if t.startswith("v") and t[1:].replace("_", "").isdigit():
            break  # version suffix
        raw_tokens.append(t)

    # Collapse runs of single-digit tokens into a dotted version number
    # (e.g. ['claude','3','5','sonnet'] → ['Claude','3.5','Sonnet']).
    out = []
    i = 0
    while i < len(raw_tokens):
        t = raw_tokens[i]
        if t.isdigit() and len(t) <= 2:
            digits = [t]
            j = i + 1
            while j < len(raw_tokens) and raw_tokens[j].isdigit() and len(raw_tokens[j]) <= 2:
                digits.append(raw_tokens[j])
                j += 1
            out.append(".".join(digits))
            i = j
        elif t.lower() in ("ai", "llm"):
            out.append(t.upper())
            i += 1
        else:
            out.append(t.capitalize())
            i += 1
    return " ".join(out) if out else model_id


@router.get("/model-insights")
async def model_insights(f: FilterSet = Depends(parse_filters)):
    where = build_where(f, has_traffic_type=True, has_account=True)
    rows = await db.fetch(
        f"""
        SELECT
            modelId,
            SUM(total_requests)::BIGINT          AS total_requests,
            SUM(failed_requests)::BIGINT         AS failed_requests,
            SUM(status_429_count)::BIGINT        AS throttled,
            SUM(total_input_tokens)::BIGINT      AS input_tokens,
            SUM(total_output_tokens)::BIGINT     AS output_tokens,
            SUM(total_cache_read_input_tokens)::BIGINT AS cache_read_tokens,
            COUNT(DISTINCT accountId)::INT       AS unique_accounts
        FROM f_daily
        WHERE {where.sql}
        GROUP BY modelId
        HAVING SUM(total_requests) > 0
        ORDER BY total_requests DESC
        """,
        *where.params,
    )

    out = []
    for r in rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        total_req     = int(r["total_requests"] or 0)
        failed        = int(r["failed_requests"] or 0)
        throttled     = int(r["throttled"] or 0)
        input_tokens  = int(r["input_tokens"] or 0)
        output_tokens = int(r["output_tokens"] or 0)
        cache_read    = int(r["cache_read_tokens"] or 0)

        provider = _provider_of(mid)
        avg_in   = (input_tokens / total_req) if total_req else 0
        avg_out  = (output_tokens / total_req) if total_req else 0
        io_ratio = (input_tokens / output_tokens) if output_tokens else 0
        cache_denom = cache_read + input_tokens
        cache_pct   = (cache_read / cache_denom * 100) if cache_denom else 0
        error_rate  = (failed / total_req * 100) if total_req else 0

        pricing = BEDROCK_PRICING.get(provider, {"input": 0.50, "output": 1.50})
        cost_est = (input_tokens / 1_000_000) * pricing["input"] \
                 + (output_tokens / 1_000_000) * pricing["output"]

        out.append({
            "modelId":          mid,
            "public_name":      _public_name(mid),
            "provider":         provider,
            "total_requests":   total_req,
            "failed_requests":  failed,
            "throttled":        throttled,
            "input_tokens":     input_tokens,
            "output_tokens":    output_tokens,
            "cache_read_tokens": cache_read,
            "cache_hit_pct":    round(cache_pct, 2),
            "avg_input":        round(avg_in, 1),
            "avg_output":       round(avg_out, 1),
            "io_ratio":         round(io_ratio, 2),
            "error_rate":       round(error_rate, 3),
            "unique_accounts":  int(r["unique_accounts"] or 0),
            "cost_estimate_usd": round(cost_est, 2),
        })
    return out
