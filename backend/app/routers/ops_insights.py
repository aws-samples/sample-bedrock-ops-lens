"""Ops Insights tab — CRIS adoption, throttle hotspots, peak RPM, caching, etc."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db, rate_catalog
from ..filters import FilterSet, build_where, parse_filters
from ..quota_match import family_hint_from_model_id, resolve_quota
from ..units import (PER_MINUTE_BASIS, hourly_total_to_per_minute,
                     utilization_pct)

router = APIRouter()


# ---------------------------------------------------------------------------
# CRIS adoption
# ---------------------------------------------------------------------------
@router.get("/ops-cris-adoption")
async def ops_cris_adoption(f: FilterSet = Depends(parse_filters)):
    """Per-model CRIS vs on-demand split, taken over CLASSIFIED requests only.

    Finding 18: the old query divided by SUM(total_requests) while counting only
    two traffic-type families in the numerator, so every other family -
    PROVISIONED_THROUGHPUT_V1, batch, or an unreported/NULL traffic_type - was
    invisible. It both deflated cris_pct and, because the gaps table lists only
    rows with od_requests > 0, made a fleet whose routing was never reported
    render as "All Claude workloads use CRIS". Unknown routing is not CRIS
    adoption. The unclassified remainder is now returned explicitly and the
    percentage is taken over what can actually be classified (NULL when nothing
    can be).
    """
    w = build_where(f)
    rows = await db.fetch(
        f"""
        WITH per_model AS (
          SELECT modelId,
            SUM(CASE WHEN traffic_type IN
                  ('CROSS_REGION_OD_INFERENCE_REQUEST',
                   'SOURCE_REGION_OD_INFERENCE_REQUEST')
                THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
            SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
                THEN total_requests ELSE 0 END)::BIGINT AS od_requests,
            SUM(total_requests)::BIGINT                 AS total_requests
          FROM f_daily
          WHERE {w.sql}
          GROUP BY modelId
          HAVING SUM(total_requests) > 0
        )
        SELECT modelId, cris_requests, od_requests, total_requests,
               (cris_requests + od_requests)::BIGINT AS classified_requests,
               (total_requests - cris_requests - od_requests)::BIGINT
                 AS unclassified_requests,
               ROUND(100.0 * cris_requests
                     / NULLIF(cris_requests + od_requests, 0), 2) AS cris_pct,
               ROUND(100.0 * (cris_requests + od_requests)
                     / NULLIF(total_requests, 0), 2) AS routing_known_pct
        FROM per_model
        ORDER BY classified_requests DESC, total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/ops-cris-by-account")
