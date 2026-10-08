#!/usr/bin/env python3
"""Busiest-MINUTE capacity observations from CloudWatch, per UTC day.

WHY THIS EXISTS. f_hourly_peak holds Period=3600 Sum, so the API can only
divide by 60 and report an hourly average per minute. AWS enforces token quotas
per minute, so a bursty workload throttles while that average looks safe.
Measured 2026-10-06 on us.anthropic.claude-sonnet-5: hourly average 2,838 TPM
against a true busiest minute of 90,888 TPM.

WHY A RAW PULL RATHER THAN METRIC MATH. `TIME_SERIES(MAX(m))` with Period=86400
does NOT return per-day maxima - it broadcasts one window-wide maximum as a
constant series (verified: seven identical values across a 7-day window), and
its timestamps are not the peak's time. Reducing a Period=60 series here also
buys three things metric math cannot:

  * per-minute choice between the native metric and the reconstruction, so a
    partially published native series cannot suppress a higher reconstructed
    minute elsewhere;
  * the real timestamp of the peak minute;
  * summing every contributing source series BEFORE the maximum.

THAT LAST POINT IS THE CORRECTNESS CORE. Application inference profile traffic
and direct traffic on the same model are separate CloudWatch series. They must be
added minute by minute and only then reduced:

    direct [100, 0] + profile [0, 100]  -> peak 100   (maxima would sum to 200)
    direct [100, 0] + profile [100, 0]  -> peak 200   (max of maxima gives 100)

So resolution happens here, not in a reporting view: once only per-identifier
maxima survive, no later SQL recovers the right answer.

Usage:
    python -m ingestion.cw_minute_peak --accounts 123456789012 --regions us-east-1 --days 14
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import asyncpg
import boto3
from botocore.config import Config

from .accounts import (
    _add_common_args,
    discover_accounts,
    session_for,
)

try:
    from app import rate_catalog
except ImportError:  # pragma: no cover - import path differs in Lambda
    from backend.app import rate_catalog

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# CloudWatch retains 1-minute datapoints for 15 days. Asking for more silently
# returns a coarser period, which would be recorded here as a minute peak.
MAX_MINUTE_DAYS = 15

# GetMetricData returns at most 100,800 datapoints per call. One day at
# Period=60 is 1,440 per series, so 5 metrics x 12 identifiers = 86,400 leaves
# headroom for the boundary minute.
METRICS = ("Invocations", "InputTokenCount", "OutputTokenCount",
           "CacheWriteInputTokenCount", "EstimatedTPMQuotaUsage")
IDS_PER_CALL = 12


def _cw(region: str, session: boto3.Session | None = None):
    s = session or boto3._get_default_session()
    return s.client("cloudwatch", region_name=region,
                    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))


def utc_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[midnight, next midnight) in UTC, so a peak belongs to exactly one day."""
    start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def minute_window(days: int, now: datetime | None = None) -> list[date]:
    """The UTC days to collect, newest last. Today is included and marked partial."""
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days), MAX_MINUTE_DAYS))
    today = now.date()
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]


def list_model_ids(cw) -> list[str]:
    """Distinct ModelId dimension values in AWS/Bedrock for this region."""
    seen: set[str] = set()
    for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/Bedrock"):
        for m in page["Metrics"]:
            for d in m.get("Dimensions", []):
                if d["Name"] == "ModelId" and d.get("Value"):
                    seen.add(d["Value"])
    return sorted(seen)


async def load_profile_map(conn, account: str, region: str) -> dict[str, str]:
    """{invocation identifier -> effective model id} for this account/Region.

    Reads the cache inference_profiles.py fills. Both the ARN and the bare
    12-character id are keys, matching CloudWatch's observed forms. Profiles with
    an unresolved model (model_id NULL) are deliberately absent: their traffic
    stays under its own identifier and is reported as an unresolved profile
    rather than being folded into a guessed model.
    """
    rows = await conn.fetch(
        """
        SELECT profile_arn, profile_id, model_id
          FROM public.dim_inference_profiles
         WHERE accountId = $1 AND region = $2 AND model_id IS NOT NULL
        """,
        account, region,
    )
    out: dict[str, str] = {}
    for r in rows:
        for key in (r["profile_arn"], r["profile_id"]):
            if key:
                out[key] = r["model_id"]
    return out


