"""LiteLLM → Bedrock Ops Lens event callback.

Emits ONE metadata-only NDJSON event per request (never prompt/response text)
into the S3 layout the dashboard's proxy-events ingester already reads:

    s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl

Covers EVERY LiteLLM backend — Bedrock, direct Anthropic API, direct OpenAI
API — so one callback lights up runtime / mantle / anthropic-api / openai-api
slices, the By-Provider rollup, and per-user/team attribution.

Install:
    1. pip install boto3 (usually already present alongside litellm)
    2. Register the callback. Which mechanism depends on how you run LiteLLM:

       PROXY (`litellm --config config.yaml`) - the proxy only invokes a
       CustomLogger for successes, so a plain function in `success_callback` is
       registered and then never called (verified against 1.93.0: requests
       return 200 and only failures reach S3, i.e. a 100% error rate over zero
       traffic). Register the instance instead, which covers both outcomes:
         litellm_settings:
           callbacks: ["litellm_callback.ops_lens_logger"]

       PYTHON SDK (in your own process) - register the functions, BOTH of them,
       or every failure is invisible and the error/throttle panels read a flat
       zero (audit T02):
         import litellm, litellm_callback
         litellm.success_callback = [litellm_callback.ops_lens_handler]
         litellm.failure_callback = [litellm_callback.ops_lens_failure_handler]

       The module name must match this file's name, and the file must be
       importable by the proxy (its working directory, or on PYTHONPATH).
    3. Env vars:
         OPS_LENS_EVENTS_BUCKET=<your-bucket>      # required
         OPS_LENS_REGION=<partition-region>        # default us-east-1
         OPS_LENS_FLUSH_EVERY_N=200                # optional
         OPS_LENS_FLUSH_EVERY_S=30                 # optional
    4. Grant the dashboard's ingester role read on the bucket (see the
       README "Workloads: per-workload attribution" section).

Attribution dimensions come from the request's litellm metadata, e.g.:
    client.chat.completions.create(..., extra_body={"metadata": {
        "workload": "search", "team": "ml-platform", "user": "jsmith@corp.com"}})
LiteLLM virtual-key user/team fields are picked up automatically when present.

Event grain
    One event per PROVIDER ATTEMPT, not per client request. A request the Router
    retries twice before succeeding emits three events: two throttles and one
    success. That matches how the dashboard decomposes attempts elsewhere
    (attempts = successes + non-throttle failures + throttles, mirroring
    CloudWatch), so a `429 -> 429 -> 200` sequence reads as a 66.67% throttle
    rate rather than 50%. `logical_request_id` groups the attempts of one client
    request, and `attempt` is the 0-based ordinal.

Streaming
    LiteLLM calls `success_callback` once per streamed chunk AND once with the
    aggregated response. Only the aggregated call carries
    `complete_streaming_response` and the usage totals, so that is the only one
    recorded - otherwise a five-chunk stream reports five requests, four of them
    with zero tokens.

Delivery guarantees (audit T03)
    * A background thread flushes on age, so a buffer does not sit unsent when
      traffic goes quiet — the previous version only checked the age while
      handling a NEW request, so the last events of a burst were never written.
    * atexit + SIGTERM flush, so a proxy restart or a container stop does not
      drop what is buffered.
    * A failed upload RE-QUEUES the batch and retries with backoff instead of
      discarding it. The buffer is bounded, and if it ever has to drop events it
      says how many rather than losing them silently.
    * The S3 PUT happens OUTSIDE the lock, so request threads never block on
      network I/O.
    * Events carry an idempotency key, and the ingester deduplicates on it, so a
      retry that partially succeeded cannot double-count.
"""
from __future__ import annotations

import asyncio
import atexit
import gzip
import json
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone

import boto3

_BUCKET = os.environ.get("OPS_LENS_EVENTS_BUCKET", "")
_REGION = os.environ.get("OPS_LENS_REGION", "us-east-1")

