"""Normalize OpenTelemetry GenAI records into the proxy-event schema.

Audit T01: the documented OTEL on-ramp was a collector config whose
`transform/opslens` processor contained nothing but comments describing the
mapping. Copy it and the collector happily ships raw OTLP log records to S3, in
which case the ingester sees no `ts` and no `model` and drops every line - the
tab stays empty with no error anywhere. The mapping has to exist in code.

Doing it here rather than in OTTL means one implementation, tested, instead of
every adopter hand-writing a transform. A collector can therefore export
gen_ai-instrumented records with `marshaler: body` and no transform at all.

Handles three shapes, all of which arrive as one JSON object per line:

  1. An OTLP log record / span with `attributes` (either a flat dict, or the
     OTLP list-of-{key,value} form with typed AnyValue wrappers).
  2. Claude Code's `claude_code.api_request` event.
  3. Anything already in the proxy-event schema, which passes through untouched.

Attribute names follow the OpenTelemetry GenAI semantic conventions
(`gen_ai.*`), which are still Development-stability, so both the current and the
previously published spellings are accepted. Unknown extra attributes become
attribution dimensions rather than being discarded.
"""
from __future__ import annotations

import re

from typing import Any

# gen_ai.provider.name / gen_ai.system -> our endpoint enum.
_PROVIDER_ENDPOINT = {
    "aws.bedrock": "runtime",
    "aws_bedrock": "runtime",
    "bedrock": "runtime",
    "amazon.bedrock": "runtime",
    "anthropic": "anthropic-api",
    "openai": "openai-api",
    "azure.ai.openai": "openai-api",
    "azure_openai": "openai-api",
}

# Attribute keys that carry a value we map explicitly. Everything else that
# looks like an identity/organisational attribute becomes a dimension.
_MODEL_KEYS = ("gen_ai.request.model", "gen_ai.response.model", "model",
               "claude_code.model")
_IN_TOK_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
                "claude_code.tokens.input", "input_tokens")
_OUT_TOK_KEYS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens",
                 "claude_code.tokens.output", "output_tokens")
_CACHE_READ_KEYS = ("gen_ai.usage.cache_read_input_tokens",
                    "gen_ai.usage.cached_input_tokens",
                    "claude_code.tokens.cache_read", "cache_read_tokens")
_CACHE_WRITE_KEYS = ("gen_ai.usage.cache_creation_input_tokens",
                     "gen_ai.usage.cache_write_input_tokens",
                     "claude_code.tokens.cache_creation", "cache_write_tokens")
_LATENCY_KEYS = ("gen_ai.client.operation.duration", "duration_ms",
                 "claude_code.duration_ms", "latency_ms", "duration")
_TTFT_KEYS = ("gen_ai.server.time_to_first_token", "time_to_first_token_ms",
              "time_to_first_chunk_ms", "ttft_ms")
_COST_KEYS = ("claude_code.cost.usd", "gen_ai.usage.cost", "cost_usd_est",
              "cost_usd")
_STATUS_KEYS = ("http.response.status_code", "http.status_code", "status")
_ERROR_KEYS = ("error.type", "exception.type", "gen_ai.response.error")
_RETRY_KEYS = ("gen_ai.request.retry_count", "retry_attempts",
               "claude_code.retry_count")
_REGION_KEYS = ("cloud.region", "aws.region", "region")
_ACCOUNT_KEYS = ("cloud.account.id", "aws.account.id", "accountId",
                 "account_id")
_ID_KEYS = ("gen_ai.response.id", "request_id", "claude_code.request_id", "id")
_TS_KEYS = ("ts", "timestamp", "Timestamp", "time", "observedTimeUnixNano",
            "timeUnixNano")

# Identity / org attributes worth keeping as attribution dimensions.
_DIM_ALIASES = {
    "user.email": "user",
    "user.id": "user",
    "user.name": "user",
    "enduser.id": "user",
    "gen_ai.user.id": "user",
    "team": "team",
    "team.name": "team",
    "service.name": "workload",
    "workload": "workload",
    "deployment.environment": "env",
    "deployment.environment.name": "env",
    "env": "env",
    "business_unit": "business_unit",
    "cost_center": "cost_center",
    "organization": "business_unit",
}