def _queries(ids: list[str]) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Period=60 Sum for every (identifier, metric). idx -> (identifier, metric)."""
    queries: list[dict] = []
    idx: dict[str, tuple[str, str]] = {}
    for i, mid in enumerate(ids):
        for j, metric in enumerate(METRICS):
            qid = f"m{i}_{j}"
            queries.append({
                "Id": qid,
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {"Namespace": "AWS/Bedrock", "MetricName": metric,
                               "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                    "Period": 60, "Stat": "Sum",
                },
            })
            idx[qid] = (mid, metric)
    return queries, idx


def fetch_minutes(cw, ids: list[str], start: datetime, end: datetime):
    """Per-minute series plus per-series completeness.

    Returns (series, incomplete) where series is
    {identifier: {metric: {minute: value}}} and `incomplete` lists any series
    CloudWatch did not report as Complete. A missing or partial series is never
    filled with zero: the caller records the gap instead.
    """
    series: dict[str, dict[str, dict[datetime, float]]] = defaultdict(lambda: defaultdict(dict))
    incomplete: list[str] = []
    for chunk_start in range(0, len(ids), IDS_PER_CALL):
        chunk = ids[chunk_start:chunk_start + IDS_PER_CALL]
        queries, idx = _queries(chunk)
        token = None
        tokens: set[str] = set()
        statuses: dict[str, str] = {}
        invalid: set[str] = set()
        while True:
            kwargs = dict(StartTime=start, EndTime=end, MetricDataQueries=queries,
                          ScanBy="TimestampAscending")
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                mid, metric = idx[r["Id"]]
                # PartialData during pagination is expected; the final page's
                # status is what counts, so keep the last one seen.
                statuses[r["Id"]] = r.get("StatusCode") or statuses.get(r["Id"], "")
                bucket = series[mid][metric]
                timestamps, values = r.get("Timestamps", []), r.get("Values", [])
                if len(timestamps) != len(values):
                    invalid.add(r["Id"])
                for ts, value in zip(timestamps, values):
                    ts = ts.astimezone(timezone.utc)
                    if (not math.isfinite(value) or value < 0
                            or ts.second or ts.microsecond or not start <= ts < end):
                        invalid.add(r["Id"])
                        continue
                    bucket[ts] = value
            token = resp.get("NextToken")
            if not token:
                break
            if token in tokens:
                raise ValueError("GetMetricData repeated a pagination token")
            tokens.add(token)
        for qid in idx:
            status = "invalid datapoints" if qid in invalid else statuses.get(qid, "missing result")
            if status != "Complete":
                mid, metric = idx[qid]
                incomplete.append(f"{mid}/{metric}:{status or 'unknown'}")
    return series, incomplete


def reduce_day(group_ids: list[str], series, rate: int) -> dict:
    """Reduce one effective model's minutes to that day's peaks.

    Per minute, each contributing identifier contributes its native
    EstimatedTPMQuotaUsage when present, else the reconstruction
    input + cache_write + output * rate. The identifier values are summed, and
    only then is the maximum taken.

    Provenance is recorded per minute and unioned: a native series covering only
    part of the day must not suppress a higher reconstructed minute elsewhere,
    and a genuinely observed zero stays an observation rather than a gap.
    """
    def at(mid: str, metric: str, minute) -> float | None:
        return series.get(mid, {}).get(metric, {}).get(minute)

    minutes = sorted({m for mid in group_ids for metric in METRICS
                      for m in series.get(mid, {}).get(metric, {})})
    peak = {"peak_rpm": None, "peak_rpm_at": None, "peak_input_tpm": None,
            "peak_output_tpm": None, "peak_quota_tpm": None, "peak_quota_tpm_at": None,
            "active_minutes": 0, "sources": set()}
    missing_quota = missing_requests = missing_raw_tokens = False
    for minute in minutes:
        req = inp = out = 0.0
        quota = 0.0
        saw_any = False
        saw_req = saw_input = saw_output = saw_quota = False
        for mid in group_ids:
            r = at(mid, "Invocations", minute)
            i = at(mid, "InputTokenCount", minute)
            o = at(mid, "OutputTokenCount", minute)
            cw_tok = at(mid, "CacheWriteInputTokenCount", minute)
            native = at(mid, "EstimatedTPMQuotaUsage", minute)
            if r is None and i is None and o is None and cw_tok is None and native is None:
                continue
            saw_any = True
            missing_requests |= r is None
            missing_raw_tokens |= (i is None and o is None and cw_tok is None
                                   and bool(r or native))
            saw_req |= r is not None
            saw_input |= i is not None or cw_tok is not None
            saw_output |= o is not None
            req += r or 0.0
            # Quota-consuming input per the AWS burndown doc: InputTokenCount
            # plus cache WRITE. Cache reads consume no Runtime TPM quota (they
            # are still billed).
            inp += (i or 0.0) + (cw_tok or 0.0)
            out += o or 0.0
            if native is None:
                if i is not None or o is not None or cw_tok is not None:
                    quota += (i or 0.0) + (cw_tok or 0.0) + (o or 0.0) * rate
                    peak["sources"].add("reconstructed")
                    saw_quota = True
                elif r:
                    # Requests alone do not establish token consumption. In a
                    # combined model, even one such source makes TPM incomplete.
                    missing_quota = True
            else:
                quota += native
                peak["sources"].add("aws_estimate")
                saw_quota = True
        if not saw_any:
            continue
        peak["active_minutes"] += 1
        if saw_req and (peak["peak_rpm"] is None or req > peak["peak_rpm"]):
            peak["peak_rpm"], peak["peak_rpm_at"] = req, minute
        if saw_input:
            peak["peak_input_tpm"] = inp if peak["peak_input_tpm"] is None else max(peak["peak_input_tpm"], inp)
        if saw_output:
            peak["peak_output_tpm"] = out if peak["peak_output_tpm"] is None else max(peak["peak_output_tpm"], out)
        if saw_quota and (peak["peak_quota_tpm"] is None or quota > peak["peak_quota_tpm"]):
            peak["peak_quota_tpm"], peak["peak_quota_tpm_at"] = quota, minute
    s = peak.pop("sources")
    if missing_quota:
        peak["peak_quota_tpm"] = peak["peak_quota_tpm_at"] = None
        s.clear()
    if missing_requests:
        peak["peak_rpm"] = peak["peak_rpm_at"] = None
    if missing_raw_tokens:
        peak["peak_input_tpm"] = peak["peak_output_tpm"] = None
    peak["quota_tpm_source"] = ("mixed" if len(s) > 1 else (next(iter(s)) if s else "unavailable"))
    for k in ("peak_rpm", "peak_input_tpm", "peak_output_tpm", "peak_quota_tpm"):
        if peak[k] is not None:
            peak[k] = int(round(peak[k]))
    return peak


def combine_minutes(group_ids: list[str], series, rate: int) -> list[dict]:
    """Per-minute series for one effective model, under reduce_day's rules.

    Every contributing identifier is summed minute by minute. Quota use per
    identifier is the native EstimatedTPMQuotaUsage when published, else the
    reconstruction input + cache_write + output * rate. A value is None, never
    zero, when that minute cannot establish it:

    * requests: some contributing identifier reported tokens but no Invocations;
    * input/output tokens: requests or native quota with no raw token metrics;
    * quota: requests with neither tokens nor a native datapoint.

    A minute whose every contributing identifier reported zero Invocations is
    an observed idle minute: its tokens and quota are zero, not unknown.

    reduce_day's peaks equal the maxima of this series wherever it reports a
    peak; tests pin that equivalence so the live chart and the stored daily
    peaks cannot disagree.
    """
    def at(mid: str, metric: str, minute) -> float | None:
        return series.get(mid, {}).get(metric, {}).get(minute)

    minutes = sorted({m for mid in group_ids for metric in METRICS
                      for m in series.get(mid, {}).get(metric, {})})
    out: list[dict] = []
    for minute in minutes:
        req = inp = outp = quota = 0.0
        saw_any = saw_req = saw_input = saw_output = saw_quota = False
        req_gap = token_gap = quota_gap = False
        sources: set[str] = set()
        for mid in group_ids:
            r = at(mid, "Invocations", minute)
            i = at(mid, "InputTokenCount", minute)
            o = at(mid, "OutputTokenCount", minute)
            cw_tok = at(mid, "CacheWriteInputTokenCount", minute)
            native = at(mid, "EstimatedTPMQuotaUsage", minute)
            if r is None and i is None and o is None and cw_tok is None and native is None:
                continue
            saw_any = True
            req_gap |= r is None
            token_gap |= (i is None and o is None and cw_tok is None and bool(r or native))
            saw_req |= r is not None
            saw_input |= i is not None or cw_tok is not None
            saw_output |= o is not None
            req += r or 0.0
            inp += (i or 0.0) + (cw_tok or 0.0)
            outp += o or 0.0
            if native is not None:
                quota += native
                sources.add("aws_estimate")
                saw_quota = True
            elif i is not None or o is not None or cw_tok is not None:
                quota += (i or 0.0) + (cw_tok or 0.0) + (o or 0.0) * rate
                sources.add("reconstructed")
                saw_quota = True
            elif r:
                quota_gap = True
        if not saw_any:
            continue
        idle = saw_req and not req_gap and req == 0

        def value(gap: bool, seen: bool, total: float) -> int | None:
            if gap:
                return None
            if seen:
                return int(round(total))
            return 0 if idle else None

        out.append({
            "minute": minute,
            "requests": None if (req_gap or not saw_req) else int(round(req)),
            "input_tokens": value(token_gap, saw_input, inp),
            "output_tokens": value(token_gap, saw_output, outp),
            "quota_tpm": value(quota_gap, saw_quota, quota),
            "quota_source": ("mixed" if len(sources) > 1
                             else next(iter(sources)) if sources else "unavailable"),
        })
    return out


async def _upsert(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO public.f_minute_peak (
            event_date, accountId, modelId, region, endpoint,
            peak_rpm, peak_rpm_at, peak_input_tpm, peak_output_tpm,
            peak_quota_tpm, peak_quota_tpm_at, quota_tpm_source,
            burndown_rate, burndown_rate_source, source_ids,
            has_application_profile, active_minutes, resolution_stale, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18, now())
        ON CONFLICT (event_date, accountId, modelId, region, endpoint) DO UPDATE SET
            peak_rpm = EXCLUDED.peak_rpm,
            peak_rpm_at = EXCLUDED.peak_rpm_at,
            peak_input_tpm = EXCLUDED.peak_input_tpm,
            peak_output_tpm = EXCLUDED.peak_output_tpm,
            peak_quota_tpm = EXCLUDED.peak_quota_tpm,
            peak_quota_tpm_at = EXCLUDED.peak_quota_tpm_at,
            quota_tpm_source = EXCLUDED.quota_tpm_source,
            burndown_rate = EXCLUDED.burndown_rate,
            burndown_rate_source = EXCLUDED.burndown_rate_source,
            source_ids = EXCLUDED.source_ids,
            has_application_profile = EXCLUDED.has_application_profile,
            active_minutes = EXCLUDED.active_minutes,
            resolution_stale = EXCLUDED.resolution_stale,
            updated_at = now()
        """,
        rows,
    )
    return len(rows)