# Buffer events and flush in batches so high-QPS proxies don't do one PUT per
# request. Flush on size, on age (from a background thread), or at shutdown.
_BUF: list[dict] = []
_LOCK = threading.Lock()
_LAST_FLUSH = time.time()
_FLUSH_EVERY_N = int(os.environ.get("OPS_LENS_FLUSH_EVERY_N", "200"))
_FLUSH_EVERY_S = float(os.environ.get("OPS_LENS_FLUSH_EVERY_S", "30"))
# Hard cap so a long S3 outage cannot grow the buffer without bound. When it is
# hit we drop the OLDEST events (the newest are the most operationally useful)
# and count what was dropped, so the loss is visible.
_MAX_BUFFERED = int(os.environ.get("OPS_LENS_MAX_BUFFERED", "20000"))
_MAX_UPLOAD_ATTEMPTS = 4
_dropped_events = 0
_s3 = None
_s3_lock = threading.Lock()
_timer: threading.Thread | None = None
_stopping = threading.Event()
# Set by _submit() when the buffer reaches the size threshold, so the worker
# uploads instead of the calling (possibly event-loop) thread.
_WAKE = threading.Event()


def _client():
    global _s3
    with _s3_lock:
        if _s3 is None:
            _s3 = boto3.client("s3", region_name=_REGION)
        return _s3


def _endpoint_for(model: str, provider: str) -> str:
    """Map LiteLLM's provider (and, failing that, the model id) onto the event
    endpoint enum.

    Audit T02: the old version returned "runtime" for ANY unrecognised provider,
    so Vertex, Cohere, Together, Ollama and friends were all reported as Bedrock
    runtime traffic — inflating the Bedrock slices with calls AWS never saw. An
    unknown provider is now reported as unknown, which the dashboard shows as
    such instead of mislabelling it.
    """
    p = (provider or "").lower()
    m = (model or "").lower()
    if "bedrock" in p:
        # LiteLLM's bedrock provider covers runtime; a mantle base_url routing
        # would surface as an openai-compatible provider against Bedrock —
        # stamp it explicitly with metadata {"opslens.endpoint": "mantle"}.
        # Do NOT use a bare "endpoint" key: the proxy overwrites that with its
        # own URL during request preparation (LP-04).
        return "runtime"
    if "anthropic" in p:
        return "anthropic-api"
    if "openai" in p or "azure" in p:
        return "openai-api"
    if not p:
        # No provider at all: fall back to the model id, which is usually enough.
        if m.startswith(("us.", "eu.", "apac.", "global.")) or m.startswith(
                ("anthropic.", "amazon.", "meta.", "mistral.", "cohere.",
                 "ai21.", "deepseek.")):
            return "runtime"
        if "claude" in m:
            return "anthropic-api"
        if m.startswith(("gpt", "o1", "o3", "o4")):
            return "openai-api"
    return "unknown"


# Endpoint values an operator may legitimately stamp via metadata.
_VALID_ENDPOINTS = frozenset(("runtime", "mantle", "anthropic-api", "openai-api"))

# Where an explicit endpoint stamp may legitimately come from, in priority order.
#
# Audit LP-04: `metadata["endpoint"]` was the documented control key, and the
# PROXY overwrites it unconditionally during request preparation - verified by
# calling the real `add_litellm_data_to_request()`, which replaced "mantle" with
# "https://proxy.example.test/v1/chat/completions". An OpenAI-compatible
# Bedrock/Mantle deployment therefore landed in the direct-OpenAI slice.
#
# `opslens.endpoint` is a namespaced key the proxy does not touch. The proxy does
# preserve the caller's original map under `requester_metadata`, so that is
# accepted as a fallback for configs already using the old key. Every source is
# validated against the enum, so the proxy's URL can never win.
_ENDPOINT_STAMP_KEYS = ("opslens.endpoint", "opslens_endpoint", "endpoint")


def _endpoint_stamp(meta: dict) -> str | None:
    """An operator's explicit endpoint stamp, or None."""
    sources = [meta]
    rm = meta.get("requester_metadata")
    if isinstance(rm, dict):
        sources.append(rm)
    for src in sources:
        for k in _ENDPOINT_STAMP_KEYS:
            v = str(src.get(k) or "").strip().lower()
            if v in _VALID_ENDPOINTS:
                return v
    return None

