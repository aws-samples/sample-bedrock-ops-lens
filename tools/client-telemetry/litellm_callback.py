"""LiteLLM → Bedrock Ops Lens event callback.

Emits ONE metadata-only NDJSON event per request (never prompt/response text)
into the S3 layout the dashboard's proxy-events ingester already reads:

    s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl

Covers EVERY LiteLLM backend — Bedrock, direct Anthropic API, direct OpenAI
API — so one callback lights up runtime / mantle / anthropic-api / openai-api
slices, the By-Provider rollup, and per-user/team attribution.

Install:
    1. pip install boto3 (usually already present alongside litellm)
    2. In litellm config.yaml:
         litellm_settings:
           callbacks: ["ops_lens_callback.ops_lens_handler"]
    3. Env vars:
         OPS_LENS_EVENTS_BUCKET=<your-bucket>      # required
         OPS_LENS_REGION=<partition-region>        # default us-east-1
    4. Grant the dashboard's ingester role read on the bucket (see the
       README "Workloads: per-workload attribution" section).

Attribution dimensions come from the request's litellm metadata, e.g.:
    client.chat.completions.create(..., extra_body={"metadata": {
        "workload": "search", "team": "ml-platform", "user": "jsmith@corp.com"}})
LiteLLM virtual-key user/team fields are picked up automatically when present.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import boto3

_BUCKET = os.environ.get("OPS_LENS_EVENTS_BUCKET", "")
_REGION = os.environ.get("OPS_LENS_REGION", "us-east-1")

# Buffer events and flush in batches so high-QPS proxies don't do one PUT per
# request. Flush on size or age, whichever first.
_BUF: list[dict] = []
_LOCK = threading.Lock()
_LAST_FLUSH = time.time()
_FLUSH_EVERY_N = 200
_FLUSH_EVERY_S = 30
_s3 = None


def _endpoint_for(model: str, provider: str) -> str:
    p = (provider or "").lower()
    if "bedrock" in p:
        # LiteLLM's bedrock provider covers runtime; a mantle base_url routing
        # would surface as an openai-compatible provider against Bedrock —
        # stamp explicitly via metadata {"endpoint": "mantle"} in that case.
        return "runtime"
    if "anthropic" in p:
        return "anthropic-api"
    if "openai" in p or "azure" in p:
        return "openai-api"
    return "runtime"


def _flush_locked() -> None:
    global _BUF, _LAST_FLUSH, _s3
    if not _BUF or not _BUCKET:
        _BUF = []
        return
    events, _BUF = _BUF, []
    _LAST_FLUSH = time.time()
    now = datetime.now(timezone.utc)
    key = (f"proxy-events/{_REGION}/{now:%Y/%m/%d/%H}/"
           f"litellm-{now:%M%S}-{uuid.uuid4().hex[:8]}.jsonl.gz")
    body = gzip.compress(
        b"\n".join(json.dumps(e, separators=(",", ":")).encode() for e in events))
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=_REGION)
    try:
        _s3.put_object(Bucket=_BUCKET, Key=key, Body=body)
    except Exception:  # noqa: BLE001 — telemetry must NEVER break inference
        pass


def ops_lens_handler(kwargs, completion_response, start_time, end_time):
    """LiteLLM success_callback signature. Metadata-only; fail-open."""
    try:
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        usage = getattr(completion_response, "usage", None) or {}
        get = (lambda o, k: getattr(o, k, None) if not isinstance(o, dict)
               else o.get(k))

        dims = {}
        for k in ("workload", "team", "env", "business_unit", "cost_center"):
            if meta.get(k):
                dims[k] = str(meta[k])
        # user: explicit metadata beats LiteLLM key identity beats none
        user = meta.get("user") or kwargs.get("user") \
            or meta.get("user_api_key_user_email") or meta.get("user_api_key_alias")
        if user:
            dims["user"] = str(user)
        if meta.get("user_api_key_team_alias") and "team" not in dims:
            dims["team"] = str(meta["user_api_key_team_alias"])

        model = kwargs.get("model") or ""
        provider = (kwargs.get("litellm_params") or {}).get("custom_llm_provider") or ""
        endpoint = str(meta.get("endpoint") or _endpoint_for(model, provider))

        event = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dimensions": dims,
            "model": model,
            "endpoint": endpoint,
            "region": _REGION,
            "input_tokens": int(get(usage, "prompt_tokens") or 0),
            "output_tokens": int(get(usage, "completion_tokens") or 0),
            "cache_read_tokens": int(get(usage, "cache_read_input_tokens") or 0),
            "status": 200,
            "throttled": False,
            "latency_ms": max(0.0, (end_time - start_time).total_seconds() * 1000),
            "request_id": str(getattr(completion_response, "id", "") or uuid.uuid4()),
        }
        # response_cost is LiteLLM's own estimate when pricing is known.
        cost = kwargs.get("response_cost")
        if cost:
            event["cost_usd_est"] = float(cost)

        with _LOCK:
            _BUF.append(event)
            if len(_BUF) >= _FLUSH_EVERY_N or time.time() - _LAST_FLUSH > _FLUSH_EVERY_S:
                _flush_locked()
    except Exception:  # noqa: BLE001 — telemetry must NEVER break inference
        pass