async def _record_collection(conn, row: dict) -> None:
    await conn.execute(
        """
        INSERT INTO public.f_minute_collection (
            event_date, accountId, region, endpoint, window_start, window_end,
            status, detail, series_requested, series_complete, partial_day,
            last_success_at, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now())
        ON CONFLICT (event_date, accountId, region, endpoint) DO UPDATE SET
            window_start = EXCLUDED.window_start,
            window_end = EXCLUDED.window_end,
            status = EXCLUDED.status,
            detail = EXCLUDED.detail,
            series_requested = EXCLUDED.series_requested,
            series_complete = EXCLUDED.series_complete,
            partial_day = EXCLUDED.partial_day,
            -- Keep the previous success stamp when this attempt failed.
            last_success_at = COALESCE(EXCLUDED.last_success_at,
                                       public.f_minute_collection.last_success_at),
            updated_at = now()
        """,
        row["event_date"], row["account"], row["region"], row["endpoint"],
        row["window_start"], row["window_end"], row["status"], row.get("detail"),
        row["series_requested"], row["series_complete"], row["partial_day"],
        row.get("last_success_at"),
    )


async def invalidate_changed_peaks(conn, account: str, region: str, profile_map: dict,
                                   catalog) -> None:
    """Old maxima cannot be recombined after a mapping or multiplier changes."""
    rows = await conn.fetch(
        """SELECT event_date, modelid, source_ids, quota_tpm_source, burndown_rate
             FROM public.f_minute_peak
            WHERE accountid=$1 AND region=$2 AND endpoint='runtime'
              AND NOT resolution_stale""", account, region)
    changed = []
    for row in rows:
        model = row["modelid"]
        mapping_changed = any(profile_map.get(raw, raw) != model
                              for raw in row["source_ids"])
        rate_changed = (row["quota_tpm_source"] in ("reconstructed", "mixed")
                        and row["burndown_rate"] != catalog.rate_for(
                            model, on_date=row["event_date"]).rate)
        if mapping_changed or rate_changed:
            # All peaks for this date are invalidated together: a newly mapped
            # profile may also need to be added to an existing direct-model row.
            changed.append((row["event_date"], account, region))
    if changed:
        await conn.executemany(
            """UPDATE public.f_minute_peak SET resolution_stale=TRUE
                WHERE event_date=$1 AND accountid=$2 AND region=$3
                  AND endpoint='runtime'""", sorted(set(changed)))