# Metadata keys the LiteLLM proxy injects for its own bookkeeping. They are not
# attribution, and two of them are per-request unbounded values that would blow
# up dimension cardinality (and carry client IP / user-agent, which this emitter
# is explicitly not in the business of shipping).
_PROXY_META_NOISE = frozenset((
    "endpoint", "opslens.endpoint", "opslens_endpoint",
    "requester_ip_address", "user_agent", "requester_metadata",
    # Seen live on the success path under the proxy.
    "attempted_retries", "max_retries", "deployment_model_name",
    "attempted_fallbacks", "attempted_deployments", "litellm_model_name",
    "queue_time_seconds", "caching_groups", "model_group", "model_group_size",
    "deployment", "model_info", "api_base", "caching", "applied_guardrails",
    "requester_custom_headers", "user_api_key_hash", "user_api_key",
    "user_api_key_org_id", "user_api_key_team_max_budget",
    "user_api_key_team_spend", "user_api_key_spend", "user_api_key_max_budget",
    "user_api_key_metadata", "user_api_key_model_max_budget",
    "user_api_key_request_route", "spend_logs_metadata", "litellm_parent_otel_span",
    "usage_object", "cold_storage_object_key", "guardrails", "tags",
    "litellm_api_version", "global_max_parallel_requests", "mcp_tool_call_metadata",
    "vector_store_request_metadata", "raw_request_typed_dict",
))


def _get(o, k):
    """Read a field from an object or a dict, whichever LiteLLM handed us."""
    if o is None:
        return None
    return o.get(k) if isinstance(o, dict) else getattr(o, k, None)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _usage_tokens(usage) -> dict:
    """Pull the token counts out of whichever usage shape arrived.

    Audit T02: only the OpenAI spellings (`prompt_tokens` /
    `completion_tokens`) were read, so a direct Anthropic API call — which
    reports `input_tokens` / `output_tokens` — recorded ZERO tokens while still
    counting as a request. Cache WRITES (`cache_creation_input_tokens`) were not
    read at all, and OpenAI's cached-token count lives one level down in
    `prompt_tokens_details.cached_tokens`.
    """
    in_tok = _int(_get(usage, "prompt_tokens") or _get(usage, "input_tokens"))
    out_tok = _int(_get(usage, "completion_tokens")
                   or _get(usage, "output_tokens"))
    cache_read = _int(_get(usage, "cache_read_input_tokens")
                      or _get(usage, "cache_read_tokens"))
    if not cache_read:
        details = _get(usage, "prompt_tokens_details")
        cache_read = _int(_get(details, "cached_tokens"))
    cache_write = _int(_get(usage, "cache_creation_input_tokens")
                       or _get(usage, "cache_write_input_tokens"))
    if not cache_write:
        details = _get(usage, "cache_creation")
        cache_write = _int(_get(details, "ephemeral_5m_input_tokens")) + \
            _int(_get(details, "ephemeral_1h_input_tokens"))
    # The three prompt counters must stay DISJOINT, because the dashboard sums
    # them for the cached-share denominator.
    #
    # Native Anthropic `input_tokens` is already exclusive of both cache reads and
    # writes. But LiteLLM normalises every provider - including Bedrock Converse
    # and Anthropic - into an OpenAI-shaped `Usage` whose `prompt_tokens` is
    # INCLUSIVE of uncached input + cache reads + cache WRITES.
    #
    # Audit LP-02: only reads were being subtracted, so cache writes landed in
    # both `input_tokens` and `cache_write_tokens`. Executed against the real
    # `AmazonConverseConfig._transform_usage()`:
    #     provider  uncached=10, read=50, write=100  -> prompt_tokens=160
    #     emitted   input=110, read=50, write=100    -> 260 prompt tokens (+62.5%)
    # and a true 50/160 = 31.25% cached share read as 50/260 = 19.23%.
    if _get(usage, "prompt_tokens") is not None and _get(usage, "input_tokens") is None:
        in_tok = max(in_tok - cache_read - cache_write, 0)
    return {"input_tokens": in_tok, "output_tokens": out_tok,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write}