# Anything under these prefixes is protocol noise, never a dimension.
# Record-shape keys that are never attribution dimensions.
_NOISE_KEYS = {"service.instance.id", "service.version", "service.namespace",
               "event.name", "event_name", "event.domain", "severity_text",
               "severityText", "severity_number", "severityNumber", "spanId",
               "traceId", "span_id", "trace_id", "flags", "dropped_attributes_count"}

_NOISE_PREFIXES = ("gen_ai.", "http.", "net.", "rpc.", "telemetry.", "otel.",
                   "process.", "host.", "container.", "k8s.", "cloud.",
                   "claude_code.", "exception.", "error.", "aws.", "db.",
                   "server.", "client.", "url.", "user_agent.", "thread.",
                   "code.", "session.")


def _anyvalue(v: Any) -> Any:
    """Unwrap an OTLP AnyValue ({"stringValue": "x"}) to a plain Python value."""
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "arrayValue", "kvlistValue",
              "bytesValue"):
        if k in v:
            return v[k]
    for k in ("intValue", "doubleValue"):
        if k in v:
            try:
                return float(v[k]) if k == "doubleValue" else int(v[k])
            except (TypeError, ValueError):
                return v[k]
    return v


def flatten_attributes(rec: dict) -> dict:
    """Collect attributes from every place OTLP puts them, into one flat dict.

    Accepts the flat form ({"attributes": {"k": v}}), the protobuf-JSON form
    ({"attributes": [{"key": k, "value": {"stringValue": v}}]}), and resource /
    scope attributes, which is where collector-level
    OTEL_RESOURCE_ATTRIBUTES (team, cost_center, ...) actually land.
    """
    out: dict = {}

    def absorb(attrs: Any) -> None:
        if isinstance(attrs, dict):
            for k, v in attrs.items():
                out[str(k)] = _anyvalue(v)
        elif isinstance(attrs, list):
            for item in attrs:
                if isinstance(item, dict) and "key" in item:
                    out[str(item["key"])] = _anyvalue(item.get("value"))

    # Resource first, so record-level attributes win on conflict.
    res = rec.get("resource") or {}
    if isinstance(res, dict):
        absorb(res.get("attributes"))
    absorb((rec.get("scope") or {}).get("attributes") if isinstance(rec.get("scope"), dict) else None)
    absorb(rec.get("resourceAttributes"))
    absorb(rec.get("attributes"))
    # `body` is where a structured log record's payload lives. It may be a plain
    # mapping OR an OTLP AnyValue wrapper - and absorbing the wrapper's own keys
    # leaked a literal `stringValue` dimension on real collector output.
    body = rec.get("body")
    if isinstance(body, dict):
        if set(body) & {"stringValue", "intValue", "doubleValue", "boolValue",
                        "arrayValue", "kvlistValue", "bytesValue"}:
            body = _anyvalue(body)
        else:
            absorb(body.get("attributes"))
            for k, v in body.items():
                if k != "attributes" and not isinstance(v, (dict, list)):
                    out.setdefault(str(k), _anyvalue(v))
    return out


def _first(attrs: dict, keys, default=None):
    for k in keys:
        if k in attrs and attrs[k] not in (None, ""):
            return attrs[k]
    return default


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _nanos(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def span_elapsed_ms(rec: dict, attrs: dict) -> float | None:
    """Elapsed wall time from a SPAN's start/end, in milliseconds.

    Round 3 (R3-01): a span carries its duration in `startTimeUnixNano` /
    `endTimeUnixNano` and usually no duration attribute at all, so latency has to
    be derived here or it is simply absent.
    """
    start = _nanos(rec.get("startTimeUnixNano") or attrs.get("startTimeUnixNano"))
    end = _nanos(rec.get("endTimeUnixNano") or attrs.get("endTimeUnixNano"))
    if start is None or end is None or end < start:
        return None
    return (end - start) / 1e6


def span_status(rec: dict) -> tuple[int | None, str | None]:
    """(http-ish status, error message) from an OTLP span Status.

    STATUS_CODE_ERROR is 2; 1 is OK and 0 is UNSET. A span that failed carries no
    HTTP status of its own, so an ERROR status has to become one or every span
    would be recorded as a success.
    """
    st = rec.get("status") or {}
    if not isinstance(st, dict):
        return None, None
    code = st.get("code")
    if isinstance(code, str):
        code = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1,
                "STATUS_CODE_ERROR": 2}.get(code.upper())
    msg = st.get("message") or None
    if code == 2:
        low = (msg or "").lower()
        # Real instrumentation puts the provider's HTTP status in the status
        # MESSAGE, not in an attribute: opentelemetry-instrumentation-openai-v2
        # 2.4b0 writes "Error code: 400 - {...}" and its `error.type` attribute is
        # dropped by the SDK (it passes a type object, not a string). Without
        # this, every failed span read as 500 and a client error was reported as a
        # server error - the two are split in the error panels.
        m = re.search(r"\b([45]\d\d)\b", msg or "")
        if m:
            st = int(m.group(1))
            return st, msg
        if any(w in low for w in ("throttl", "ratelimit", "rate limit",
                                  "too many requests")):
            return 429, msg
        return 500, (msg or "span status ERROR")
    return None, msg


