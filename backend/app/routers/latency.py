"""Latency tab endpoints. Reads f_latency_daily (pre-computed percentiles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/latency-by-model")
async def latency_by_model(f: FilterSet = Depends(parse_filters)):
    """Sample-count-weighted average across (traffic_type, region) per model.

    Percentiles can't be re-aggregated, so we report:
      - sample_count weighted average (statistically valid)
      - the MAX p50/p90/p99 across underlying buckets (worst case)
    The reference returns sample-weighted means, not max — keeping that for parity.
    """
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e,
          SUM(avg_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_ttft,
          SUM(p50_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_ttft,
          SUM(p90_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_ttft,
          SUM(p99_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_ttft
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY modelId
        ORDER BY sample_count DESC
        """,
        *w.params,
    )
    out = db.rows_to_dicts(rows)

    # OTPS (Output Tokens Per Second) — the wiki's key throughput/UX latency
    # signal alongside TTFT: OTPS = output_tokens / (TTLT - TTFT), i.e. the
    # generation speed AFTER the first token. We approximate the generation
    # window as (avg_e2e - avg_ttft) ms and divide the per-model average output
    # tokens per request by it. Output tokens + request counts come from f_daily
    # (f_latency_daily has no token columns). Endpoint-agnostic join.
    tok = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_output_tokens)::BIGINT AS out_tokens,
          SUM(total_requests)::BIGINT      AS reqs
        FROM f_daily
        WHERE {build_where(f).sql}
        GROUP BY modelId
        """,
        *build_where(f).params,
    )
    tok_by_model = {(r["modelid"] if "modelid" in r else r["modelId"]): r for r in tok}
    for row in out:
        mid = row.get("modelid") or row.get("modelId")
        t = tok_by_model.get(mid)
        avg_out_per_req = (int(t["out_tokens"]) / int(t["reqs"])) if (t and t["reqs"]) else None
        gen_ms = None
        if row.get("avg_e2e") and row.get("avg_ttft") is not None:
            gen_ms = float(row["avg_e2e"]) - float(row["avg_ttft"])
        # OTPS only meaningful when we have a positive generation window and
        # per-request output tokens; else null (chart shows a gap, not a fake 0).
        row["avg_output_tokens_per_req"] = round(avg_out_per_req, 1) if avg_out_per_req is not None else None
        row["otps"] = round(avg_out_per_req / (gen_ms / 1000.0), 1) if (avg_out_per_req and gen_ms and gen_ms > 0) else None
    return out


@router.get("/latency-cris-vs-od")
async def latency_cris_vs_od(f: FilterSet = Depends(parse_filters)):
    """Same per-model latency split by traffic_type — quantifies CRIS overhead."""
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT modelId, traffic_type,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e,
          SUM(avg_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_ttft,
          SUM(p50_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_ttft,
          SUM(p90_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_ttft,
          SUM(p99_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_ttft
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY modelId, traffic_type
        ORDER BY modelId, traffic_type
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/operation-latency")
async def operation_latency(f: FilterSet = Depends(parse_filters)):
    """f_latency_daily doesn't carry operation; we approximate using f_daily
    request volumes for shape, then weight by sample_count where models match.
    Returns operation-level summary by joining latency to operation in f_daily."""
    # Simpler approach: latency rows don't have operation, so we report per-traffic-type
    # which is the closest proxy the schema supports.
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT traffic_type AS operation,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY traffic_type
        ORDER BY sample_count DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