def _dimensions(kwargs, meta: dict) -> dict:
    """Attribution dimensions, from request metadata and LiteLLM key identity.

    Audit T02: identity was read from two places only, so requests authenticated
    with a LiteLLM virtual key but no explicit metadata carried no user at all
    and landed in __unattributed__.
    """
    dims: dict[str, str] = {}
    for k in ("workload", "team", "env", "environment", "business_unit",
              "cost_center", "project", "application"):
        if meta.get(k):
            dims["env" if k == "environment" else k] = str(meta[k])
    proxy_body = ((kwargs.get("proxy_server_request") or {}).get("body") or {}) \
        if isinstance(kwargs.get("proxy_server_request"), dict) else {}
    user = (meta.get("user")
            or meta.get("user_api_key_user_email")
            or meta.get("user_api_key_end_user_id")
            or meta.get("user_api_key_user_id")
            or kwargs.get("user")
            or proxy_body.get("user")
            or meta.get("user_api_key_alias"))
    if user:
        dims["user"] = str(user)
    team = (meta.get("team") or meta.get("user_api_key_team_alias")
            or meta.get("user_api_key_team_id"))
    if team:
        dims.setdefault("team", str(team))
    # Any other scalar metadata the caller sent is attribution the operator
    # chose to provide; keep it rather than discarding it. But skip what the
    # PROXY injects: verified live, it adds endpoint (its own URL),
    # requester_ip_address, user_agent and queue_time_seconds, which are not
    # attribution and would make every request its own dimension value.
    for k, v in meta.items():
        if (k in dims or k in _PROXY_META_NOISE
                or k.startswith(("user_api_key", "litellm", "_", "proxy_"))):
            continue
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            dims.setdefault(k, str(v))
    return dims


def _region_for(kwargs, meta: dict) -> str:
    """The model's own region when LiteLLM knows it, not just the bucket region.

    Audit T02: every event was stamped with OPS_LENS_REGION, so a fleet calling
    Bedrock in three regions appeared to be single-region and the per-region
    panels were wrong.
    """
    lp = kwargs.get("litellm_params") or {}
    return str(meta.get("region") or lp.get("aws_region_name")
               or lp.get("vertex_location") or _REGION)


def _ttft_ms(kwargs, start_time) -> float | None:
    """LiteLLM records when the first chunk arrived; that is the TTFT the
    dashboard's streaming panels need, and it was never captured."""
    first = kwargs.get("completion_start_time")
    if first is None or start_time is None:
        return None
    try:
        return max(0.0, (first - start_time).total_seconds() * 1000)
    except (TypeError, AttributeError):
        return None


def _build_event(kwargs, response, start_time, end_time, *,
                 status: int, throttled: bool) -> dict:
    meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    model = kwargs.get("model") or ""
    provider = (kwargs.get("litellm_params") or {}).get("custom_llm_provider") or ""
    endpoint = _endpoint_stamp(meta) or _endpoint_for(model, provider)
    usage = _get(response, "usage") or {}

    event = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dimensions": _dimensions(kwargs, meta),
        "model": model,
        "endpoint": endpoint,
        "region": _region_for(kwargs, meta),
        "status": status,
        "throttled": throttled,
    }
    event.update(_usage_tokens(usage))
    if start_time is not None and end_time is not None:
        try:
            event["latency_ms"] = max(
                0.0, (end_time - start_time).total_seconds() * 1000)
        except (TypeError, AttributeError):
            pass
    ttft = _ttft_ms(kwargs, start_time)
    if ttft is not None:
        event["ttft_ms"] = ttft
    account = meta.get("accountId") or meta.get("account_id")
    if account:
        event["accountId"] = str(account)
    # LiteLLM counts retries it performed itself; report it as a retry COUNT
    # (the ingester distinguishes that from an attempt ordinal). Under the proxy
    # the real count arrives as metadata["attempted_retries"], which is more
    # accurate than the configured num_retries ceiling.
    retries = meta.get("attempted_retries")
    if retries is None:
        retries = kwargs.get("num_retries") or meta.get("retry_attempts")
    if retries is not None:
        event["retry_attempts"] = _int(retries)
    cost = kwargs.get("response_cost")
    if cost:
        event["cost_usd_est"] = float(cost)
    # `request_id` must be unique PER ATTEMPT. The ingester deduplicates on
    # (event_date, request_id, ts), so emitting three attempts that all carry the
    # shared litellm_call_id would collapse them back into one row - defeating the
    # LP-01 fix at the next layer down.
    aid = attempt_id(kwargs, response)
    if aid:
        event["request_id"] = aid
        event["logical_request_id"] = logical_request_id(kwargs, response)
        event["attempt"] = _attempt_ordinal(kwargs)
    else:
        event["request_id"] = str(_get(response, "id") or uuid.uuid4())
    return event


