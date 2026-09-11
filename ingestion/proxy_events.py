#!/usr/bin/env python3
"""
Proxy per-workload event ingester for Bedrock Ops Lens (Task A).

A GenAI proxy that fronts Bedrock signs every request with ONE IAM role, so
caller identity can't attribute usage to a workload. Instead the proxy emits
ONE metadata-only event per request to an S3 bucket in the customer's account,
which we read cross-account (same trust pattern as Bedrock invocation logs — no
public inbound endpoint). Works across BOTH bedrock-runtime and bedrock-mantle
because the proxy reads token counts from whichever response body it gets.

S3 layout the proxy writes (NDJSON, one JSON object per line; .jsonl or
.jsonl.gz):
    s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl[.gz]

Each line (metadata only — NEVER prompt/response text):
    {
      "ts": "2026-07-03T18:03:22Z",   # ISO-8601 UTC
      "dimensions": {                  # ARBITRARY custom attribution map
        "workload": "search-service",
        "env": "prod",
        "business_unit": "retail"
      },
      "model": "anthropic.claude-opus-4-8",
      "endpoint": "runtime" | "mantle",
      "region": "us-east-1",
      "input_tokens": 812,
      "output_tokens": 143,
      "cache_read_tokens": 0,          # optional
      "status": 200,                   # HTTP status the proxy saw
      "throttled": false,              # true if a 429/throttle
      "retry_attempts": 0,             # optional: number of RETRIES (0 = none).
                                       # `attempt` (1-based ordinal) also
                                       # accepted and converted to a count.

      "latency_ms": 940,               # optional; proxy wall-clock
      "request_id": "msg_bdrk_..."     # for idempotency
    }

    Back-compat: a top-level "workload": "x" is accepted and folded into
    dimensions as {"workload": "x"}. A request with no dimensions is bucketed
    under {"workload": "__unattributed__"} so its tokens still count.

Counting rules (audit T04/T05):
  - Throttles, non-throttle errors and successes are DISJOINT: a 429 counts once,
    as a throttle. Consumers derive successes as
    (total_requests - throttled_count - error_count), which only holds if the
    three populations do not overlap.
  - Each request contributes to the rollup at most once, even if the same event
    is delivered more than once: the raw insert arbitrates, and the rollup is
    built only from rows it actually inserted.

Writes:
  - f_request_events       raw per-request rows w/ full JSONB dimensions map
  - f_proxy_dim_hourly     hourly rollup, one row per (dim_key, dim_value, …)
  - dim_proxy_dimensions   distinct (dim_key, dim_value) pairs for the picker

Resumability: a `(s3_key)` entry in `proxy_events_objects` records files already
processed, so re-runs skip them.

Usage:
    python -m ingestion.proxy_events \\
        --bucket my-genai-proxy-events \\
        --regions us-east-1,us-west-2 --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import asyncpg
import boto3

from . import otel_normalize
from botocore.config import Config

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# Cap how many raw request rows we retain per hour bucket in memory, so a
# pathological volume day can't blow the Lambda's memory. The hourly rollup is
# always complete; only the raw f_request_events sample is bounded.
RAW_RETENTION_DAYS = 14


def _s3_client(region: str):
    return boto3.client(
        "s3", region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_event_keys(s3, bucket: str, region: str,
                     start_dt: datetime, end_dt: datetime) -> list[str]:
    """Every proxy-event object key in the date range for one region."""
    keys: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        prefix = f"proxy-events/{region}/{cur:%Y/%m/%d}/"
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                k = obj["Key"]
                if obj["Size"] == 0:
                    continue
                if not (k.endswith(".jsonl") or k.endswith(".jsonl.gz")
                        or k.endswith(".json") or k.endswith(".json.gz")):
                    continue
                keys.append(k)
        cur += timedelta(days=1)
    return keys


async def _already_processed(conn: asyncpg.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = await conn.fetch(
        "SELECT s3_key FROM proxy_events_objects WHERE s3_key = ANY($1::text[])",
        keys,
    )
    return {r["s3_key"] for r in rows}


# Per-object decode-error counts, keyed by S3 key. A partially decoded object must
# NOT be recorded as processed, or a normal re-run skips it forever and the valid
# records inside it are lost for good (round 3, R3-04).
_OBJ_ERRORS: dict[str, int] = {}


def _read_event_lines(s3, bucket: str, key: str):
    """Yield parsed JSON objects from a proxy-events object (NDJSON, maybe gz)."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if key.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return
    text = body.decode("utf-8", "replace")
    # NDJSON is the common case, but the OTEL collector's otlp_json marshaler
    # writes CONCATENATED JSON documents with no separator at all (one object per
    # export, no trailing newline). Splitting on newlines alone found a single
    # unparseable blob and dropped the whole file, so decode incrementally.
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    errors = 0
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            # Round 3 (R3-04): recovery used to look only for a newline, and
            # collector `otlp_json` output contains none - so a corrupt document
            # in the middle silently discarded every VALID document after it,
            # while the caller still marked the object fully processed. Resync on
            # the next plausible document start as well as the next newline, and
            # count the failure so the caller can refuse to mark it done.
            errors += 1
            nl = text.find("\n", i)
            brace = text.find("{", i + 1)
            cands = [c for c in (nl + 1 if nl != -1 else -1, brace) if c > i]
            if not cands:
                break
            i = min(cands)
            continue
        i = end
        # One document can carry many events (an OTLP envelope holds several
        # logRecords, each with its own resource attributes).
        for ev in otel_normalize.expand(obj):
            yield ev
    if errors:
        # Surfaced via a mutable attribute rather than a return value because this
        # is a generator the caller iterates; see _OBJ_ERRORS.
        _OBJ_ERRORS[key] = _OBJ_ERRORS.get(key, 0) + errors