def _ts_from(rec: dict, attrs: dict) -> str | None:
    """ISO-8601 UTC timestamp. OTLP uses nanoseconds since the epoch.

    Round 3 (R3-01): `startTimeUnixNano` was not consulted, so a standard OTLP
    SPAN had no timestamp and `normalize()` returned None - every inference span
    the collector delivered was silently dropped.
    """
    from datetime import datetime, timezone
    for k in ("timeUnixNano", "observedTimeUnixNano", "startTimeUnixNano"):
        raw = rec.get(k) or attrs.get(k)
        if raw:
            try:
                ns = int(raw)
            except (TypeError, ValueError):
                continue
            # Tolerate seconds / millis / micros / nanos.
            for scale in (1e9, 1e6, 1e3, 1.0):
                secs = ns / scale
                if 1_000_000_000 < secs < 4_000_000_000:   # ~2001..2096
                    # MICROSECOND precision, not whole seconds. Round 3 (R3-03):
                    # formatting with "%Y-%m-%dT%H:%M:%SZ" discarded the fraction,
                    # and since an OTLP log record need carry no response/span id,
                    # ten throttles 10 ms apart collapsed to ONE ingestion
                    # identity - a 90% undercount. Postgres TIMESTAMPTZ holds
                    # microseconds, so that is the precision we keep.
                    return datetime.fromtimestamp(secs, timezone.utc).isoformat(
                        timespec="microseconds").replace("+00:00", "Z")
    v = _first(rec, _TS_KEYS) or _first(attrs, _TS_KEYS)
    return str(v) if v else None


def looks_like_otel(rec: dict) -> bool:
    """True when this record needs normalizing rather than passing through."""
    if not isinstance(rec, dict):
        return False
    if rec.get("ts") and (rec.get("model") or rec.get("modelId")):
        return False        # already the proxy-event shape
    if any(k in rec for k in ("attributes", "resource", "resourceAttributes",
                              "timeUnixNano", "observedTimeUnixNano", "body",
                              # span fields (R3-01)
                              "startTimeUnixNano", "endTimeUnixNano", "spanId",
                              "traceId")):
        return True
    return False


