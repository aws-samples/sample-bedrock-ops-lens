"""Latency tab endpoints. Reads f_latency_daily (pre-computed percentiles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/latency-by-model")
async def latency_by_model(f: FilterSet = Depends(parse_filters)):
    """Per-model latency across the selected scope.

    STATISTICS CONTRACT (audit finding 09). Percentiles are NOT mergeable: a
    sample-weighted mean of bucket p50s is not the population p50. For 9,900
    requests at 1 ms and 100 at 10,000 ms the true median is 1 ms, while the
    weighted bucket medians give 100.99 ms. We therefore return, per model:

      avg_e2e            true population mean (means ARE weight-mergeable)
      p50/p90/p99_e2e    the WORST bucket's percentile — an upper bound on the
                         population percentile, labelled as such via
                         `percentile_basis`, never presented as exact
      *_range            min/max across buckets, so the spread is visible

    TTFT uses its OWN sample count: TimeToFirstToken is published only for
    streaming operations, so weighting it by the E2E count understated it
    (900 non-streaming + 100 streaming @200 ms reported 20 ms). Rows predating
    migration 010 have a NULL ttft_sample_count and are excluded from the TTFT
    population rather than treated as zero.

    Account scope is honoured (has_account=True). It previously was not, so every
    account returned identical distributions.
    """
    w = build_where(f, has_account=True)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(sample_count)::BIGINT AS sample_count,
          -- Means are weight-mergeable, so this one IS the population mean.
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          -- Percentiles are not mergeable: report the worst bucket (upper
          -- bound) plus the observed range instead of a fake population value.
          MAX(p50_e2e) AS p50_e2e_worst_bucket,
          MAX(p90_e2e) AS p90_e2e_worst_bucket,
          MAX(p99_e2e) AS p99_e2e_worst_bucket,
          MIN(p50_e2e) AS p50_e2e_best_bucket,
          MIN(p90_e2e) AS p90_e2e_best_bucket,
          MIN(p99_e2e) AS p99_e2e_best_bucket,
          -- TTFT weighted/divided by its OWN population, and only over rows
          -- that actually have a TTFT sample count.
          SUM(ttft_sample_count)::BIGINT AS ttft_sample_count,
          SUM(avg_ttft * ttft_sample_count) FILTER (WHERE ttft_sample_count > 0)
            / NULLIF(SUM(ttft_sample_count) FILTER (WHERE ttft_sample_count > 0), 0)
            AS avg_ttft,
          MAX(p50_ttft) FILTER (WHERE ttft_sample_count > 0) AS p50_ttft_worst_bucket,
          MAX(p90_ttft) FILTER (WHERE ttft_sample_count > 0) AS p90_ttft_worst_bucket,
          MAX(p99_ttft) FILTER (WHERE ttft_sample_count > 0) AS p99_ttft_worst_bucket,
          COUNT(*)::BIGINT AS bucket_count
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY modelId
        ORDER BY sample_count DESC
        """,
        *w.params,
    )
    out = db.rows_to_dicts(rows)
    for row in out:
        # Percentile fields keep their historical names (clients read them) but
        # now carry the worst-bucket value with the basis declared alongside.
        row["percentile_basis"] = "worst_bucket_upper_bound"
        for stat in ("p50", "p90", "p99"):
            row[f"{stat}_e2e"] = row.get(f"{stat}_e2e_worst_bucket")
            row[f"{stat}_ttft"] = row.get(f"{stat}_ttft_worst_bucket")
            lo = row.get(f"{stat}_e2e_best_bucket")
            hi = row.get(f"{stat}_e2e_worst_bucket")
            row[f"{stat}_e2e_range"] = (
                {"min": lo, "max": hi} if lo is not None and hi is not None else None)
        # TTFT availability is explicit: no streaming samples != 0 ms.
        row["ttft_available"] = bool(row.get("ttft_sample_count"))

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
        #
        # HONEST LIMIT: (avg_e2e - avg_ttft) subtracts a STREAMING-only mean from
        # an all-operations mean, so the generation window is only a valid
        # estimate when the traffic is predominantly streaming. It is emitted
        # with `otps_basis` so consumers know it is an approximation over mixed
        # populations, and suppressed entirely when there is no TTFT population
        # to subtract (previously TTFT NULL was treated as 0 ms, which silently
        # turned OTPS into "tokens per whole request").
        row["avg_output_tokens_per_req"] = round(avg_out_per_req, 1) if avg_out_per_req is not None else None
        if avg_out_per_req and gen_ms and gen_ms > 0 and row.get("ttft_available"):
            row["otps"] = round(avg_out_per_req / (gen_ms / 1000.0), 1)
            row["otps_basis"] = "approx_mixed_population"
        else:
            row["otps"] = None
            row["otps_basis"] = None
    return out