# Bucket for requests the proxy sent with no dimensions at all — so their
# tokens still count toward totals instead of vanishing.
_UNATTRIBUTED = "__unattributed__"
# Cap dimension keys per event so a misconfigured proxy can't explode the
# fan-out (mirrors the spirit of Bedrock's 10-tag requestMetadata limit).
_MAX_DIMS = 10


def _extract_dimensions(e: dict) -> dict:
    """Return a cleaned {key: value} string map from an event.

    Accepts either a `dimensions` object or a top-level `workload` (back-compat),
    or both (merged). Keys/values are trimmed strings; non-string scalars are
    stringified; empty keys/values dropped. Capped at _MAX_DIMS keys."""
    dims: dict[str, str] = {}
    raw = e.get("dimensions")
    if isinstance(raw, dict):
        for k, v in raw.items():
            ks = str(k).strip()
            if v is None:
                continue
            vs = str(v).strip()
            if ks and vs:
                dims[ks] = vs
    # Back-compat: a bare top-level workload becomes a dimension.
    wl = e.get("workload")
    if wl is not None:
        wls = str(wl).strip()
        if wls and "workload" not in dims:
            dims["workload"] = wls
    if not dims:
        dims = {"workload": _UNATTRIBUTED}
    # Deterministic truncation (sorted) if a proxy over-emits.
    if len(dims) > _MAX_DIMS:
        dims = dict(sorted(dims.items())[:_MAX_DIMS])
    return dims


# Recognized endpoint paths. runtime/mantle are Bedrock; the direct-API values
# exist ONLY in client telemetry (nothing AWS-side can see that traffic). A few
# aliases map emitter vocab (gen_ai.provider.name, LiteLLM provider names) onto
# our enum.
#
# 'unknown' is a real value, not a fallback to be avoided: an emitter reporting a
# provider we do not recognise (Vertex, Cohere, Together, a self-hosted model)
# used to be relabelled "runtime", which counted non-AWS traffic as Bedrock
# traffic and inflated every Bedrock panel (audit T02). Reporting it as unknown
# keeps the request visible without claiming AWS served it - and, since it is not
# in AWS_BILLED_ENDPOINTS, it receives no share of the AWS invoice and is not
# scored against AWS quotas.
_ENDPOINTS = ("runtime", "mantle", "anthropic-api", "openai-api", "unknown")
_ENDPOINT_ALIASES = {
    "bedrock": "runtime", "bedrock-runtime": "runtime",
    "bedrock-mantle": "mantle",
    "anthropic": "anthropic-api", "anthropic_api": "anthropic-api",
    "openai": "openai-api", "openai_api": "openai-api",
    "azure-openai": "openai-api", "azure_openai": "openai-api",
}