def normalize(rec: dict) -> dict | None:
    """OTLP GenAI record -> proxy-event dict, or None when it is not an
    inference record (a tool-call span, a start event, ...)."""
    if not looks_like_otel(rec):
        return rec
    attrs = flatten_attributes(rec)
    model = _first(attrs, _MODEL_KEYS)
    if not model:
        # No model attribute: not an inference record. Dropping it is correct,
        # unlike dropping a real one for want of a mapping.
        return None
    ts = _ts_from(rec, attrs)
    if not ts:
        return None

    provider = str(_first(attrs, ("gen_ai.provider.name", "gen_ai.system",
                                  "cloud.provider"), "") or "").lower()
    endpoint = _PROVIDER_ENDPOINT.get(provider)
    if endpoint is None and not provider:
        # No provider attribute at all (common with hand-rolled instrumentation):
        # a Bedrock or OpenAI model id is recognisable on its own.
        mid = str(model).lower()
        if mid.startswith(("us.", "eu.", "apac.", "global.")) or (
                "." in mid and mid.split(".")[0] in
                ("anthropic", "amazon", "meta", "mistral", "cohere", "ai21",
                 "deepseek", "writer", "luma", "stability", "qwen")):
            endpoint = "runtime"
        elif "claude" in mid:
            endpoint = "anthropic-api"
        elif mid.startswith(("gpt", "o1", "o3", "o4")):
            endpoint = "openai-api"
    if endpoint is None:
        # A provider we do not recognise (Vertex, Cohere, Together, self-hosted).
        # Guessing "runtime" here would count non-AWS traffic as Bedrock traffic,
        # inflating every Bedrock panel and the AWS cost/quota attribution with
        # calls AWS never served (audit T02).
        endpoint = "unknown"
    # An explicit endpoint attribute always wins (this is how mantle is stamped,
    # since no provider attribute distinguishes it).
    explicit_ep = _first(attrs, ("opslens.endpoint", "endpoint"))
    if explicit_ep:
        endpoint = str(explicit_ep)

    status = _num(_first(attrs, _STATUS_KEYS), None)
    err = _first(attrs, _ERROR_KEYS)
    # A span's own Status is authoritative when no HTTP status attribute exists.
    span_st, span_msg = span_status(rec)
    if status is None and span_st is not None:
        status = span_st
        err = err or span_msg
    if status is None:
        # An error attribute with no status still has to count as a failure, or
        # the error rate silently reads zero.
        status = 429 if (err and "throttl" in str(err).lower()) else (
            500 if err else 200)
    status = int(status)
    throttled = status == 429 or bool(
        err and any(w in str(err).lower()
                    for w in ("throttl", "ratelimit", "rate_limit", "too many")))

    # gen_ai.client.operation.duration is SECONDS per the convention; the
    # *_ms spellings are milliseconds. Guessing wrong is a 1000x error.
    latency = None
    if attrs.get("gen_ai.client.operation.duration") is not None:
        latency = _num(attrs["gen_ai.client.operation.duration"])
        latency = latency * 1000.0 if latency is not None else None
    if latency is None:
        latency = _num(_first(attrs, ("duration_ms", "claude_code.duration_ms",
                                      "latency_ms")))
    if latency is None and attrs.get("duration") is not None:
        d = _num(attrs["duration"])
        latency = d * 1000.0 if d is not None and d < 600 else d
    if latency is None:
        # SPAN: derive elapsed time from start/end (R3-01).
        latency = span_elapsed_ms(rec, attrs)

    ttft = None
    if attrs.get("gen_ai.server.time_to_first_token") is not None:
        ttft = _num(attrs["gen_ai.server.time_to_first_token"])
        ttft = ttft * 1000.0 if ttft is not None else None
    if ttft is None:
        ttft = _num(_first(attrs, ("time_to_first_token_ms",
                                   "time_to_first_chunk_ms", "ttft_ms")))

    # Dimensions: known identity/org attributes, plus any non-namespaced custom
    # attribute (which is what a user setting OTEL_RESOURCE_ATTRIBUTES gets).
    dims: dict[str, str] = {}
    for k, v in attrs.items():
        if v in (None, "") or k in _NOISE_KEYS:
            continue
        alias = _DIM_ALIASES.get(k)
        if alias:
            dims.setdefault(alias, str(v))
            continue
        if k.startswith("opslens.dimensions."):
            dims[k.split(".", 2)[2]] = str(v)
            continue
        if "." not in k and not isinstance(v, (dict, list)):
            if k in {"model", "endpoint", "region", "status", "duration",
                     "input_tokens", "output_tokens", "cache_read_tokens",
                     "cache_write_tokens", "latency_ms", "ttft_ms", "ts",
                     "timestamp", "request_id", "id", "cost_usd",
                     "cost_usd_est", "retry_attempts", "throttled",
                     "accountId", "account_id"}:
                continue
            dims.setdefault(k, str(v))
            continue
        if not k.startswith(_NOISE_PREFIXES) and "." in k:
            dims.setdefault(k.replace(".", "_"), str(v))

    out = {
        "ts": ts,
        "dimensions": dims,
        "model": str(model),
        "endpoint": endpoint,
        "region": str(_first(attrs, _REGION_KEYS, "") or ""),
        "accountId": str(_first(attrs, _ACCOUNT_KEYS, "") or ""),
        "input_tokens": int(_num(_first(attrs, _IN_TOK_KEYS), 0) or 0),
        "output_tokens": int(_num(_first(attrs, _OUT_TOK_KEYS), 0) or 0),
        "cache_read_tokens": int(_num(_first(attrs, _CACHE_READ_KEYS), 0) or 0),
        "cache_write_tokens": int(_num(_first(attrs, _CACHE_WRITE_KEYS), 0) or 0),
        "status": status,
        "throttled": throttled,
    }
    if latency is not None:
        out["latency_ms"] = latency
    if ttft is not None:
        out["ttft_ms"] = ttft
    cost = _num(_first(attrs, _COST_KEYS))
    if cost is not None:
        out["cost_usd_est"] = cost
    retries = _first(attrs, _RETRY_KEYS)
    if retries is not None:
        out["retry_attempts"] = int(_num(retries, 0) or 0)
    rid = _first(attrs, _ID_KEYS) or rec.get("spanId") or rec.get("traceId")
    if rid:
        out["request_id"] = str(rid)
    # Trace identity is worth keeping: it is the only stable per-attempt id a
    # span-based emitter provides, and it makes an event traceable back to the
    # emitting system.
    if rec.get("traceId"):
        out["trace_id"] = str(rec["traceId"])
    if rec.get("spanId"):
        out["span_id"] = str(rec["spanId"])
    return out