async def ops_cris_by_account(f: FilterSet = Depends(parse_filters)):
    """Per-account CRIS vs OD breakdown - find accounts not using CRIS.

    Finding 18: also returns the unclassified remainder, so the UI can report
    "routing not reported" instead of implying full CRIS adoption.
    """
    w = build_where(f)
    rows = await db.fetch(
        f"""
        WITH per_acct AS (
          SELECT accountId, modelId,
            SUM(CASE WHEN traffic_type IN
                  ('CROSS_REGION_OD_INFERENCE_REQUEST',
                   'SOURCE_REGION_OD_INFERENCE_REQUEST')
                THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
            SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
                THEN total_requests ELSE 0 END)::BIGINT AS od_requests,
            SUM(total_requests)::BIGINT                 AS total_requests
          FROM f_daily
          WHERE {w.sql}
          GROUP BY accountId, modelId
          HAVING SUM(total_requests) > 0
        )
        SELECT accountId, modelId, cris_requests, od_requests, total_requests,
               (total_requests - cris_requests - od_requests)::BIGINT
                 AS unclassified_requests
        FROM per_acct
        ORDER BY od_requests DESC, total_requests DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Throttle rate hotspots
# ---------------------------------------------------------------------------
@router.get("/ops-throttle-rate")
async def ops_throttle_rate(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(status_429_count)::BIGINT AS throttled,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(status_429_count) > 0 AND SUM(total_requests) > 100
        ORDER BY throttle_pct DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Peak RPM / TPM (max-over-hour from f_hourly_peak)
# ---------------------------------------------------------------------------
@router.get("/ops-peak-rpm")
async def ops_peak_rpm(f: FilterSet = Depends(parse_filters)):
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    if f.provider != "all":
        from ..filters import PROVIDER_PREFIX
        parts.append(f"modelId LIKE ${len(params)+1}")
        params.append(PROVIDER_PREFIX[f.provider] + "%")
    # Endpoint slice: f_hourly_peak carries the runtime/mantle endpoint column,
    # so honor the tab's switcher here (else the Mantle sub-tab would show the
    # combined runtime+mantle peak — identical to runtime).
    if f.endpoint != "all":
        parts.append(f"endpoint = ${len(params)+1}")
        params.append(f.endpoint)
    w = " AND ".join(parts)

    # Fetch per-hour rows and reduce in Python so the output-token burndown
    # multiplier can be applied to each hour BEFORE the peak is taken (the rate
    # is per-model, and the busiest quota-hour can differ from the busiest
    # raw-token hour — so we cannot pre-sum then multiply). See app/burndown.py
    # and the AWS quota-token-burndown doc.
    # Per the AWS token-burndown doc, the quota-consuming input is
    #   InputTokenCount + CacheWriteInputTokens
    # (CacheReadInputTokens do NOT count). In this repo total_input_tokens is
    # exactly InputTokenCount (cache excluded), and cache-write is its own
    # column. So the quota-input is input + cache_write (COALESCE NULLs to 0 for
    # endpoints/rows that don't populate cache columns, e.g. Mantle).
    # peak_quota_tpm prefers the NATIVE AWS metric EstimatedTPMQuotaUsage
    # (estimated_tpm_quota_usage) — AWS computes it including cache-write + the
    # output burndown multiplier, so no reconstruction is needed. When it's
    # absent (rows ingested before the column; the mantle endpoint, which has
    # no such metric), we fall back to the doc formula:
    #   InputTokenCount + CacheWriteInputTokens + OutputTokenCount*rate
    # (CacheRead excluded). total_input_tokens = InputTokenCount; add cache-write.
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region, event_date,
          total_requests,
          (total_input_tokens + COALESCE(total_cache_write_input_tokens, 0)) AS input_quota_tokens,
          total_output_tokens,
          estimated_tpm_quota_usage
        FROM f_hourly_peak
        WHERE {w}
        """,
        *params,
    )

    is_mantle = (f.endpoint == "mantle")
    # One snapshot per request: a fleet with 500 models must not issue 500
    # `ingestion_meta` reads to price its multipliers.
    _cat = await rate_catalog.snapshot()
    agg: dict = {}
    for r in db.rows_to_dicts(rows):
        mid = r.get("modelid") or r.get("modelId")
        key = (r.get("accountid") or r.get("accountId"), mid, r["region"])
        a = agg.get(key)
        if a is None:
            # Burndown applies only to bedrock-runtime; Mantle has separate
            # input/output quotas (rate forced to 1 by is_mantle).
            res = _cat.rate_for(mid, on_date=r.get("event_date"),
                                is_mantle=is_mantle)
            rate = res.rate
            a = agg[key] = {
                "burndown_rate_source": res.source,
                "burndown_rate_verified": res.verified,
                "accountId": key[0], "modelId": mid, "region": key[2],
                "burndown_rate": rate,
                "peak_requests_hour": 0,
                "peak_input_tpm": 0,    # InputTokenCount + CacheWriteInputTokens
                "peak_output_tpm": 0,   # raw output, 1:1
                "peak_quota_tpm": 0,    # native metric, else doc formula
                # Set from the set of sources actually used across all hours, at
                # the end. Labelling the whole series "native" because ONE hour
                # had a datapoint told the reader that AWS measured a number the
                # app had mostly reconstructed itself.
                "quota_tpm_source": "unavailable",
                "_sources": set(),
            }
        rate = a["burndown_rate"]
        req = int(r["total_requests"] or 0)
        out = int(r["total_output_tokens"] or 0)
        inp = int(r["input_quota_tokens"] or 0)
        native = r.get("estimated_tpm_quota_usage")
        a["peak_requests_hour"] = max(a["peak_requests_hour"], req)
        a["peak_output_tpm"] = max(a["peak_output_tpm"], out)
        a["peak_input_tpm"] = max(a["peak_input_tpm"], inp)
        # An observed zero is a real observation. `native > 0` treated it as
        # missing, which silently preferred the reconstruction for any genuinely
        # idle hour. Absent (NULL) is the only "no datapoint".
        if native is not None:
            # Authoritative: AWS already applied cache-write + burndown, so the
            # rate must NOT be applied again.
            a["peak_quota_tpm"] = max(a["peak_quota_tpm"], int(native))
            a["_sources"].add("aws_estimate")
        else:
            # Fallback reconstruction (per-hour, weighted before the peak).
            a["peak_quota_tpm"] = max(a["peak_quota_tpm"], inp + out * rate)
            a["_sources"].add("reconstructed")

    # Disclose the true composition of each series: native, formula, or mixed.
    for a in agg.values():
        s = a.pop("_sources")
        a["quota_tpm_source"] = ("mixed" if len(s) > 1
                                 else (next(iter(s)) if s else "unavailable"))

    # Convert the busiest hour's SUMs into hourly-average per-minute rates.
    # f_hourly_peak holds Period=3600 Sum totals; returning them as "tpm"/"rpm"
    # overstated every rate by 60x. These are lower bounds on the true minute
    # peak (no minute-resolution source exists here) — `rate_basis` says so and
    # the field names avoid the word "peak" for the per-minute values.
    out_rows = []
    for a in agg.values():
        if a["peak_requests_hour"] <= 0:
            continue
        a["busiest_hour_avg_rpm"] = round(
            hourly_total_to_per_minute(a["peak_requests_hour"]), 2)
        a["busiest_hour_avg_input_tpm"] = round(
            hourly_total_to_per_minute(a["peak_input_tpm"]), 2)
        a["busiest_hour_avg_output_tpm"] = round(
            hourly_total_to_per_minute(a["peak_output_tpm"]), 2)
        a["busiest_hour_avg_quota_tpm"] = round(
            hourly_total_to_per_minute(a["peak_quota_tpm"]), 2)
        a["rate_basis"] = PER_MINUTE_BASIS
        # Hourly totals kept under explicit *_hour names for anyone who wants
        # the raw stored grain.
        a["requests_busiest_hour_total"] = a["peak_requests_hour"]
        a["quota_tokens_busiest_hour_total"] = a["peak_quota_tpm"]
        # Back-compat keys now carry the CORRECTED per-minute values.
        a["peak_rpm"] = a["busiest_hour_avg_rpm"]
        a["peak_input_tpm"] = a["busiest_hour_avg_input_tpm"]
        a["peak_output_tpm"] = a["busiest_hour_avg_output_tpm"]
        a["peak_quota_tpm"] = a["busiest_hour_avg_quota_tpm"]
        out_rows.append(a)
    out_rows.sort(key=lambda a: a["requests_busiest_hour_total"], reverse=True)
    return out_rows[:200]