async def collect_region(conn, account: str, region: str, days: int,
                         session: boto3.Session | None, catalog=None) -> dict:
    cw = _cw(region, session)
    catalog = catalog or await rate_catalog.snapshot_with_conn(conn)
    profile_map = await load_profile_map(conn, account, region)
    await invalidate_changed_peaks(conn, account, region, profile_map, catalog)
    now = datetime.now(timezone.utc)
    window = minute_window(days, now)
    try:
        ids = list_model_ids(cw)
    except Exception as exc:  # noqa: BLE001 - record and move on
        for day in window:
            start, end = utc_day_bounds(day)
            await _record_collection(conn, {
                "event_date": day, "account": account, "region": region,
                "endpoint": "runtime", "window_start": start, "window_end": min(end, now),
                "status": "failed", "detail": f"list_metrics: {type(exc).__name__}",
                "series_requested": 0, "series_complete": 0,
                "partial_day": day == now.date()})
        return {"account": account, "region": region, "status": "failed",
                "detail": f"list_metrics: {type(exc).__name__}", "rows": 0}
    # ListMetrics can stop listing an inactive source before minute retention
    # expires. Include previously observed sources so a successful replacement
    # does not silently drop their still-queryable history.
    previous = await conn.fetch(
        """SELECT DISTINCT unnest(source_ids) AS source_id
             FROM public.f_minute_peak
            WHERE accountid=$1 AND region=$2 AND endpoint='runtime'
              AND event_date BETWEEN $3 AND $4""",
        account, region, window[0], window[-1])
    ids = sorted(set(ids) | {r["source_id"] for r in previous})

    # Group raw identifiers by the model they actually ran on. An unmapped
    # identifier groups under itself, so an unresolved profile stays visible.
    groups: dict[str, list[str]] = defaultdict(list)
    for raw in ids:
        groups[profile_map.get(raw, raw)].append(raw)

    today = now.date()
    written = 0
    statuses = []
    for day in window:
        start, end = utc_day_bounds(day)
        now = datetime.now(timezone.utc)
        capped_end = min(end, now)
        if capped_end <= start:
            continue
        try:
            series, incomplete = fetch_minutes(cw, ids, start, capped_end)
            status = "partial" if incomplete else "complete"
            detail = ("; ".join(incomplete[:5]) or None) if incomplete else None
        except Exception as exc:  # noqa: BLE001
            # Never write peaks from a failed pull: absent rows plus a failed
            # collection status beat a confident zero.
            await _record_collection(conn, {
                "event_date": day, "account": account, "region": region,
                "endpoint": "runtime", "window_start": start, "window_end": capped_end,
                "status": "failed", "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                "series_requested": len(ids) * len(METRICS), "series_complete": 0,
                "partial_day": day == today, "last_success_at": None})
            statuses.append("failed")
            continue

        statuses.append(status)
        collection = {
            "event_date": day, "account": account, "region": region,
            "endpoint": "runtime", "window_start": start, "window_end": capped_end,
            "status": status, "detail": detail,
            "series_requested": len(ids) * len(METRICS),
            "series_complete": len(ids) * len(METRICS) - len(incomplete),
            "partial_day": day == today,
            "last_success_at": now if status == "complete" else None}
        if incomplete:
            # Retain the last complete observations. A partial retry must never
            # replace a higher peak with a falsely reassuring lower one.
            await _record_collection(conn, collection)
            continue

        rows = []
        for effective, group_ids in sorted(groups.items()):
            resolved_rate = catalog.rate_for(effective, on_date=day)
            rate, rate_source = resolved_rate.rate, resolved_rate.source
            peak = reduce_day(group_ids, series, rate)
            if not peak["active_minutes"]:
                continue
            active_ids = [g for g in group_ids if any(series.get(g, {}).values())]
            has_profile = any(g in profile_map for g in active_ids)
            rows.append((
                day, account, effective, region, "runtime",
                peak["peak_rpm"], peak["peak_rpm_at"], peak["peak_input_tpm"],
                peak["peak_output_tpm"], peak["peak_quota_tpm"], peak["peak_quota_tpm_at"],
                peak["quota_tpm_source"], rate, rate_source, sorted(active_ids),
                has_profile, peak["active_minutes"], False,
            ))
        async with conn.transaction():
            # Replace this successful day's snapshot, including old raw-ID rows
            # now merged into a resolved model. Never delete on partial failure.
            await conn.execute(
                """DELETE FROM public.f_minute_peak
                    WHERE event_date=$1 AND accountid=$2 AND region=$3
                      AND endpoint='runtime'""", day, account, region)
            written += await _upsert(conn, rows)
            await _record_collection(conn, collection)
    status = ("complete" if all(s == "complete" for s in statuses)
              else "failed" if all(s == "failed" for s in statuses) else "partial")
    return {"account": account, "region": region, "status": status, "rows": written}