# --------------------------------------------------------------------------- #
# Buffering and delivery
# --------------------------------------------------------------------------- #
def _upload(events: list[dict]) -> bool:
    """PUT one batch. Returns False when the batch should be re-queued.

    Runs OUTSIDE the buffer lock so request threads never wait on S3.
    """
    if not events or not _BUCKET:
        return True
    now = datetime.now(timezone.utc)
    key = (f"proxy-events/{_REGION}/{now:%Y/%m/%d/%H}/"
           f"litellm-{now:%M%S}-{uuid.uuid4().hex[:8]}.jsonl.gz")
    # Trailing newline: these objects are NDJSON in S3 and downstream readers
    # (Athena, Glue, or anything that concatenates keys) join them end to end.
    # Without it the last record of one object and the first of the next merge
    # into one unparseable line - which is exactly what happened the first time
    # the live test read the bucket back.
    body = gzip.compress(
        b"".join(json.dumps(e, separators=(",", ":")).encode() + b"\n"
                 for e in events))
    delay = 0.5
    for attempt in range(_MAX_UPLOAD_ATTEMPTS):
        try:
            _client().put_object(Bucket=_BUCKET, Key=key, Body=body)
            return True
        except Exception:  # noqa: BLE001 — telemetry must NEVER break inference
            if attempt == _MAX_UPLOAD_ATTEMPTS - 1 or _stopping.is_set():
                return False
            time.sleep(delay)
            delay *= 2
    return False


def _requeue(events: list[dict]) -> None:
    """Put a failed batch back at the FRONT of the buffer, dropping the oldest
    events only if the cap is exceeded — and counting any that are dropped."""
    global _dropped_events
    with _LOCK:
        _BUF[:0] = events
        overflow = len(_BUF) - _MAX_BUFFERED
        if overflow > 0:
            del _BUF[:overflow]
            _dropped_events += overflow


def _take_batch(force: bool = False) -> list[dict]:
    global _LAST_FLUSH
    with _LOCK:
        if not _BUF:
            return []
        due = (force or len(_BUF) >= _FLUSH_EVERY_N
               or time.time() - _LAST_FLUSH >= _FLUSH_EVERY_S)
        if not due:
            return []
        batch, _BUF[:] = list(_BUF), []
        _LAST_FLUSH = time.time()
        return batch


def flush(force: bool = True) -> None:
    """Send whatever is buffered. Safe to call from anywhere."""
    batch = _take_batch(force=force)
    if not batch:
        return
    if not _upload(batch):
        _requeue(batch)


def _timer_loop() -> None:
    """The single uploader worker.

    Handles BOTH triggers so no other thread ever performs I/O:
      * age  - it wakes on a timer, which is what audit T03 added (before that,
               the age was only checked while handling a NEW request, so a quiet
               period left the tail of the buffer unwritten);
      * size - `_submit` sets `_WAKE` instead of uploading inline (LP-03).
    """
    interval = max(1.0, min(_FLUSH_EVERY_S, 5.0))
    while not _stopping.is_set():
        woken = _WAKE.wait(interval)
        _WAKE.clear()
        try:
            # A size-triggered wake must flush regardless of the age.
            flush(force=woken)
        except Exception as e:  # noqa: BLE001
            _debug("uploader worker", e)


