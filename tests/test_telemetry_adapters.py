"""OTEL and LiteLLM telemetry adapters — Audit findings T01, T02, T03.

No database and no network: these drive the real normalizer and the real callback
with a stubbed uploader.

Run: .venv/bin/python -m pytest tests/test_telemetry_adapters.py -q
"""
from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/client-telemetry"))

from ingestion import otel_normalize as on  # noqa: E402
from ingestion import proxy_events as pe  # noqa: E402

_FIELDS = ("ts", "ev_date", "hr", "dims", "model", "endpoint", "region", "account",
           "in_tok", "out_tok", "cache_read", "cache_write", "status", "throttled",
           "latency_ms", "ttft_ms", "retries", "cost", "rid")

NANOS = "1789000000000000000"      # a fixed instant in 2026


def _attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}


def _otlp(attrs: dict, resource: dict | None = None) -> dict:
    rec = {"timeUnixNano": NANOS,
           "attributes": [_attr(k, v) for k, v in attrs.items()]}
    if resource:
        rec["resource"] = {"attributes": [_attr(k, v) for k, v in resource.items()]}
    return rec


def _parsed(rec):
    p = pe._parse_event(rec)
    return dict(zip(_FIELDS, p)) if p else None


# --------------------------------------------------------------------------- #
# T01 — OTLP records must actually be normalized, not dropped
# --------------------------------------------------------------------------- #
def test_a_gen_ai_record_is_ingested_not_dropped():
    """The documented collector transform was comments only, so a real OTEL
    on-ramp shipped raw OTLP: no `ts`, no `model`, every line silently dropped."""
    d = _parsed(_otlp({
        "gen_ai.provider.name": "aws.bedrock",
        "gen_ai.request.model": "us.anthropic.claude-sonnet-5",
        "gen_ai.usage.input_tokens": 4210,
        "gen_ai.usage.output_tokens": 318,
    }))
    assert d is not None, "the OTLP record was dropped"
    assert d["model"] == "us.anthropic.claude-sonnet-5"
    assert d["endpoint"] == "runtime"
    assert (d["in_tok"], d["out_tok"]) == (4210, 318)


def test_seconds_and_milliseconds_are_not_confused():
    """gen_ai.client.operation.duration is SECONDS per the convention; reading it
    as milliseconds is a 1000x error in the latency panels."""
    d = _parsed(_otlp({
        "gen_ai.request.model": "us.anthropic.claude-sonnet-5",
        "gen_ai.client.operation.duration": 2.4,
        "gen_ai.server.time_to_first_token": 0.42,
    }))
    assert d["latency_ms"] == pytest.approx(2400.0)
    assert d["ttft_ms"] == pytest.approx(420.0)


def test_millisecond_spellings_are_taken_as_milliseconds():
    d = _parsed(_otlp({"gen_ai.request.model": "m", "duration_ms": 1800,
                       "time_to_first_chunk_ms": 300}))
    assert d["latency_ms"] == pytest.approx(1800.0)
    assert d["ttft_ms"] == pytest.approx(300.0)


def test_all_three_prompt_token_counters_are_captured():
    d = _parsed(_otlp({
        "gen_ai.request.model": "m",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.cache_read_input_tokens": 3000,
        "gen_ai.usage.cache_creation_input_tokens": 500,
    }))
    assert (d["in_tok"], d["cache_read"], d["cache_write"]) == (1000, 3000, 500)


def test_resource_attributes_become_dimensions():
    """OTEL_RESOURCE_ATTRIBUTES lands in resource attributes, which is where a
    team / cost_center stamp actually arrives."""
    d = _parsed(_otlp(
        {"gen_ai.request.model": "m", "user.email": "jsmith@corp.com"},
        resource={"service.name": "search-svc", "team": "ml-platform",
                  "deployment.environment": "prod", "cost_center": "cc-123"}))
    assert d["dims"] == {"workload": "search-svc", "team": "ml-platform",
                         "env": "prod", "cost_center": "cc-123",
                         "user": "jsmith@corp.com"}


def test_protocol_noise_is_not_attribution():
    """Protocol/infra attributes must not become attribution dimensions. With no
    real attributes left, the ingester's documented fallback applies: the request
    is bucketed under workload=__unattributed__ so its tokens still count."""
    d = _parsed(_otlp({"gen_ai.request.model": "m", "http.method": "POST",
                       "telemetry.sdk.name": "opentelemetry", "k8s.pod.name": "p-1",
                       "event.name": "claude_code.api_request"}))
    assert d["dims"] == {"workload": "__unattributed__"}
    assert on.normalize(_otlp({"gen_ai.request.model": "m", "http.method": "POST",
                               "k8s.pod.name": "p-1"}))["dimensions"] == {}


def test_an_error_attribute_alone_still_counts_as_a_failure():
    """No status attribute plus an error type must not read as HTTP 200, or the
    error rate is structurally zero."""
    d = _parsed(_otlp({"gen_ai.request.model": "m",
                       "error.type": "ThrottlingException"}))
    assert d["status"] == 429
    assert d["throttled"] is True
    d2 = _parsed(_otlp({"gen_ai.request.model": "m", "error.type": "ValidationError"}))
    assert d2["status"] == 500 and d2["throttled"] is False