async def run(args) -> int:
    accts = discover_accounts(args)
    if not accts:
        print("ERROR: no monitored accounts resolved", file=sys.stderr)
        return 2

    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        try:
            from .config import load_config
            regions = load_config().resolved_regions()
        except Exception:  # noqa: BLE001
            regions = [os.environ.get("BEDROCK_REGION", "us-east-1")]

    days = max(1, min(int(args.days), MAX_MINUTE_DAYS))
    print(f"Ingesting minute peaks: {len(accts)} account(s), regions={regions}, "
          f"{days} UTC day(s)")

    conn = await asyncpg.connect(args.db_url)
    failures: list[tuple[str, str, str]] = []
    total = 0
    try:
        catalog = await rate_catalog.snapshot_with_conn(conn)
        for monitored in accts:
            acct = monitored.accountId
            try:
                session = session_for(acct, role_name=args.role_name,
                                      external_id=args.external_id)
            except Exception as e:  # noqa: BLE001
                msg = f"sts:AssumeRole failed: {type(e).__name__}: {e}"
                print(f"  [{acct}] SKIP — {msg}", flush=True)
                failures.append((acct, "*", msg))
                continue
            for region in regions:
                try:
                    out = await collect_region(conn, acct, region, days, session, catalog)
                    total += out.get("rows", 0)
                    extra = f": {out['detail']}" if out.get("detail") else ""
                    print(f"  [{acct}/{region}] f_minute_peak: {out.get('rows', 0)} rows "
                          f"({out['status']}{extra})", flush=True)
                    if out["status"] != "complete":
                        failures.append((acct, region, out.get("detail") or out["status"]))
                except Exception as e:  # noqa: BLE001 — one region must not kill the run
                    msg = f"{type(e).__name__}: {e}"
                    print(f"  [{acct}/{region}] ERROR — {msg}", flush=True)
                    failures.append((acct, region, msg))

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_minute_peak_attempt', $1, now()),
                   ('last_minute_peak_days', $2, now()),
                   ('last_minute_peak_status', $3, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(), str(days),
            "partial" if failures else "complete",
        )
        if not failures:
            await conn.execute(
                """INSERT INTO ingestion_meta (key, value, updated_at)
                   VALUES ('last_minute_peak_refresh', $1, now())
                   ON CONFLICT (key) DO UPDATE SET
                     value=EXCLUDED.value, updated_at=EXCLUDED.updated_at""",
                datetime.now(timezone.utc).isoformat())
    finally:
        await conn.close()

    print(f"[cw_minute_peak] wrote {total} rows")
    if failures:
        print(f"DONE with {len(failures)} failure(s):")
        for acct, region, msg in failures:
            print(f"  [{acct}/{region}] {msg}")
        return 1
    print("DONE.")
    return 0


async def main() -> int:
    """Coroutine, NOT a sync wrapper.

    lambda_handler._run_module awaits each module's main() from inside an already
    running event loop, so calling asyncio.run() here raises
    "asyncio.run() cannot be called from a running event loop" and the module
    fails in Lambda while working fine from the CLI. Every other ingester in this
    package is an `async def main`; this one must match.
    """
    ap = argparse.ArgumentParser(description="Busiest-minute Bedrock capacity peaks")
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    ap.add_argument("--days", type=int, default=int(os.environ.get("MINUTE_PEAK_DAYS", "14")))
    _add_common_args(ap)
    ap.add_argument("--regions", default="",
                    help="comma-separated AWS regions; defaults to config.yaml monitored_regions")
    args = ap.parse_args()
    if args.days > MAX_MINUTE_DAYS:
        print(f"[cw_minute_peak] capping --days {args.days} at {MAX_MINUTE_DAYS}"
              " (CloudWatch retains 1-minute datapoints for 15 days)")
    return await run(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