@router.get("/latency-cris-vs-od")
async def latency_cris_vs_od(f: FilterSet = Depends(parse_filters)):
    """Same per-model latency split by traffic_type — quantifies CRIS overhead.

    Account scope now applies (f_latency_daily gained accountId in migration
    010); percentile columns are worst-bucket upper bounds, see
    /latency-by-model's contract."""
    w = build_where(f, has_account=True)
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
    """Latency grouped by TRAFFIC TYPE, not by API operation.

    Audit finding 16: this endpoint selected `traffic_type AS operation`, so the
    values were ON_DEMAND_INFERENCE_REQUEST / CROSS_REGION_OD_INFERENCE_REQUEST /
    PROVISIONED_THROUGHPUT_V1 — never Converse / ConverseStream / InvokeModel.
    CloudWatch's AWS/Bedrock namespace exposes only ModelId (and ContextWindow)
    as dimensions, so a real per-operation latency split is NOT available from
    this source; invocation logs would be required. The response now names the
    dimension truthfully (`traffic_type`, with `operation` kept as a deprecated
    alias) and declares `dimension` so the UI can label it correctly."""
    w = build_where(f, has_account=True)
    rows = await db.fetch(
        f"""
        SELECT traffic_type,
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
    out = db.rows_to_dicts(rows)
    for r in out:
        # `operation` kept as a deprecated alias so existing clients don't break,
        # but the value is a traffic type and `dimension` says so.
        r["operation"] = r.get("traffic_type")
        r["dimension"] = "traffic_type"
        r["operation_available"] = False
    return out


@router.get("/latency-impacted-accounts")
async def latency_impacted_accounts(
    f: FilterSet = Depends(parse_filters),
    model_id: str = Query(..., min_length=1),
):
    """Per-account latency drill for one model over the filter window
    (Latency tab: click a model bar → which accounts experienced what).
    Source: f_hourly_status latency_sum/count (invocation logs) — the only
    account-grain latency in the store. avg only (percentiles aren't
    additive across the aggregate)."""
    parts = ["event_date BETWEEN $1::date AND $2::date", "modelId = $3"]
    params: list = [f.start, f.end, model_id]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    if f.endpoint != "all":
        parts.append(f"endpoint = ${len(params)+1}")
        params.append(f.endpoint)
    rows = await db.fetch(
        f"""
        SELECT accountId, region,
          SUM(total_requests)::BIGINT AS total_requests,
          CASE WHEN SUM(latency_count) > 0
               THEN SUM(latency_sum_ms) / SUM(latency_count) END AS avg_latency_ms,
          SUM(latency_count)::BIGINT AS latency_samples,
          SUM(COALESCE(status_429_count,0))::BIGINT AS throttled,
          SUM(COALESCE(status_500_count,0) + COALESCE(status_503_count,0))::BIGINT AS errors_5xx
        FROM f_hourly_status
        WHERE {' AND '.join(parts)}
        GROUP BY accountId, region
        HAVING SUM(latency_count) > 0
        ORDER BY avg_latency_ms DESC NULLS LAST
        LIMIT 500
        """,
        *params,
    )
    return {"model_id": model_id, "rows": db.rows_to_dicts(rows)}