def _ensure_timer() -> None:
    global _timer
    if _timer is not None and _timer.is_alive():
        return
    with _LOCK:
        if _timer is not None and _timer.is_alive():
            return
        _timer = threading.Thread(target=_timer_loop, name="ops-lens-flush",
                                  daemon=True)
        _timer.start()


def _shutdown(*_args) -> None:
    """Flush on exit so a restart does not discard buffered telemetry."""
    _stopping.set()
    _WAKE.set()                 # release the worker from its wait
    try:
        # Drain on THIS thread: the worker may already have exited, and at
        # shutdown blocking is exactly what we want.
        flush(force=True)
    except Exception:  # noqa: BLE001
        pass
    if _dropped_events:
        print(f"[ops-lens] WARNING: dropped {_dropped_events} telemetry events "
              f"(buffer cap {_MAX_BUFFERED} reached while S3 was unreachable)")


atexit.register(_shutdown)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        _prev = signal.getsignal(_sig)

        def _handler(signum, frame, _prev=_prev):
            _shutdown()
            if callable(_prev):
                _prev(signum, frame)

        signal.signal(_sig, _handler)
    except (ValueError, OSError, RuntimeError):
        # Not the main thread (or a platform without the signal): atexit still
        # covers the ordinary shutdown path.
        pass


def _has_usage(resp) -> bool:
    """True when a response carries real token usage (i.e. it is the aggregate,
    not a partial stream chunk)."""
    u = _get(resp, "usage")
    if u is None:
        return False
    return bool(_int(_get(u, "prompt_tokens")) or _int(_get(u, "input_tokens"))
                or _int(_get(u, "completion_tokens"))
                or _int(_get(u, "output_tokens")))


# One request must yield one event. Under the proxy, BOTH the sync and the async
# CustomLogger hooks fire for the same call (verified live: one failed request
# produced two identical failure events). The ingester deduplicates on
# (event_date, request_id, ts), but the two firings can straddle a second
# boundary and then both persist - so suppress the duplicate here, at the source.
_SEEN_MAX = 4096
_seen_calls: dict[str, None] = {}


def logical_request_id(kwargs, resp=None) -> str:
    """The id of the CLIENT's request, stable across provider retries.

    The proxy assigns one `litellm_call_id` per incoming request and its Router
    keeps that id across retries and fallbacks, so this identifies the logical
    request - not the attempt.
    """
    return str(kwargs.get("litellm_call_id")
               or (kwargs.get("litellm_params") or {}).get("litellm_call_id")
               or _get(resp, "id") or "")


def _attempt_ordinal(kwargs) -> int:
    """Which provider attempt this notification is for. 0 = first try."""
    meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    for k in ("attempted_retries", "retry_count"):
        v = meta.get(k, kwargs.get(k))
        if v is not None:
            try:
                return max(int(v), 0)
            except (TypeError, ValueError):
                continue
    return 0


def _deployment_id(kwargs) -> str:
    """Which deployment served the attempt, so a FALLBACK to a different
    deployment is a distinct attempt even at the same retry ordinal."""
    lp = kwargs.get("litellm_params") or {}
    meta = lp.get("metadata") or {}
    return str(meta.get("model_info", {}).get("id")
               if isinstance(meta.get("model_info"), dict) else
               (meta.get("deployment") or lp.get("model")
                or kwargs.get("model") or ""))[:120]