# ---------------------------------------------------------------------------
# Burndown risk — Claude 4+ family using > some % of applied quota.
# ---------------------------------------------------------------------------
@router.get("/ops-burndown-risk")
async def ops_burndown_risk(f: FilterSet = Depends(parse_filters)):
    """Joins f_hourly_peak with f_quotas (applied TPM) to surface Claude
    deployments approaching their TPM limit, with the per-model output-token
    burndown multiplier applied (15x Opus 4.8 / 5x other Claude 3.7+ / 1x else)
    so "Peak TPM (quota)" matches how CloudWatch burns down the quota.

    The multiplier is applied to output per-hour BEFORE the peak is taken
    (see app/burndown.py), then joined to the applied quota. The Claude-family
    filter matches both bare ids (`anthropic.claude-...`) and CRIS-prefixed ids
    (`us.`/`eu.`/`apac.`/`global.` ... + `anthropic.claude-...`) — a bare-prefix
    filter silently dropped all cross-region traffic, which is most of it."""
    parts = ["h.event_date BETWEEN $1::date AND $2::date",
             "h.modelId LIKE '%anthropic.claude-%'"]
    params: list = [f.start, f.end]
    # Honor the global provider filter. This widget is Claude-only by design
    # (the burndown multiplier is a Claude quota concept), so any non-anthropic
    # provider yields an EMPTY result — without this, switching the provider
    # filter left the table stuck on the full Claude list (TAM bug report:
    # "one metric does not update; Throttle hotspots above it does").
    if f.provider not in ("all", "anthropic"):
        return []
    if f.accounts:
        parts.append(f"h.accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"h.region = ${len(params)+1}")
        params.append(f.region)
    if f.endpoint != "all":
        parts.append(f"h.endpoint = ${len(params)+1}")
        params.append(f.endpoint)
    w = " AND ".join(parts)

    # Per-hour rows; reduce in Python so the per-model rate is applied to each
    # hour's output before the peak (the rate is per-model so it can't live in
    # the SQL aggregate, and the busiest quota-hour differs from the busiest
    # raw-token hour).
    # Quota-input per the AWS doc: InputTokenCount + CacheWriteInputTokens
    # (cache-read excluded). total_input_tokens is InputTokenCount; add cache
    # write. This block is Claude-only (see LIKE filter) on bedrock-runtime.
    hourly = await db.fetch(
        f"""
        SELECT h.accountId, h.modelId, h.region, h.event_date,
          (h.total_input_tokens + COALESCE(h.total_cache_write_input_tokens, 0)) AS input_quota_tokens,
          h.total_output_tokens,
          -- Prefer AWS's own estimate for the hour when it published one.
          h.estimated_tpm_quota_usage
        FROM f_hourly_peak h
        WHERE {w}
        """,
        *params,
    )

    is_mantle = (f.endpoint == "mantle")
    # One catalog snapshot for the whole request, not a lookup per model.
    cat = await rate_catalog.snapshot()
    peaks: dict = {}
    for r in db.rows_to_dicts(hourly):
        mid = r.get("modelid") or r.get("modelId")
        key = (r.get("accountid") or r.get("accountId"), mid, r["region"])
        inp = int(r["input_quota_tokens"] or 0)
        p = peaks.get(key)
        if p is None:
            res = cat.rate_for(mid, on_date=r.get("event_date"),
                               is_mantle=is_mantle)
            p = peaks[key] = {
                "accountId": key[0], "modelId": mid, "region": key[2],
                "burndown_rate": res.rate,
                "burndown_rate_source": res.source,
                "burndown_rate_verified": res.verified,
                "peak_output_tpm": 0, "peak_quota_tpm": 0,
                "_sources": set(),
            }
        out = int(r["total_output_tokens"] or 0)
        native = r.get("estimated_tpm_quota_usage")
        p["peak_output_tpm"] = max(p["peak_output_tpm"], out)
        # Source selection per hour, then the peak — never max(native, formula),
        # which would let a stale multiplier override a live AWS observation.
        if native is not None:
            p["peak_quota_tpm"] = max(p["peak_quota_tpm"], int(native))
            p["_sources"].add("aws_estimate")
        else:
            p["peak_quota_tpm"] = max(
                p["peak_quota_tpm"], inp + out * p["burndown_rate"])
            p["_sources"].add("reconstructed")

    for p in peaks.values():
        s = p.pop("_sources")
        p["quota_tpm_source"] = ("mixed" if len(s) > 1
                                 else (next(iter(s)) if s else "unavailable"))

    if not peaks:
        return []

    # TPM quotas for the in-scope accounts/regions. Resolution goes through the
    # ONE shared resolver (app/quota_match.py) so this widget and the quota
    # drill-down can no longer disagree, and so an ambiguous traffic family is
    # reported as ambiguous instead of silently resolving to the most generous
    # limit. `default_value` is included: an un-raised quota is still a real
    # limit, and dropping those rows made models look quota-less.
    qparts = ["metric = 'TPM'",
              "COALESCE(applied_value, default_value) IS NOT NULL"]
    qparams: list = []
    accts = sorted({k[0] for k in peaks})
    qparts.append(f"accountId = ANY(${len(qparams)+1}::text[])")
    qparams.append(accts)
    if f.region != "all":
        qparts.append(f"region = ${len(qparams)+1}")
        qparams.append(f.region)
    quota_rows = db.rows_to_dicts(await db.fetch(
        f"""SELECT accountId, region, model_name, metric, traffic_type,
                   quota_code, applied_value, default_value
            FROM f_quotas WHERE {' AND '.join(qparts)}""",
        *qparams,
    ))

    out_rows = []
    for p in peaks.values():
        # A CRIS-prefixed model id tells us the routing family exactly, so the
        # lookup is only ambiguous for bare ids that have several families.
        q = resolve_quota(quota_rows, p["accountId"], p["region"], p["modelId"],
                          metric="TPM",
                          family_hint=family_hint_from_model_id(p["modelId"]))
        # Hourly SUMs -> hourly-average per-minute rate. f_hourly_peak stores
        # Period=3600 Sum, so comparing it to a per-minute limit directly was
        # 60x too high. This is a lower bound on the true minute peak; the field
        # names and `rate_basis` say so.
        quota_tpm_avg = hourly_total_to_per_minute(p["peak_quota_tpm"])
        output_tpm_avg = hourly_total_to_per_minute(p["peak_output_tpm"])
        util = utilization_pct(quota_tpm_avg, q.value)
        row = {
            "accountId": p["accountId"],
            "modelId": p["modelId"],
            "region": p["region"],
            "burndown_rate": p["burndown_rate"],
            # Busiest hour, expressed as an hourly-average per-minute rate.
            "busiest_hour_avg_tpm": round(quota_tpm_avg, 2),
            "busiest_hour_avg_output_tpm": round(output_tpm_avg, 2),
            "rate_basis": PER_MINUTE_BASIS,
            # Back-compat aliases (same corrected values) for older clients.
            "peak_tpm": round(quota_tpm_avg, 2),
            "peak_output_tpm": round(output_tpm_avg, 2),
            "effective_tpm": q.value,
            "overhead_pct": round(util, 2) if util is not None else None,
            "utilization_pct": round(util, 2) if util is not None else None,
        }
        row.update(q.as_dict())
        out_rows.append(row)
    # Unknown-quota rows sort last but are RETAINED: dropping them hid real
    # traffic whose limit we simply could not resolve.
    out_rows.sort(key=lambda r: (r["utilization_pct"] is None,
                                 -(r["utilization_pct"] or 0)))
    return out_rows[:200]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
@router.get("/ops-caching")
async def ops_caching(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_input_tokens)::BIGINT              AS total_input_tokens,
          SUM(total_cache_read_input_tokens)::BIGINT   AS cache_read_tokens,
          SUM(total_cache_write_input_tokens)::BIGINT  AS cache_write_tokens,
          -- Share of PROMPT tokens served from cache.
          --
          -- Finding 14: the denominator was (cache_read + input) and so omitted
          -- cache WRITES, overstating the share on any workload that is actively
          -- populating a cache - the very workload the panel is meant to
          -- encourage. The Bedrock Runtime TokenUsage structure carries
          -- inputTokens, cacheReadInputTokens and cacheWriteInputTokens as three
          -- disjoint counters, so the prompt-side total is all three added.
          --
          -- It is also a TOKEN share, not a request hit rate: it says nothing
          -- about what fraction of requests hit the cache (CloudWatch publishes
          -- no per-request cache dimension). The field name and `basis` say so.
          ROUND(100.0 * COALESCE(SUM(total_cache_read_input_tokens), 0)
              / NULLIF(COALESCE(SUM(total_cache_read_input_tokens), 0)
                       + COALESCE(SUM(total_cache_write_input_tokens), 0)
                       + COALESCE(SUM(total_input_tokens), 0), 0), 2)
            AS cached_prompt_token_pct,
          (COALESCE(SUM(total_cache_read_input_tokens), 0)
           + COALESCE(SUM(total_cache_write_input_tokens), 0)
           + COALESCE(SUM(total_input_tokens), 0))::BIGINT AS prompt_tokens_total
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_input_tokens) > 0
        ORDER BY total_input_tokens DESC
        """,
        *w.params,
    )
    out = db.rows_to_dicts(rows)
    for r in out:
        # `hit_rate_pct` kept as an alias so an older cached client does not
        # break, but it now carries the corrected value and the basis is stated.
        r["hit_rate_pct"] = r["cached_prompt_token_pct"]
        r["basis"] = "cached_share_of_prompt_tokens"
        r["request_hit_rate_available"] = False
    return out


# ---------------------------------------------------------------------------
# Context length routing
# ---------------------------------------------------------------------------
@router.get("/ops-context-length")
async def ops_context_length(f: FilterSet = Depends(parse_filters)):
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    w = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT routed_model_id, modelId,
          SUM(total_requests)::BIGINT     AS total_requests,
          SUM(total_input_tokens)::BIGINT AS input_tokens
        FROM f_context_length
        WHERE {w}
        GROUP BY routed_model_id, modelId
        ORDER BY total_requests DESC
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Request shape (avg input/output ratio)
# ---------------------------------------------------------------------------
@router.get("/ops-request-shape")
async def ops_request_shape(f: FilterSet = Depends(parse_filters)):
    """Average prompt/completion shape per account+model+region.

    Finding 12: `ratio` was computed as output/input but presented under an
    "In:Out ratio" header and thresholded as if it were input/output. Traffic
    averaging 9,253 input and 124 output tokens - 74.7:1 input-heavy, the
    textbook prompt-caching candidate - rendered as "0.0:1" and tripped the
    `< 2` "output-heavy, burndown amplifier" warning, which is the opposite
    advice. Ops Review computed the same measure the other way up, so the two
    panels contradicted each other on identical data.

    Both ratios are now returned under unambiguous names, `ratio` matches the
    label (input:output), and `ratio_basis` states it. Rows with zero output
    tokens return null rather than a bogus infinity.
    """
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          ROUND(SUM(total_input_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_input,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_output,
          ROUND(SUM(total_input_tokens)::NUMERIC
                / NULLIF(SUM(total_output_tokens), 0), 1) AS ratio,
          ROUND(SUM(total_input_tokens)::NUMERIC
                / NULLIF(SUM(total_output_tokens), 0), 1) AS input_output_ratio,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_input_tokens), 0), 4) AS output_input_ratio,
          SUM(total_requests)::BIGINT AS total_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) > 100
        ORDER BY SUM(total_requests) DESC
        LIMIT 200
        """,
        *w.params,
    )
    out = db.rows_to_dicts(rows)
    for r in out:
        r["ratio_basis"] = "input_tokens_per_output_token"
    return out


# ---------------------------------------------------------------------------
# Service tier
# ---------------------------------------------------------------------------
@router.get("/ops-service-tier")
async def ops_service_tier(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT service_tier,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY service_tier
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Inference profile prefix adoption
# ---------------------------------------------------------------------------
@router.get("/ops-inference-profile")
async def ops_inference_profile(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT inference_profile_prefix,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        GROUP BY inference_profile_prefix
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