def _parse_event(e: dict):
    """Normalize one proxy event. Returns a tuple or None if unusable.

    (ts, event_date, hour, dimensions_dict, modelId, endpoint, region, accountId,
     in_tok, out_tok, cache_read, cache_write, status, throttled, latency_ms,
     ttft_ms, retry_attempts, cost_usd_est, request_id)
    """
    # Audit T01: OTLP GenAI records are normalized in code, not in a collector
    # transform (the documented one was comments only, so an OTEL on-ramp shipped
    # raw OTLP - no `ts`, no `model` - and every line was silently dropped).
    # `_read_event_lines` already expands and normalizes, so this is for direct
    # callers; records already in the proxy-event shape pass straight through.
    if otel_normalize.is_envelope(e):
        expanded = otel_normalize.expand(e)
        if not expanded:
            return None
        e = expanded[0]
    elif otel_normalize.looks_like_otel(e):
        e = otel_normalize.normalize(e)
        if not e:
            return None
    ts_raw = e.get("ts") or e.get("timestamp")
    model = (e.get("model") or e.get("modelId") or "").strip()
    if not ts_raw or not model:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None

    dimensions = _extract_dimensions(e)

    endpoint = (e.get("endpoint") or "runtime").strip().lower()
    endpoint = _ENDPOINT_ALIASES.get(endpoint, endpoint)
    if endpoint not in _ENDPOINTS:
        # Unrecognised path: say so rather than asserting it was Bedrock.
        endpoint = "unknown"
    region = (e.get("region") or "").strip() or "unknown"
    account = (e.get("accountId") or e.get("account_id") or "__none__").strip() or "__none__"

    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    in_tok = _int(e.get("input_tokens"))
    out_tok = _int(e.get("output_tokens"))
    cache_read = _int(e.get("cache_read_tokens"))
    # Cache WRITES are a third, disjoint prompt-token counter. They were parsed
    # nowhere and therefore lost, which is the same denominator error finding 14
    # fixed on the CloudWatch side (audit T02).
    cache_write = _int(e.get("cache_write_tokens")
                       or e.get("cache_creation_input_tokens"))
    status = _int(e.get("status")) or 200
    throttled = bool(e.get("throttled")) or status == 429
    def _float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    latency_ms = _float(e.get("latency_ms") or e.get("duration_ms"))
    # Client-telemetry extras (all optional; emitter vocab aliases accepted).
    ttft_ms = _float(e.get("ttft_ms") or e.get("time_to_first_token_ms")
                     or e.get("time_to_first_chunk_ms"))
    # `retry_attempts` and `attempt` are DIFFERENT vocabularies and conflating
    # them (audit T05) both undercounted and miscounted retries:
    #   retry_attempts = how many retries happened      (1 -> one retry)
    #   attempt        = which attempt this record is   (1 -> first try, none)
    # The old code took either value and counted "retried" only when > 1, so a
    # genuine `retry_attempts: 1` was ignored while `attempt: 1` was treated
    # identically to it. Normalise both to a retry COUNT.
    if e.get("retry_attempts") is not None:
        retries = _int(e.get("retry_attempts"))
    elif e.get("attempt") is not None:
        # Attempt ordinals are 1-based; attempt N means N-1 retries.
        retries = max(_int(e.get("attempt")) - 1, 0)
    else:
        retries = None
    retry_attempts = retries
    cost_usd_est = _float(e.get("cost_usd_est") or e.get("cost_usd"))
    # Idempotency key. Fall back to a synthetic one if the proxy omitted it,
    # combining the fields so identical re-reads dedupe but distinct calls don't.
    request_id = (e.get("request_id") or e.get("id") or "").strip()
    if not request_id:
        # Round 3 (R3-03): this fallback is the ONLY identity a record without a
        # provider response id, span id or trace id ever gets, and dedup is
        # (event_date, request_id, ts). Whole-second timestamps made ten distinct
        # same-second failures one event. `ts_raw` now carries microseconds, and
        # the account/region/endpoint go into the signature so two accounts
        # emitting otherwise identical metadata stay distinct. It must remain
        # DERIVED (not random) or redelivery would stop deduplicating.
        dim_sig = ",".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
        request_id = (f"{dim_sig}:{model}:{endpoint}:{region}:{account}"
                      f":{ts_raw}:{in_tok}:{out_tok}:{status}")

    return (dt, dt.date(), dt.hour, dimensions, model, endpoint, region, account,
            in_tok, out_tok, cache_read, cache_write, status, throttled,
            latency_ms,
            ttft_ms, retry_attempts, cost_usd_est, request_id)


