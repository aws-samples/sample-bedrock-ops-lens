#!/usr/bin/env python3
"""
Reference GenAI proxy for Bedrock Ops Lens — per-workload telemetry.

This is a MINIMAL, real, self-contained example of the "GenAI proxy" pattern
the Workloads tab is fed by. It:

  1. Makes REAL Amazon Bedrock calls (bedrock-runtime InvokeModel via inference
     profiles, and — if reachable — the bedrock-mantle OpenAI-compatible
     endpoint), reading the ACTUAL token counts / latency / status from each
     response. Nothing is fabricated.
  2. Tags each call with a `workload` (the attribution key an application would
     set per use-case).
  3. Drops ONE metadata-only NDJSON event per request to S3 in the layout the
     ingester reads:
         s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl
     NEVER writing prompt or response text — only counts + metadata.

A production proxy would sit in the request path and emit one event per real
user request; this tool drives a handful of representative calls so the feature
can be validated with genuine data end-to-end.

Usage:
    python bedrock_proxy.py \
      --bucket bedrock-ops-lens-proxytest-<ACCOUNT_ID>-<region> \
      --region us-west-2 \
      --calls 20

Requires credentials with bedrock:InvokeModel and s3:PutObject on the bucket.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

# Representative workloads + the model each tends to use, plus the ARBITRARY
# custom dimensions a real proxy would attach (env, business_unit, …). A real
# proxy learns these from request headers/context; here we drive a spread so
# the dashboard shows a realistic multi-dimension picture. Models are inference
# profiles (on-demand bare ids aren't invocable for these).
#
# Each tuple: (dimensions_map, model_id, prompt)
WORKLOADS = [
    ({"workload": "chat-assistant", "env": "prod", "business_unit": "consumer"},
     "us.anthropic.claude-haiku-4-5-20251001-v1:0",
     "Answer in one short sentence: what is Amazon Bedrock?"),
    ({"workload": "summarizer", "env": "prod", "business_unit": "consumer"},
     "us.anthropic.claude-haiku-4-5-20251001-v1:0",
     "Summarize in 5 words: Amazon Bedrock is a managed service for foundation models."),
    ({"workload": "code-helper", "env": "dev", "business_unit": "platform"},
     "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "Write a one-line Python list comprehension for squares of 0..4."),
    ({"workload": "doc-qa", "env": "prod", "business_unit": "platform"},
     "us.anthropic.claude-haiku-4-5-20251001-v1:0",
     "In one sentence, what does TPM stand for in Bedrock quotas?"),
]


def _runtime_client(region: str):
    return boto3.client("bedrock-runtime", region_name=region,
                        config=Config(retries={"max_attempts": 2, "mode": "standard"}))


def _invoke_runtime(brt, model_id: str, prompt: str) -> dict:
    """One real Anthropic-on-Bedrock call. Returns real metadata (no text)."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": prompt}],
    })
    t0 = time.time()
    status = 200
    throttled = False
    in_tok = out_tok = cache_read = 0
    request_id = None
    try:
        resp = brt.invoke_model(modelId=model_id, body=body,
                                contentType="application/json")
        request_id = resp.get("ResponseMetadata", {}).get("RequestId")
        payload = json.loads(resp["body"].read())
        usage = payload.get("usage", {}) or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    except Exception as e:
        name = type(e).__name__
        # Map real errors to a status the proxy would have observed.
        if "Throttling" in name or "TooManyRequests" in name:
            status, throttled = 429, True
        elif "AccessDenied" in name or "Unauthorized" in name:
            status = 403
        elif "Validation" in name:
            status = 400
        else:
            status = 500
        print(f"    (call error {name} -> status {status})", file=sys.stderr)
    latency_ms = round((time.time() - t0) * 1000, 1)
    return {
        "input_tokens": in_tok, "output_tokens": out_tok,
        "cache_read_tokens": cache_read, "status": status,
        "throttled": throttled, "latency_ms": latency_ms,
        "request_id": request_id,
    }


def _write_events_to_s3(s3, bucket: str, region: str, events: list[dict]) -> str:
    """Write events grouped into the hour-partitioned NDJSON layout the
    ingester expects. One file per (this run) under the current UTC hour."""
    now = datetime.now(timezone.utc)
    key = (f"proxy-events/{region}/{now:%Y}/{now:%m}/{now:%d}/{now:%H}/"
           f"events-{now:%Y%m%dT%H%M%S}Z.jsonl.gz")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for ev in events:
            gz.write((json.dumps(ev) + "\n").encode("utf-8"))
    buf.seek(0)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue(),
                  ContentType="application/gzip")
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description="Real Bedrock reference proxy → S3 proxy-events.")
    ap.add_argument("--bucket", required=True, help="S3 bucket the ingester reads")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--calls", type=int, default=20, help="total real Bedrock calls to make")
    args = ap.parse_args()

    brt = _runtime_client(args.region)
    s3 = boto3.client("s3", region_name=args.region)

    events: list[dict] = []
    print(f"Making {args.calls} REAL Bedrock calls in {args.region}...")
    for i in range(args.calls):
        dims, model, prompt = WORKLOADS[i % len(WORKLOADS)]
        meta = _invoke_runtime(brt, model, prompt)
        ev = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "dimensions": dims,      # arbitrary custom attribution map
            "model": model,
            "endpoint": "runtime",   # these are real bedrock-runtime calls
            "region": args.region,
            **meta,
        }
        # request_id may be None on error; the ingester falls back to a synthetic
        # dedup key, but real successful calls carry the true Bedrock request id.
        if ev.get("request_id") is None:
            ev["request_id"] = f"proxy_{int(time.time()*1000)}_{i}"
        events.append(ev)
        wl = dims.get("workload", "—")
        print(f"  [{i+1}/{args.calls}] {wl:16s} {model.split('.')[-1][:24]:24s} "
              f"in={meta['input_tokens']:4d} out={meta['output_tokens']:4d} "
              f"status={meta['status']} {meta['latency_ms']}ms")

    key = _write_events_to_s3(s3, args.bucket, args.region, events)
    ok = sum(1 for e in events if e["status"] == 200)
    tot_in = sum(e["input_tokens"] for e in events)
    tot_out = sum(e["output_tokens"] for e in events)
    print(f"\nWrote {len(events)} REAL events → s3://{args.bucket}/{key}")
    print(f"  {ok}/{len(events)} succeeded · {tot_in} input + {tot_out} output tokens (real)")
    print(f"  workloads: {sorted(set(e['dimensions'].get('workload','—') for e in events))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