def test_non_inference_records_are_skipped_not_mangled():
    assert on.normalize({"timeUnixNano": NANOS,
                         "attributes": {"http.method": "GET"}}) is None


def test_proxy_shaped_events_pass_through_untouched():
    rec = {"ts": "2026-09-08T10:00:00Z", "model": "m", "endpoint": "mantle",
           "region": "us-east-1", "input_tokens": 1, "output_tokens": 2,
           "request_id": "r"}
    assert on.normalize(rec) is rec
    d = _parsed(rec)
    assert d["endpoint"] == "mantle" and d["in_tok"] == 1


def test_mantle_can_be_stamped_explicitly():
    """No provider attribute distinguishes mantle, so an explicit stamp must win."""
    d = _parsed(_otlp({"gen_ai.provider.name": "aws.bedrock",
                       "gen_ai.request.model": "anthropic.claude-opus-4-8",
                       "opslens.endpoint": "mantle"}))
    assert d["endpoint"] == "mantle"


def test_claude_code_event_shape():
    d = _parsed({"ts": "2026-09-08T10:00:00Z", "attributes": {
        "event.name": "claude_code.api_request",
        "claude_code.model": "claude-sonnet-5",
        "claude_code.tokens.input": 900, "claude_code.tokens.output": 120,
        "claude_code.tokens.cache_read": 800, "claude_code.duration_ms": 1800,
        "claude_code.cost.usd": 0.0041, "user.email": "dev@corp.com"}})
    assert d["endpoint"] == "anthropic-api"
    assert (d["in_tok"], d["out_tok"], d["cache_read"]) == (900, 120, 800)
    assert d["cost"] == pytest.approx(0.0041)
    assert d["dims"] == {"user": "dev@corp.com"}


def test_unknown_provider_is_not_relabelled_as_bedrock():
    """Counting a Vertex or Cohere call as bedrock-runtime inflates every Bedrock
    panel with traffic AWS never served."""
    d = _parsed(_otlp({"gen_ai.provider.name": "gcp.vertex_ai",
                       "gen_ai.request.model": "gemini-2.5-pro"}))
    assert d["endpoint"] == "unknown"


def test_documented_mapping_is_not_just_a_comment():
    """T01's root cause: the README told operators to write a collector transform
    and gave them a body of comments. The mapping must live in code."""
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "transform/opslens" not in src, "the empty transform sketch is back"
    assert "otel_normalize" in src, "the docs must point at the real normalizer"


# --------------------------------------------------------------------------- #
# T02 — the LiteLLM adapter must not lose usage, identity, provider or region
# --------------------------------------------------------------------------- #
@pytest.fixture()
def lc():
    import litellm_callback as mod
    mod._BUF.clear()
    mod._dropped_events = 0
    mod._uploaded = []
    mod._upload = lambda events: (mod._uploaded.append(list(events)) or True)
    return mod


def _times():
    now = dt.datetime.now(dt.timezone.utc)
    return now - dt.timedelta(seconds=2), now, now - dt.timedelta(seconds=1.6)


def test_anthropic_usage_shape_is_read(lc):
    """Only the OpenAI spellings were read, so a direct Anthropic call recorded
    ZERO tokens while still counting as a request."""
    start, end, first = _times()
    kwargs = {"model": "claude-sonnet-5",
              "litellm_params": {"custom_llm_provider": "anthropic", "metadata": {}},
              "completion_start_time": first}
    resp = types.SimpleNamespace(id="msg_1", usage={
        "input_tokens": 800, "output_tokens": 120,
        "cache_read_input_tokens": 700, "cache_creation_input_tokens": 90})
    e = lc._build_event(kwargs, resp, start, end, status=200, throttled=False)
    assert (e["input_tokens"], e["output_tokens"]) == (800, 120)
    assert (e["cache_read_tokens"], e["cache_write_tokens"]) == (700, 90)