def _pct(sorted_vals: list[float], p: float):
    """Nearest-rank percentile (matches invocation_logs latency math)."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(round(p * (n - 1)))))
    return sorted_vals[idx]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--regions", default="us-east-1")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    conn = await asyncpg.connect(args.db_url)
    total_events = 0
    new_keys: list[tuple] = []

    try:
        # Hourly rollup accumulator + latency samples per bucket. We fan each
        # request out to one bucket PER dimension key (workload/env/bu/…), so
        # summing a single dim_key later is correct.
        # key = (event_date, hour, dim_key, dim_value, modelId, endpoint, region, account)
        rollup: dict[tuple, dict] = defaultdict(lambda: {
            "total_requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "throttled_count": 0, "error_count": 0,
            "latencies": [], "ttfts": [], "retried_count": 0, "cost_usd_est": 0.0,
        })
        raw_rows: list[tuple] = []
        # Parsed events held until the raw insert tells us which are new (T04).
        events: list[tuple] = []
        seen_idents: set[tuple] = set()
        dup_in_batch = 0
        bad_objects: list[tuple[str, int]] = []
        dup_persisted = 0
        out_of_retention = 0
        raw_cutoff = (end - timedelta(days=RAW_RETENTION_DAYS)).date()

        for region in regions:
            s3 = _s3_client(region)
            keys = _list_event_keys(s3, args.bucket, region, start, end)
            already = await _already_processed(conn, keys)
            pending = [k for k in keys if k not in already]
            print(f"  [{region}] {len(keys)} proxy-event objects "
                  f"({len(already)} already processed, {len(pending)} new)")

            for key in pending:
                obj_rows = 0
                for e in _read_event_lines(s3, args.bucket, key):
                    parsed = _parse_event(e)
                    if not parsed:
                        continue
                    (ts, ev_date, hr, dimensions, model, endpoint, region_v, account,
                     in_tok, out_tok, cache_read, cache_write, status, throttled,
                     latency_ms, ttft_ms, retry_attempts, cost_usd_est,
                     request_id) = parsed
                    obj_rows += 1

                    # Deduplicate WITHIN this run first. Proxy callbacks and
                    # S3 event delivery are at-least-once, so the same request
                    # can appear twice in one batch (audit T04).
                    ident = (ev_date, request_id, ts)
                    if ident in seen_idents:
                        dup_in_batch += 1
                        continue
                    seen_idents.add(ident)

                    events.append((
                        ident, ev_date, hr, dimensions, model, endpoint, region_v,
                        account, in_tok, out_tok, cache_read, cache_write, status,
                        throttled, latency_ms, ttft_ms, retry_attempts,
                        cost_usd_est, request_id, ts,
                    ))
                    if ev_date >= raw_cutoff:
                        raw_rows.append((
                            ts, ev_date, json.dumps(dimensions), model, endpoint,
                            region_v, account, in_tok, out_tok, cache_read,
                            cache_write, status, throttled, latency_ms, ttft_ms,
                            retry_attempts, cost_usd_est, request_id,
                        ))

                if _OBJ_ERRORS.get(key):
                    # Leave the key un-marked so it stays eligible for replay
                    # after the corruption is repaired. The events we DID decode
                    # are still ingested; they dedupe on re-read.
                    bad_objects.append((key, _OBJ_ERRORS[key]))
                else:
                    new_keys.append((key, obj_rows))
                total_events += obj_rows

        # --- write raw per-request rows (idempotent on request_id, ts) ------
        #
        # Audit T04: the rollup used to be accumulated during parsing, while the
        # raw table deduplicated on (event_date, request_id, ts). A redelivered
        # event was therefore ignored by the raw table but ADDED AGAIN to the
        # additive hourly upsert, so f_proxy_dim_hourly drifted above
        # f_request_events and every attribution figure inherited the drift.
        # (The s3_key marker only prevents reprocessing the same OBJECT; it does
        # nothing about the same event arriving in a different object.)
        #
        # The raw insert is now the arbiter: RETURNING tells us exactly which
        # requests were new, and only those are rolled up. Duplicates therefore
        # cost nothing in either table.
        inserted: set | None = None
        if raw_rows:
            returned = await conn.fetch(
                """
                INSERT INTO f_request_events (
                    ts, event_date, dimensions, modelId, endpoint, region, accountId,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, status, throttled, latency_ms,
                    ttft_ms, retry_attempts, cost_usd_est, request_id
                )
                SELECT * FROM UNNEST(
                    $1::timestamptz[], $2::date[], $3::jsonb[], $4::text[], $5::text[],
                    $6::text[], $7::text[], $8::bigint[], $9::bigint[], $10::bigint[],
                    $11::bigint[], $12::int[], $13::boolean[],
                    $14::double precision[], $15::double precision[], $16::int[],
                    $17::double precision[], $18::text[])
                ON CONFLICT (event_date, request_id, ts) DO NOTHING
                RETURNING event_date, request_id, ts
                """,
                *[list(col) for col in zip(*raw_rows)],
            )
            inserted = {(r["event_date"], r["request_id"], r["ts"]) for r in returned}
            dup_persisted = len(raw_rows) - len(inserted)

        # --- build the hourly rollup from NEW requests only -----------------
        for (ident, ev_date, hr, dimensions, model, endpoint, region_v, account,
             in_tok, out_tok, cache_read, cache_write, status, throttled,
             latency_ms, ttft_ms, retry_attempts, cost_usd_est, request_id,
             ts) in events:
            if inserted is not None and ev_date >= raw_cutoff and ident not in inserted:
                # Already counted by an earlier run.
                continue
            if ev_date < raw_cutoff:
                # No raw row exists to arbitrate against, so cross-run duplicates
                # of events older than RAW_RETENTION_DAYS cannot be detected.
                # Within-batch duplicates were already dropped above.
                out_of_retention += 1
            # Fan out: one rollup bucket per (dim_key, dim_value), PLUS an
            # '__all__' accounting row.
            #
            # Audit T09: client-reported dimensions are sparse - one caller sends
            # `workload`, another only `team`, another nothing at all (folded to
            # workload=__unattributed__). Code that "pins to one dim_key because
            # every key covers 100% of requests" therefore silently loses every
            # request that lacks that key, and loses a whole PROVIDER if none of
            # its traffic carries it. '__all__' carries exactly one row per
            # request, so totals and provider lists read off it are complete. It
            # is excluded from dim_proxy_dimensions, so it never appears in a
            # picker.
            for dim_key, dim_value in list(dimensions.items()) + [("__all__", "__all__")]:
                b = rollup[(ev_date, hr, dim_key, dim_value, model,
                            endpoint, region_v, account)]
                b["total_requests"] += 1
                b["input_tokens"] += in_tok
                b["output_tokens"] += out_tok
                b["cache_read_tokens"] += cache_read
                b["cache_write_tokens"] += cache_write
                # Disjoint populations (audit T05). A 429 used to increment BOTH
                # counters, and every consumer computes successes as
                # (total - throttled - error), so a single throttled request
                # produced successes = -1 and failures = 2. Throttles are counted
                # as throttles and nothing else; error_count is non-throttle
                # 4xx/5xx only. Invariant: throttled + error + success == total.
                if throttled:
                    b["throttled_count"] += 1
                elif status >= 400:
                    b["error_count"] += 1
                if latency_ms is not None and latency_ms >= 0:
                    b["latencies"].append(latency_ms)
                if ttft_ms is not None and ttft_ms >= 0:
                    b["ttfts"].append(ttft_ms)
                # retry_attempts is now a retry COUNT, so any value >= 1 means
                # the request was retried at least once.
                if retry_attempts is not None and retry_attempts >= 1:
                    b["retried_count"] += 1
                if cost_usd_est:
                    b["cost_usd_est"] += cost_usd_est

        # --- write hourly rollup (additive upsert) --------------------------
        if rollup:
            roll_rows = []
            for (ev_date, hr, dim_key, dim_value, model, endpoint, region_v, account), m in rollup.items():
                lat = sorted(m["latencies"])
                ttf = sorted(m["ttfts"])
                roll_rows.append((
                    ev_date, hr, dim_key, dim_value, model, endpoint, region_v, account,
                    m["total_requests"], m["input_tokens"], m["output_tokens"],
                    m["cache_read_tokens"], m["cache_write_tokens"],
                    m["throttled_count"], m["error_count"],
                    _pct(lat, 0.50), _pct(lat, 0.90), _pct(lat, 0.99),
                    _pct(ttf, 0.50), _pct(ttf, 0.90),
                    m["retried_count"], round(m["cost_usd_est"], 6),
                ))
            await conn.executemany(
                """
                INSERT INTO f_proxy_dim_hourly (
                    event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId,
                    total_requests, input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, throttled_count, error_count,
                    p50_latency_ms, p90_latency_ms, p99_latency_ms,
                    p50_ttft_ms, p90_ttft_ms, retried_count, cost_usd_est
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                ON CONFLICT (event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId)
                DO UPDATE SET
                    total_requests   = f_proxy_dim_hourly.total_requests   + EXCLUDED.total_requests,
                    input_tokens     = f_proxy_dim_hourly.input_tokens     + EXCLUDED.input_tokens,
                    output_tokens    = f_proxy_dim_hourly.output_tokens    + EXCLUDED.output_tokens,
                    cache_read_tokens= f_proxy_dim_hourly.cache_read_tokens+ EXCLUDED.cache_read_tokens,
                    cache_write_tokens=f_proxy_dim_hourly.cache_write_tokens+EXCLUDED.cache_write_tokens,
                    throttled_count  = f_proxy_dim_hourly.throttled_count  + EXCLUDED.throttled_count,
                    error_count      = f_proxy_dim_hourly.error_count      + EXCLUDED.error_count,
                    -- percentiles: take the max as a cheap "worst seen" merge
                    -- (exact cross-batch percentiles would need raw samples;
                    -- for the reporting use case worst-of is acceptable and honest).
                    p50_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p50_latency_ms,0), COALESCE(EXCLUDED.p50_latency_ms,0)),
                    p90_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p90_latency_ms,0), COALESCE(EXCLUDED.p90_latency_ms,0)),
                    p99_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p99_latency_ms,0), COALESCE(EXCLUDED.p99_latency_ms,0)),
                    p50_ttft_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p50_ttft_ms,0), COALESCE(EXCLUDED.p50_ttft_ms,0)),
                    p90_ttft_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p90_ttft_ms,0), COALESCE(EXCLUDED.p90_ttft_ms,0)),
                    retried_count = f_proxy_dim_hourly.retried_count + EXCLUDED.retried_count,
                    cost_usd_est  = f_proxy_dim_hourly.cost_usd_est  + EXCLUDED.cost_usd_est
                """,
                roll_rows,
            )

        # --- resumability markers -------------------------------------------
        if new_keys:
            await conn.executemany(
                "INSERT INTO proxy_events_objects (s3_key, row_count) "
                "VALUES ($1, $2) ON CONFLICT (s3_key) DO NOTHING",
                new_keys,
            )

        # --- retention: trim raw events beyond the window ------------------
        await conn.execute(
            "DELETE FROM f_request_events WHERE event_date < current_date - $1::int",
            RAW_RETENTION_DAYS,
        )

        # --- refresh dim_proxy_dimensions dropdown source -------------------
        await conn.execute("DELETE FROM dim_proxy_dimensions")
        await conn.execute("""
            INSERT INTO dim_proxy_dimensions (dim_key, dim_value, first_seen, last_seen, total_requests_30d, endpoints)
            SELECT dim_key, dim_value, MIN(event_date), MAX(event_date), SUM(total_requests),
                   array_agg(DISTINCT endpoint)
            FROM f_proxy_dim_hourly
            WHERE event_date >= current_date - INTERVAL '30 days'
              -- '__all__' is the whole-population accounting row, not an
              -- attribute a user can filter by (audit T09).
              AND dim_key <> '__all__'
            GROUP BY dim_key, dim_value
        """)

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_proxy_events_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )

        n_dims = await conn.fetchval("SELECT COUNT(*) FROM dim_proxy_dimensions")
        n_keys = await conn.fetchval("SELECT COUNT(DISTINCT dim_key) FROM dim_proxy_dimensions")
        print(f"DONE. parsed {total_events} proxy events → "
              f"{len(rollup)} hourly rollup rows, {n_dims} distinct (key,value) pairs "
              f"across {n_keys} dimension keys.")
        # Make at-least-once delivery visible instead of silent (T04).
        print(f"  deduplicated: {dup_in_batch} duplicate events within this batch, "
              f"{dup_persisted} already persisted by an earlier run.")
        if bad_objects:
            print(f"  WARNING: {len(bad_objects)} object(s) had JSON decode errors and "
                  f"were NOT marked processed (they stay eligible for replay):")
            for k, nerr in bad_objects[:10]:
                print(f"    {nerr} decode error(s) in s3://{args.bucket}/{k}")
        if out_of_retention:
            print(f"  NOTE: {out_of_retention} events predate the {RAW_RETENTION_DAYS}-day "
                  f"raw retention window, so cross-run duplicates of those cannot be "
                  f"detected (within-batch duplicates were dropped).")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