def attempt_id(kwargs, resp=None) -> str:
    """Identity of one PROVIDER ATTEMPT.

    Audit LP-01: deduplicating on (outcome, litellm_call_id) collapsed distinct
    attempts. With a real Router doing `429 -> 429 -> 200`, all three callbacks
    carry the SAME call id, and the two 429s share the outcome "err", so the
    second throttle vanished: 3 attempts / 2 throttles (66.67%) was reported as
    2 attempts / 1 throttle (50%).

    The attempt ordinal (`attempted_retries`, which the Router increments) plus
    the serving deployment distinguishes a genuine second attempt from a
    duplicate sync/async notification OF that attempt - which is what the memo
    must actually suppress.
    """
    lid = logical_request_id(kwargs, resp)
    if not lid:
        return ""
    return f"{lid}#{_attempt_ordinal(kwargs)}@{_deployment_id(kwargs)}"


def _call_key(kwargs, resp, outcome: str) -> str:
    aid = attempt_id(kwargs, resp)
    return f"{outcome}:{aid}" if aid else ""


def _first_time(kwargs, resp, outcome: str) -> bool:
    """False when this (attempt, outcome) has already been recorded."""
    key = _call_key(kwargs, resp, outcome)
    if not key:
        return True            # no id to dedupe on; let the ingester arbitrate
    with _LOCK:
        if key in _seen_calls:
            return False
        _seen_calls[key] = None
        while len(_seen_calls) > _SEEN_MAX:
            _seen_calls.pop(next(iter(_seen_calls)))
    return True