# ---------------------------------------------------------------------------
# OTLP envelope expansion
# ---------------------------------------------------------------------------
# Found by running a REAL otel/opentelemetry-collector-contrib against a real
# bucket, which no unit test had done. Two surprises:
#
#   1. `marshaler: body` - which this repo's README used to recommend - writes
#      ONLY the log body. The 4,477-byte record above became the 23-byte string
#      "gen_ai.client.inference": every gen_ai attribute discarded. The correct
#      setting is `marshaler: otlp_json`.
#   2. `otlp_json` does not write one flat record per line. It writes the OTLP
#      ENVELOPE - {"resourceLogs":[{"resource":…,"scopeLogs":[{"logRecords":[…]}]}]}
#      - with resource attributes held ONCE per resourceLogs entry, several
#      records per envelope, and no trailing newline (so one S3 object can hold
#      several concatenated JSON documents).
#
# `normalize()` alone therefore saw an object with no `ts` and no `model` and
# dropped the whole batch silently. Expansion has to happen before normalizing,
# and each record has to inherit its resource/scope attributes - which is exactly
# where `team` / `cost_center` / `deployment.environment` live when an operator
# sets OTEL_RESOURCE_ATTRIBUTES.
_ENVELOPE_KEYS = ("resourceLogs", "resource_logs", "resourceSpans",
                  "resource_spans")


def is_envelope(obj) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _ENVELOPE_KEYS)


def _envelope_records(obj: dict):
    """Yield (record, resource_attrs, scope_attrs) for every record in an OTLP
    logs/traces envelope."""
    for env_key in _ENVELOPE_KEYS:
        for entry in (obj.get(env_key) or []):
            if not isinstance(entry, dict):
                continue
            res = (entry.get("resource") or {}).get("attributes")
            for scope_key in ("scopeLogs", "scope_logs", "scopeSpans",
                              "scope_spans"):
                for scope in (entry.get(scope_key) or []):
                    if not isinstance(scope, dict):
                        continue
                    scope_attrs = (scope.get("scope") or {}).get("attributes")
                    for rec_key in ("logRecords", "log_records", "spans"):
                        for rec in (scope.get(rec_key) or []):
                            if isinstance(rec, dict):
                                yield rec, res, scope_attrs


def expand(obj) -> list[dict]:
    """One S3 record -> zero or more proxy-event dicts.

    Handles the OTLP envelope, a bare OTLP record, and anything already in the
    proxy-event schema (returned unchanged). Records that are not inference
    records are dropped, not guessed at.
    """
    if is_envelope(obj):
        out: list[dict] = []
        for rec, res_attrs, scope_attrs in _envelope_records(obj):
            merged = dict(rec)
            # Attach the resource/scope attributes so flatten_attributes() sees
            # them; record-level attributes still win on conflict.
            if res_attrs is not None:
                merged["resource"] = {"attributes": res_attrs}
            if scope_attrs is not None:
                merged["scope"] = {"attributes": scope_attrs}
            ev = normalize(merged)
            if ev:
                out.append(ev)
        return out
    if looks_like_otel(obj):
        ev = normalize(obj)
        return [ev] if ev else []
    return [obj] if isinstance(obj, dict) else []