def test_openai_cached_tokens_are_nested_and_kept_disjoint(lc):
    start, end, _ = _times()
    kwargs = {"model": "gpt-5.2-mini",
              "litellm_params": {"custom_llm_provider": "openai", "metadata": {}}}
    resp = types.SimpleNamespace(id="c1", usage={
        "prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 400}})
    e = lc._build_event(kwargs, resp, start, end, status=200, throttled=False)
    # OpenAI's prompt_tokens INCLUDES cached tokens; the event's counters must not.
    assert e["input_tokens"] == 600
    assert e["cache_read_tokens"] == 400


def test_ttft_is_captured_from_the_first_chunk(lc):
    start, end, first = _times()
    e = lc._build_event({"model": "m", "litellm_params": {"metadata": {}},
                         "completion_start_time": first},
                        types.SimpleNamespace(id="x", usage={}), start, end,
                        status=200, throttled=False)
    assert round(e["ttft_ms"]) == 400


def test_virtual_key_identity_is_picked_up(lc):
    start, end, _ = _times()
    e = lc._build_event({"model": "m", "litellm_params": {"metadata": {
        "user_api_key_user_email": "a@corp.com",
        "user_api_key_team_alias": "ml-platform"}}},
        types.SimpleNamespace(id="x", usage={}), start, end,
        status=200, throttled=False)
    assert e["dimensions"]["user"] == "a@corp.com"
    assert e["dimensions"]["team"] == "ml-platform"


def test_region_is_the_models_region_not_the_bucket_region(lc):
    start, end, _ = _times()
    e = lc._build_event({"model": "m", "litellm_params": {
        "custom_llm_provider": "bedrock", "aws_region_name": "eu-west-1",
        "metadata": {}}}, types.SimpleNamespace(id="x", usage={}), start, end,
        status=200, throttled=False)
    assert e["region"] == "eu-west-1"


@pytest.mark.parametrize("provider,model,expected", [
    ("bedrock", "us.anthropic.claude-sonnet-5", "runtime"),
    ("anthropic", "claude-sonnet-5", "anthropic-api"),
    ("openai", "gpt-5.2", "openai-api"),
    ("azure", "gpt-5.2", "openai-api"),
    ("vertex_ai", "gemini-2.5", "unknown"),
    ("cohere", "command-r", "unknown"),
    ("", "us.anthropic.claude-sonnet-5", "runtime"),
    ("", "gpt-5.2", "openai-api"),
])
def test_provider_mapping_never_guesses_bedrock(lc, provider, model, expected):
    assert lc._endpoint_for(model, provider) == expected


def test_failures_emit_an_event_with_a_real_status(lc):
    """Without a failure callback, status was hardcoded 200 and throttled False,
    so proxy error and throttle rates were structurally zero."""
    start, end, _ = _times()

    class Thr(Exception):
        status_code = 429

    lc.ops_lens_failure_handler(
        {"model": "m", "litellm_params": {"metadata": {}},
         "exception": Thr("rate limit exceeded")}, None, start, end)
    assert lc._BUF, "a failed request produced no event"
    ev = lc._BUF[-1]
    assert ev["status"] == 429 and ev["throttled"] is True


def test_failure_without_a_status_code_is_still_a_failure(lc):
    start, end, _ = _times()
    lc.ops_lens_failure_handler(
        {"model": "m", "litellm_params": {"metadata": {}},
         "exception": ValueError("bad request")}, None, start, end)
    assert lc._BUF[-1]["status"] == 500
    assert lc._BUF[-1]["throttled"] is False


def test_both_callbacks_are_documented(lc):
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "failure_callback" in src, (
        "a success-only registration hides every error and throttle")


# --------------------------------------------------------------------------- #
# T03 — buffered telemetry must be delivered, and never silently discarded
# --------------------------------------------------------------------------- #
def test_a_failed_upload_requeues_the_batch(lc):
    lc._upload = lambda events: False
    for i in range(3):
        lc._submit({"n": i})
    lc.flush(force=True)
    assert len(lc._BUF) == 3, "a failed upload discarded the batch"


def test_the_buffer_is_bounded_and_counts_drops(lc):
    lc._upload = lambda events: False
    lc._MAX_BUFFERED = 5
    try:
        for i in range(10):
            lc._submit({"n": i})
        assert len(lc._BUF) <= 5
        assert lc._dropped_events > 0, "dropped events must be counted"
    finally:
        lc._MAX_BUFFERED = 20000


def test_shutdown_flushes_the_tail(lc):
    """The age check used to run only while handling a NEW request, so the tail of
    a burst sat in memory until the process died."""
    lc._submit({"n": "tail"})
    lc._shutdown()
    assert lc._uploaded and lc._uploaded[0][0]["n"] == "tail"
    lc._stopping.clear()


def test_a_background_flusher_exists(lc):
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    assert "_timer_loop" in src and "threading.Thread" in src
    assert "atexit.register" in src
    assert "signal.SIGTERM" in src


def test_uploads_happen_outside_the_lock(lc):
    """Holding the buffer lock across the S3 PUT made every request wait on
    network I/O during a flush."""
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    upload_body = src[src.index("def _upload("):src.index("def _requeue(")]
    assert "_LOCK" not in upload_body


def test_events_carry_a_per_attempt_idempotency_key(lc):
    """Audit LP-01: `request_id` identifies one provider ATTEMPT, because the
    ingester deduplicates on it - three retry attempts sharing the logical
    litellm_call_id would otherwise collapse into one row. The logical parent is
    kept alongside so the attempts of one client request can be grouped."""
    start, end, _ = _times()
    e = lc._build_event({"model": "m",
                         "litellm_params": {"metadata": {"attempted_retries": 2}},
                         "litellm_call_id": "call-123"},
                        types.SimpleNamespace(usage={}), start, end,
                        status=200, throttled=False)
    assert e["request_id"].startswith("call-123#2@")
    assert e["logical_request_id"] == "call-123"
    assert e["attempt"] == 2