def _debug(where: str, exc: BaseException) -> None:
    """Fail-open, but not fail-SILENT. Telemetry must never break inference, yet a
    handler that raises on every request while the proxy happily returns 200 is
    indistinguishable from "no traffic" - which is how the proxy integration was
    broken without anyone noticing. Set OPS_LENS_DEBUG=1 to surface it."""
    if os.environ.get("OPS_LENS_DEBUG"):
        import traceback
        print(f"[ops-lens] {where} raised: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def _submit(event: dict) -> None:
    """Enqueue only. NEVER uploads on the caller's thread.

    Audit LP-03: this used to call flush() - and therefore a synchronous
    boto3.put_object() plus time.sleep() backoff - whenever the buffer reached
    the size threshold. Under the proxy that ran on the event loop: with 199
    events buffered, one async hook and a 250 ms PUT delayed a 20 ms heartbeat
    to 256 ms, and three failed retries would add 3.5 s of sleep to the loop
    before any network time. Uploads now belong to the worker thread only.
    """
    global _dropped_events
    with _LOCK:
        _BUF.append(event)
        overflow = len(_BUF) - _MAX_BUFFERED
        if overflow > 0:
            del _BUF[:overflow]
            _dropped_events += overflow
        due = len(_BUF) >= _FLUSH_EVERY_N
    _ensure_timer()
    if due:
        _WAKE.set()          # hand the size-triggered flush to the worker


def ops_lens_handler(kwargs, completion_response, start_time, end_time):
    """LiteLLM success_callback signature. Metadata-only; fail-open.

    Found by an end-to-end run against real Bedrock, not by any unit test: with
    `stream=True` LiteLLM invokes the success callback ONCE PER CHUNK and then a
    final time with the aggregated response. A five-chunk stream therefore fired
    five times, so one request became five events - four of them carrying
    `in=0 out=0` because a partial chunk has no usage. Event grain
    One event per PROVIDER ATTEMPT, not per client request. A request the Router
    retries twice before succeeding emits three events: two throttles and one
    success. That matches how the dashboard decomposes attempts elsewhere
    (attempts = successes + non-throttle failures + throttles, mirroring
    CloudWatch), so a `429 -> 429 -> 200` sequence reads as a 66.67% throttle
    rate rather than 50%. `logical_request_id` groups the attempts of one client
    request, and `attempt` is the 0-based ordinal.

Streaming traffic was
    over-counted (10 events for 6 requests in the first live run) and the
    zero-token rows dragged every per-request average down.

    Only the final firing carries `complete_streaming_response`, so that is the
    one we record; the intermediate chunks are skipped.
    """
    try:
        if kwargs.get("stream"):
            final = kwargs.get("complete_streaming_response")
            if final is not None:
                # SDK path: the last of N firings carries the aggregate.
                completion_response = final
            elif not _has_usage(completion_response):
                # SDK path: an intermediate chunk. Skip it.
                return
            # PROXY path: exactly one firing, whose response_obj is ALREADY the
            # aggregated ModelResponse with usage but with no
            # `complete_streaming_response` in kwargs. Requiring that key dropped
            # every streamed request through the proxy - verified live.
        if not _first_time(kwargs, completion_response, "ok"):
            return
        _submit(_build_event(kwargs, completion_response, start_time, end_time,
                             status=200, throttled=False))
    except Exception as e:  # noqa: BLE001 — telemetry must NEVER break inference
        _debug("success handler", e)


def ops_lens_failure_handler(kwargs, exception_or_response, start_time, end_time):
    """LiteLLM failure_callback signature.

    Audit T02: without this, `status` was hardcoded to 200 and `throttled` to
    False, so the dashboard's error rate and throttle rate for proxy traffic were
    structurally zero — the exact signals an operator opens the tab for.
    """
    try:
        exc = kwargs.get("exception") or exception_or_response
        status = 0
        for attr in ("status_code", "http_status", "code"):
            v = _get(exc, attr)
            try:
                status = int(v)
                break
            except (TypeError, ValueError):
                continue
        name = f"{type(exc).__name__} {exc}".lower() if exc is not None else ""
        throttled = status == 429 or any(
            w in name for w in ("ratelimit", "rate limit", "throttl",
                                "too many requests"))
        if not status:
            status = 429 if throttled else 500
        if not _first_time(kwargs, exception_or_response, "err"):
            return
        _submit(_build_event(kwargs, None, start_time, end_time,
                             status=status, throttled=throttled))
    except Exception as e:  # noqa: BLE001
        _debug("failure handler", e)


# Back-compat: some configs register a single `callbacks:` entry. LiteLLM calls
# it for successes only, so keep the old name pointing at the success path.
ops_lens_callback = ops_lens_handler

# ---------------------------------------------------------------------------
# LiteLLM PROXY integration
# ---------------------------------------------------------------------------
# Verified against litellm 1.93.0 running as a proxy (`litellm --config
# config.yaml`): a plain function in `litellm_settings.success_callback` is
# REGISTERED ("Initialized Success Callbacks - [<function ops_lens_handler>]")
# and then never invoked for a successful request. Requests returned 200, the
# proxy logged them, and no event was ever emitted. `failure_callback` happened
# to fire, so the only telemetry reaching S3 was errors - a dashboard showing
# 100% error rate and zero traffic.
#
# The mechanism the proxy actually calls for both outcomes is a CustomLogger
# instance registered under `litellm_settings.callbacks`. The class below wraps
# the same event builder and buffer as the SDK path, so both on-ramps produce
# byte-identical events:
#
#   litellm_settings:
#     callbacks: ["litellm_callback.ops_lens_logger"]
#
# The plain functions above remain the supported path for the litellm PYTHON SDK
# (litellm.success_callback = [...]), which is where they are proven to work.
try:
    from litellm.integrations.custom_logger import CustomLogger as _CustomLogger
except Exception:  # noqa: BLE001 - litellm not installed (unit tests, docs build)
    _CustomLogger = object


class OpsLensLogger(_CustomLogger):
    """CustomLogger the LiteLLM proxy invokes for successes AND failures."""

    # -- success ----------------------------------------------------------
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        ops_lens_handler(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time,
                                     end_time):
        # Off the event loop entirely. Building the event is cheap, but it is
        # cheap only as long as nothing in the path can block; running it in a
        # worker thread means a future change cannot silently reintroduce LP-03.
        await asyncio.to_thread(ops_lens_handler, kwargs, response_obj,
                                start_time, end_time)

    # -- failure ----------------------------------------------------------
    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        ops_lens_failure_handler(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time,
                                      end_time):
        await asyncio.to_thread(ops_lens_failure_handler, kwargs, response_obj,
                                start_time, end_time)


# The module-level INSTANCE is what `callbacks: ["litellm_callback.ops_lens_logger"]`
# resolves to. A class reference would not work - the proxy expects an instance.
ops_lens_logger = OpsLensLogger()
