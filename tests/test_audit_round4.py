"""Audit round 3 + LiteLLM-proxy follow-up — R3-01..R3-04 and LP-01..LP-04.

Every case here is the reviewer's own executed counterexample, several of them
taken from the live AWS catalog. Each asserts the corrected value AND, where the
old behaviour was a plausible-looking number, that the wrong number is gone.

Run: .venv/bin/python -m pytest tests/test_audit_round4.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools/client-telemetry"))

from app.model_identity import identity, match_rank  # noqa: E402
from app.quota_match import resolve_quota  # noqa: E402
from ingestion import otel_normalize as on  # noqa: E402
from ingestion import proxy_events as pe  # noqa: E402

ACCT, REGION = "111111111111", "us-east-1"


def q(name, limit, traffic="On-demand", code="L-x", acct=ACCT, region=REGION):
    return {"accountId": acct, "region": region, "model_name": name,
            "traffic_type": traffic, "metric": "TPM", "applied_value": limit,
            "default_value": limit, "quota_code": code}


# =========================================================================== #
# R3-02 — model identity: real SKUs from the live AWS catalog
# =========================================================================== #
def test_mistral_2402_does_not_borrow_the_2407_limit():
    """Both are ACTIVE with separate quotas. Treating any >2-digit number as a
    build number collapsed them, so 80,000 TPM of 24.02 against its own 50K limit
    reported 8% instead of 160%."""
    rows = [q("Mistral Large 2407", 1_000_000, code="L-1C28E1AB"),
            q("Mistral AI Mistral Large", 50_000, code="L-01447289")]
    res = resolve_quota(rows, ACCT, REGION, "mistral.mistral-large-2402-v1:0",
                        "TPM", "On-demand")
    assert res.value == 50_000, "the 2402 model must not take the 2407 limit"
    assert 80_000 / res.value * 100 == pytest.approx(160.0)


def test_the_version_specific_quota_name_wins_for_its_own_model():
    rows = [q("Mistral Large 2407", 1_000_000, code="L-1C28E1AB"),
            q("Mistral AI Mistral Large", 50_000, code="L-01447289")]
    res = resolve_quota(rows, ACCT, REGION, "mistral.mistral-large-2407-v1:0",
                        "TPM", "On-demand")
    assert res.value == 1_000_000


def test_cohere_embed_v4_still_resolves():
    """`-v4:0` is the model GENERATION here, not a disposable transport revision;
    stripping it lost a real 300K limit."""
    rows = [q("Cohere Embed V4", 300_000, "Cross-region", code="L-4C3F0FE6")]
    res = resolve_quota(rows, ACCT, REGION, "us.cohere.embed-v4:0", "TPM",
                        "Cross-region")
    assert res.value == 300_000


def test_colliding_same_rank_quotas_report_ambiguous_not_the_larger():
    """Keeping the larger of two matching quotas is exactly how the Mistral error
    produced a confident wrong number."""
    rows = [q("Mistral Large 2407", 1_000_000, code="L-a"),
            q("Mistral Large 2407", 50_000, code="L-b")]
    res = resolve_quota(rows, ACCT, REGION, "mistral.mistral-large-2407-v1:0",
                        "TPM", "On-demand")
    assert res.value is None
    assert res.ambiguous is True


@pytest.mark.parametrize("name,model_id,rank", [
    ("Mistral Large 2407", "mistral.mistral-large-2402-v1:0", 0),
    ("Mistral Large 2407", "mistral.mistral-large-2407-v1:0", 2),
    ("Mistral AI Mistral Large", "mistral.mistral-large-2402-v1:0", 1),
    ("Cohere Embed V4", "cohere.embed-v4:0", 2),
    ("Cohere Embed V4", "cohere.embed-english-v3:0", 0),
    ("Titan Text Embeddings V2", "amazon.titan-embed-text-v2:0", 2),
    # the round-2 Claude boundaries must survive the round-3 rewrite
    ("Claude Sonnet 4.5", "anthropic.claude-sonnet-4-5-20250929-v1:0", 2),
    ("Claude Sonnet 4", "anthropic.claude-sonnet-4-5-20250929-v1:0", 0),
    ("Claude Haiku 4.5", "anthropic.claude-sonnet-4-5-20250929-v1:0", 0),
    # a revision-specific quota name must not match the other snapshot
    ("Claude 3.5 Sonnet V2", "anthropic.claude-3-5-sonnet-20241022-v2:0", 2),
    ("Claude 3.5 Sonnet V2", "anthropic.claude-3-5-sonnet-20240620-v1:0", 0),
])
def test_match_ranks(name, model_id, rank):
    assert match_rank(name, model_id) == rank


def test_identity_keeps_semantic_versions_and_separates_revisions():
    assert identity("mistral.mistral-large-2402-v1:0") == (("large",), (2402,), (1,))
    assert identity("cohere.embed-v4:0") == (("embed",), (4,), ())
    assert identity("anthropic.claude-sonnet-4-5-20250929-v1:0") == \
        (("claude", "sonnet"), (4, 5), (1,))


# =========================================================================== #
# R3-01 — standard OTLP spans
# =========================================================================== #
def _span(status_code=1, msg=None, attrs=None, start=1789056386000000000,
          end=1789056387516400000):
    a = attrs if attrs is not None else [
        {"key": "gen_ai.provider.name", "value": {"stringValue": "aws.bedrock"}},
        {"key": "gen_ai.request.model",
         "value": {"stringValue": "us.anthropic.claude-sonnet-5"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "4210"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "318"}},
    ]
    st = {"code": status_code}
    if msg:
        st["message"] = msg
    return {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "svc"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": "5b8aa5a2d2c872e8321cf37308d69df2",
            "spanId": "051581bf3cb55c13", "name": "chat", "kind": 3,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
            "attributes": a, "status": st}]}]}]}


def test_a_standard_inference_span_is_ingested():
    """A span has startTimeUnixNano/endTimeUnixNano and no log timestamp, so
    `_ts_from` returned None and normalize() dropped it: one valid span produced
    ZERO events."""
    out = on.expand(_span())
    assert len(out) == 1
    e = out[0]
    assert e["input_tokens"] == 4210 and e["output_tokens"] == 318
    assert e["endpoint"] == "runtime"


def test_span_latency_comes_from_start_and_end():
    e = on.expand(_span())[0]
    assert e["latency_ms"] == pytest.approx(1516.4, abs=0.01)


def test_span_identity_is_preserved():
    e = on.expand(_span())[0]
    assert e["trace_id"] == "5b8aa5a2d2c872e8321cf37308d69df2"
    assert e["span_id"] == "051581bf3cb55c13"
    assert e["request_id"]


def test_an_error_span_is_not_recorded_as_a_success():
    e = on.expand(_span(2, "ValidationException"))[0]
    assert e["status"] == 500 and e["throttled"] is False


def test_a_throttling_span_reads_429():
    e = on.expand(_span(2, "ThrottlingException: rate exceeded"))[0]
    assert e["status"] == 429 and e["throttled"] is True


def test_a_non_inference_span_is_skipped():
    ns = _span(attrs=[{"key": "http.request.method",
                       "value": {"stringValue": "GET"}}])
    assert on.expand(ns) == []


def test_the_collector_example_exports_traces_too():
    """An SDK emitting spans needs a traces pipeline or nothing leaves the
    collector, however well the ingester handles spans."""
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "traces:" in src and "receivers: [otlp]" in src


# =========================================================================== #
# R3-03 — subsecond precision and stable identity
# =========================================================================== #
def _log(ns, acct=None, rid=None):
    attrs = [{"key": "gen_ai.request.model",
              "value": {"stringValue": "us.anthropic.claude-sonnet-5"}},
             {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "0"}},
             {"key": "error.type", "value": {"stringValue": "ThrottlingException"}}]
    if acct:
        attrs.append({"key": "cloud.account.id", "value": {"stringValue": acct}})
    if rid:
        attrs.append({"key": "gen_ai.response.id", "value": {"stringValue": rid}})
    return {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [
        {"logRecords": [{"observedTimeUnixNano": str(ns),
                         "body": {"stringValue": "x"}, "attributes": attrs}]}]}]}


def _identity_of(env):
    p = pe._parse_event(on.expand(env)[0])
    return (p[1], p[18], p[0])          # (event_date, request_id, ts)


BASE_NS = 1789056386000000000


def test_ten_same_second_throttles_stay_ten():
    """Whole-second formatting plus a dimensions-derived fallback id made ten
    distinct throttles 10 ms apart ONE ingestion identity: a 90% undercount."""
    ids = {_identity_of(_log(BASE_NS + i * 10_000_000)) for i in range(10)}
    assert len(ids) == 10


def test_replaying_those_same_events_adds_nothing():
    """The fallback id must stay DERIVED, or redelivery would stop deduplicating."""
    first = {_identity_of(_log(BASE_NS + i * 10_000_000)) for i in range(10)}
    again = {_identity_of(_log(BASE_NS + i * 10_000_000)) for i in range(10)}
    assert len(first | again) == 10


def test_two_accounts_with_identical_metadata_stay_distinct():
    a = _identity_of(_log(BASE_NS, acct="111111111111"))
    b = _identity_of(_log(BASE_NS, acct="222222222222"))
    assert a != b


def test_the_timestamp_keeps_microseconds():
    e = on.expand(_log(BASE_NS + 123_456_000))[0]
    assert e["ts"] == "2026-09-10T16:06:26.123456Z"


def test_a_provider_response_id_still_wins():
    ids = {_identity_of(_log(BASE_NS, rid=f"resp-{i}")) for i in range(10)}
    assert len(ids) == 10


# =========================================================================== #
# R3-04 — corrupt concatenated JSON
# =========================================================================== #
def _obj(*models):
    return "".join(json.dumps({"resourceLogs": [{
        "resource": {"attributes": []}, "scopeLogs": [{"logRecords": [{
            "observedTimeUnixNano": "1789056386000000000",
            "body": {"stringValue": "x"},
            "attributes": [
                {"key": "gen_ai.request.model", "value": {"stringValue": m}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "5"}},
            ]}]}]}]}) for m in models)


class _S3:
    def __init__(self, blob):
        self.blob = blob

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": staticmethod(lambda: self.blob)})()}


def test_a_corrupt_middle_document_does_not_lose_the_valid_suffix():
    """Recovery searched only for a newline, and otlp_json output has none - so
    everything after the corruption was discarded silently."""
    pe._OBJ_ERRORS.clear()
    blob = (_obj("model-one") + '{ "corrupt": !!! }' + _obj("model-two")).encode()
    evs = list(pe._read_event_lines(_S3(blob), "b", "logs.json"))
    assert [e["model"] for e in evs] == ["model-one", "model-two"]


def test_a_decode_error_is_recorded_so_the_object_is_not_marked_done():
    pe._OBJ_ERRORS.clear()
    blob = (_obj("m1") + '{ bad }' + _obj("m2")).encode()
    list(pe._read_event_lines(_S3(blob), "b", "bad.json"))
    assert pe._OBJ_ERRORS.get("bad.json"), "a partial read must be reported"


def test_a_clean_object_records_no_error():
    pe._OBJ_ERRORS.clear()
    list(pe._read_event_lines(_S3(_obj("a", "b").encode()), "b", "clean.json"))
    assert not pe._OBJ_ERRORS.get("clean.json")


def test_the_ingester_refuses_to_mark_a_partial_object_processed():
    src = (ROOT / "ingestion/proxy_events.py").read_text()
    assert "_OBJ_ERRORS" in src
    assert "bad_objects.append" in src
    assert "were NOT marked processed" in src


# =========================================================================== #
# LP-01 — retry attempts must not collapse
# =========================================================================== #
@pytest.fixture()
def cb(monkeypatch):
    import litellm_callback as lc
    sent: list[dict] = []
    monkeypatch.setattr(lc, "_submit", lambda ev: sent.append(ev))
    lc._seen_calls.clear()
    lc._sent = sent
    return lc


class _Throttled(Exception):
    status_code = 429


def _kw(retries, call_id="req-A", model="claude-sonnet", **extra):
    k = {"model": model, "litellm_call_id": call_id,
         "litellm_params": {"custom_llm_provider": "bedrock",
                            "metadata": {"workload": "w",
                                         "attempted_retries": retries}}}
    k.update(extra)
    return k


class _Ok:
    id = "resp-A"
    usage = {"prompt_tokens": 14, "completion_tokens": 4}


def test_429_429_200_emits_three_attempts(cb):
    """The Router keeps ONE litellm_call_id across retries, so a memo keyed on
    (outcome, call_id) dropped the second throttle: 3 attempts / 2 throttles
    (66.67%) was reported as 2 / 1 (50%)."""
    cb.ops_lens_logger.log_failure_event(
        dict(_kw(0), exception=_Throttled("t")), None, None, None)
    cb.ops_lens_logger.log_failure_event(
        dict(_kw(1), exception=_Throttled("t")), None, None, None)
    cb.ops_lens_logger.log_success_event(_kw(2), _Ok(), None, None)
    sent = cb._sent
    assert len(sent) == 3
    throttles = sum(1 for e in sent if e["throttled"])
    assert throttles == 2
    assert throttles / len(sent) == pytest.approx(2 / 3)


def test_each_attempt_gets_a_distinct_request_id(cb):
    """Otherwise the ingester's (event_date, request_id, ts) dedup collapses them
    again one layer down."""
    for i in range(3):
        cb.ops_lens_logger.log_success_event(_kw(i), _Ok(), None, None)
    assert len({e["request_id"] for e in cb._sent}) == 3


def test_the_logical_request_is_preserved_across_attempts(cb):
    for i in range(3):
        cb.ops_lens_logger.log_success_event(_kw(i), _Ok(), None, None)
    assert {e["logical_request_id"] for e in cb._sent} == {"req-A"}
    assert [e["attempt"] for e in cb._sent] == [0, 1, 2]


def test_duplicate_notification_of_one_attempt_still_collapses(cb):
    k = dict(_kw(1), exception=_Throttled("t"))
    cb.ops_lens_logger.log_failure_event(k, None, None, None)
    asyncio.run(cb.ops_lens_logger.async_log_failure_event(k, None, None, None))
    assert len(cb._sent) == 1


def test_all_attempts_failing_emits_every_failure(cb):
    for i in range(3):
        cb.ops_lens_logger.log_failure_event(
            dict(_kw(i), exception=_Throttled("t")), None, None, None)
    assert len(cb._sent) == 3
    assert all(e["throttled"] for e in cb._sent)


def test_a_fallback_to_another_deployment_is_a_distinct_attempt(cb):
    cb.ops_lens_logger.log_failure_event(
        dict(_kw(0, model="claude-sonnet"), exception=_Throttled("t")),
        None, None, None)
    cb.ops_lens_logger.log_success_event(_kw(0, model="claude-haiku"), _Ok(),
                                        None, None)
    assert len(cb._sent) == 2


# =========================================================================== #
# LP-02 — LiteLLM's inclusive prompt_tokens
# =========================================================================== #
def _converse_usage(uncached, out, read, write):
    from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig
    return AmazonConverseConfig()._transform_usage({
        "inputTokens": uncached, "outputTokens": out,
        "totalTokens": uncached + out,
        "cacheReadInputTokens": read, "cacheWriteInputTokens": write})


@pytest.mark.parametrize("uncached,read,write", [
    (10, 50, 100),    # mixed - the reviewer's case
    (10, 50, 0),      # read only
    (10, 0, 100),     # write only
    (10, 0, 0),       # no cache
])
def test_prompt_tokens_are_conserved_and_disjoint(cb, uncached, read, write):
    """LiteLLM's prompt_tokens is INCLUSIVE of reads AND writes. Subtracting only
    reads put cache writes in both `input_tokens` and `cache_write_tokens`:
    160 prompt tokens were emitted as 260 (+62.5%), turning a true 31.25% cached
    share into 19.23%."""
    u = _converse_usage(uncached, 7, read, write)
    got = cb._usage_tokens(u)
    total = (got["input_tokens"] + got["cache_read_tokens"]
             + got["cache_write_tokens"])
    assert total == u.prompt_tokens
    assert got["input_tokens"] == uncached
    assert got["cache_read_tokens"] == read
    assert got["cache_write_tokens"] == write


def test_the_reviewers_exact_numbers(cb):
    u = _converse_usage(10, 7, 50, 100)
    got = cb._usage_tokens(u)
    assert u.prompt_tokens == 160
    assert got["input_tokens"] == 10, "110 was the bug"
    total = sum(got[k] for k in ("input_tokens", "cache_read_tokens",
                                 "cache_write_tokens"))
    assert total == 160 and total != 260


def test_native_anthropic_exclusive_input_is_left_alone(cb):
    got = cb._usage_tokens({"input_tokens": 10, "output_tokens": 7,
                            "cache_read_input_tokens": 50,
                            "cache_creation_input_tokens": 100})
    assert got["input_tokens"] == 10


# =========================================================================== #
# LP-03 — the async hooks must not block the event loop
# =========================================================================== #
def test_the_async_hook_does_not_block_the_loop(monkeypatch):
    """At the size threshold the hook used to run a synchronous PUT on the event
    loop: a 250 ms upload delayed a 20 ms heartbeat to 256 ms."""
    import litellm_callback as lc
    lc._BUF.clear()
    lc._seen_calls.clear()
    lc._BUCKET = "b"
    monkeypatch.setattr(lc, "_upload", lambda evs: (time.sleep(0.25) or True))
    for i in range(lc._FLUSH_EVERY_N - 1):
        lc._BUF.append({"n": i})

    async def run():
        late = []

        async def heartbeat():
            t0 = time.perf_counter()
            await asyncio.sleep(0.02)
            late.append((time.perf_counter() - t0) * 1000)

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)          # let the heartbeat start its timer
        await lc.ops_lens_logger.async_log_success_event(
            _kw(0), _Ok(), None, None)
        await task
        return late[0]

    try:
        delay = asyncio.run(run())
        assert delay < 150, f"event loop blocked for {delay:.0f}ms"
    finally:
        lc._stopping.set()
        lc._WAKE.set()
        lc._stopping.clear()


def test_submission_never_uploads_inline():
    """Inspect CODE lines only - the docstring quotes the old behaviour."""
    import ast
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_submit")
    called = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "flush" not in called, f"_submit must enqueue only; it calls {called}"
    assert "_WAKE.set" in called


def test_the_worker_handles_both_triggers():
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    loop = src[src.index("def _timer_loop("):src.index("def _ensure_timer(")]
    assert "_WAKE.wait(" in loop and "flush(force=woken)" in loop


def test_the_async_hooks_use_to_thread():
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    assert src.count("asyncio.to_thread(") >= 2


# =========================================================================== #
# LP-04 — the endpoint stamp must survive proxy request preparation
# =========================================================================== #
def _prepared_metadata(md: dict) -> dict:
    """Run LiteLLM's REAL proxy request preparation over `md`."""
    from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
    from litellm.proxy._types import UserAPIKeyAuth
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
             "headers": [], "query_string": b"", "client": ("127.0.0.1", 1234),
             "server": ("proxy.example.test", 443), "scheme": "https",
             "http_version": "1.1"}
    data = asyncio.run(add_litellm_data_to_request(
        data={"model": "claude-sonnet",
              "messages": [{"role": "user", "content": "hi"}],
              "metadata": dict(md)},
        request=Request(scope),
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-x", user_id="u"),
        proxy_config=type("C", (), {"get_config": lambda *a, **k: {}})(),
        general_settings={}, version="1.93.0"))
    return data.get("metadata") or {}


def test_the_proxy_really_does_overwrite_a_bare_endpoint_key():
    """Guards the premise: if LiteLLM stops overwriting it, this test tells us."""
    meta = _prepared_metadata({"endpoint": "mantle", "workload": "review"})
    assert meta.get("endpoint", "").startswith("http"), \
        "premise changed: the proxy no longer overwrites metadata.endpoint"


@pytest.mark.parametrize("stamp", [
    {"opslens.endpoint": "mantle", "workload": "review"},
    {"endpoint": "mantle", "workload": "review"},        # legacy key
])
def test_a_mantle_stamp_survives_real_proxy_preparation(cb, stamp):
    """An OpenAI-compatible Mantle deployment landed in the direct-OpenAI slice,
    because the proxy replaced the stamp with its own URL."""
    meta = _prepared_metadata(stamp)
    kw = {"model": "gpt-oss", "litellm_call_id": "c1",
          "litellm_params": {"custom_llm_provider": "openai", "metadata": meta}}
    ev = cb._build_event(kw, _Ok(), None, None, status=200, throttled=False)
    assert ev["endpoint"] == "mantle"


def test_the_proxy_url_can_never_become_the_endpoint(cb):
    meta = _prepared_metadata({"workload": "review"})
    kw = {"model": "us.anthropic.claude-sonnet-5", "litellm_call_id": "c1",
          "litellm_params": {"custom_llm_provider": "bedrock", "metadata": meta}}
    ev = cb._build_event(kw, _Ok(), None, None, status=200, throttled=False)
    assert ev["endpoint"] == "runtime"


def test_control_keys_are_not_attribution(cb):
    ev = cb._build_event(
        {"model": "m", "litellm_call_id": "c",
         "litellm_params": {"metadata": {"opslens.endpoint": "mantle",
                                         "workload": "w"}}},
        _Ok(), None, None, status=200, throttled=False)
    assert set(ev["dimensions"]) == {"workload"}


# =========================================================================== #
# DOC-01 — the module header must document the mechanism that works
# =========================================================================== #
def test_the_module_header_documents_the_customlogger_for_the_proxy():
    src = (ROOT / "tools/client-telemetry/litellm_callback.py").read_text()
    header = src[:src.index('"""', 3)]
    assert 'callbacks: ["litellm_callback.ops_lens_logger"]' in header
    assert "success_callback:" not in header, \
        "the header must not tell a proxy operator to use success_callback"


# =========================================================================== #
# the audit's OTEL test plan, executed: real OpenAI SDK + real instrumentation
# =========================================================================== #
# Shapes below are VERBATIM from a live run of openai 2.48.0 +
# opentelemetry-instrumentation-openai-v2 2.4b0 against a local
# OpenAI-compatible fixture server, exported by a real
# otel/opentelemetry-collector-contrib v0.160.0 with a traces pipeline. They are
# recorded here so the contract is pinned without needing the collector.
REAL_INSTRUMENTATION_ERROR_SPAN = {
    "resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "openai-fixture-app"}},
            {"key": "team", "value": {"stringValue": "protocol-test"}},
            {"key": "deployment.environment", "value": {"stringValue": "staging"}},
            {"key": "cost_center", "value": {"stringValue": "cc-otel-openai"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": "a" * 32, "spanId": "b" * 16, "name": "chat gpt-fixture",
            "kind": 3,
            "startTimeUnixNano": "1789056386000000000",
            "endTimeUnixNano": "1789056386001000000",
            # The instrumentation's `error.type` attribute is DROPPED by the SDK
            # (it passes a type object, not a string), so the only signal is the
            # status message - which carries the provider's real HTTP code.
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.provider.name", "value": {"stringValue": "openai"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-fixture"}},
                {"key": "server.address", "value": {"stringValue": "127.0.0.1"}}],
            "status": {"code": 2, "message": (
                "Error code: 400 - {'error': {'message': 'invalid request "
                "(fixture)', 'type': 'invalid_request_error'}}")},
        }]}]}]}


def test_a_real_instrumentation_error_span_keeps_the_providers_status():
    """A client error must not be reported as a server error: the panels split
    4xx from 5xx. The status arrives only inside the span message."""
    out = on.expand(REAL_INSTRUMENTATION_ERROR_SPAN)
    assert len(out) == 1
    assert out[0]["status"] == 400
    assert out[0]["throttled"] is False


@pytest.mark.parametrize("msg,expected", [
    ("Error code: 400 - {'error': {}}", 400),
    ("Error code: 429 - rate limited", 429),
    ("Error code: 500 - boom", 500),
    ("ThrottlingException: slow down", 429),   # no code, throttle wording
    ("something unrecognised", 500),           # no code, no wording
])
def test_span_status_classification(msg, expected):
    assert on.span_status({"status": {"code": 2, "message": msg}})[0] == expected


def test_a_real_instrumentation_span_carries_attribution_from_resource():
    e = on.expand(REAL_INSTRUMENTATION_ERROR_SPAN)[0]
    assert e["dimensions"] == {"workload": "openai-fixture-app",
                               "team": "protocol-test", "env": "staging",
                               "cost_center": "cc-otel-openai"}
    assert e["endpoint"] == "openai-api"


def test_the_readme_states_what_this_instrumentation_cannot_report():
    """Verified live: openai-v2 2.4b0 emits only input/output token attributes -
    no cached-token breakdown - and spans the LOGICAL call, so the SDK's own HTTP
    retries (two 429s in the live run) produce no events. Both must be stated
    rather than left for an operator to discover as missing data."""
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    low = src.lower()
    assert "cached" in low and "retr" in low
    assert "openai-compatible" in low


# =========================================================================== #
# Integration findings from the fresh us-east-2 deploy
# =========================================================================== #
def test_the_polymorphic_breakdown_groups_by_ordinal():
    """Found by the fresh-region run, where the effective source was
    invocation_logs for the first time. `f_daily_tagged` and `f_daily` have
    GENERATED columns named year/month/day, so `GROUP BY year` binds to the table
    column instead of the SELECT alias and Postgres rejects the ungrouped
    EXTRACT(...): "column f_daily_tagged.event_date must appear in the GROUP BY
    clause". `f_proxy_dim_hourly` has no such columns, so the proxy-sourced demo
    never hit it - a 500 that only a real invocation-log deployment reveals."""
    src = (ROOT / "backend/app/routers/attribution.py").read_text()
    fn = src[src.index("async def xtab_breakdown("):src.index("# --- attribute-filtered COST")]
    assert "GROUP BY year, month, day" not in fn, \
        "a query polymorphic over these tables must not group by ambiguous names"
    assert "GROUP BY 1, 2, 3, 4" in fn


def test_deploy_sh_passes_the_signup_mode_and_says_which_it_is():
    """The template defaults to admin-create-only, deploy.sh never passed the
    parameter, yet printed "sign-up gated to <domain>" and the README said "open
    the dashboard URL and sign up" - so a first deploy ended with a login nobody
    could pass."""
    src = (ROOT / "deploy.sh").read_text()
    assert '"ParameterKey":"CognitoSelfSignUp"' in src
    assert "admin-create-user" in src, "it must print how to create the first user"
    readme = (ROOT / "README.md").read_text()
    assert "admin-create-only by default" in readme
    assert "COGNITO_SELF_SIGNUP=enabled" in readme


def test_deploy_sh_treats_invocation_logging_as_per_region():
    """It found a logging bucket in ANOTHER region and treated the deploy region as
    handled, so every invocation-log-derived view stayed empty while the deploy
    reported success."""
    src = (ROOT / "deploy.sh").read_text()
    assert "configured PER REGION" in src
    assert 'BEDROCK_LOGS_REGION" != "$REGION"' in src


def test_deploy_sh_also_prints_the_admin_grant():
    """admin-create-user does NOT fire the PostConfirmation trigger that runs the
    first-admin bootstrap, so an admin-created user logs in with no admin and the
    dashboard offers no way to grant it. Verified live on the fresh stack: the user
    had zero groups until `admin-add-user-to-group` was run by hand."""
    src = (ROOT / "deploy.sh").read_text()
    assert "admin-add-user-to-group" in src
    assert "bedrock-lens-admins" in src
    assert "does NOT fire that trigger" in src
    readme = (ROOT / "README.md").read_text()
    assert "admin-add-user-to-group" in readme
    assert "sign out and back in" in readme.lower()


# =========================================================================== #
# Release review — /quota-drilldown must not disable ranked matching
# =========================================================================== #
def test_the_drilldown_endpoint_does_not_override_the_matcher():
    """Release review: the endpoint passed `matcher=_matches`, a BOOL predicate,
    so resolve_quota could not rank candidates and scored every match as exact.
    A generic quota name ("Mistral AI Mistral Large") then looked identical to a
    version-specific one ("Mistral Large 2407"), the two became a same-rank
    collision, and the resolver correctly refused to guess - so this endpoint
    reported an UNKNOWN limit where the shared resolver reported 50K and 160% for
    the same input."""
    src = (ROOT / "backend/app/routers/quota_drilldown.py").read_text()
    assert "matcher=_matches" not in src
    assert "Do NOT pass `matcher=` here" in src


@pytest.mark.parametrize("generic,specific,expected", [
    # (generic-name limit, version-specific limit, what 2407 should resolve to)
    (1_000_000, 50_000, 50_000),      # the reviewer's case: 160%, not unknown
    (50_000, 1_000_000, 1_000_000),   # exact name wins over generic
])
def test_the_drilldown_agrees_with_the_shared_resolver(generic, specific, expected):
    """Both paths must return the same limit for the same input. The endpoint
    resolves via the ranked default now, so this asserts agreement rather than
    re-testing the resolver."""
    from app.quota_match import family_hint_from_model_id, resolve_quota
    from app.routers import quota_drilldown as qd

    mid = "mistral.mistral-large-2407-v1:0"
    rows = [q("Mistral AI Mistral Large", generic, code="L-01447289"),
            q("Mistral Large 2407", specific, code="L-1C28E1AB")]
    hint = family_hint_from_model_id(mid)

    ranked = resolve_quota(rows, ACCT, REGION, mid, "TPM", hint)
    assert ranked.value == expected

    # The bool-matcher path is what the endpoint used to take: prove it diverges,
    # so this test fails if anyone reintroduces the override.
    overridden = resolve_quota(rows, ACCT, REGION, mid, "TPM", hint,
                               matcher=qd._matches)
    assert overridden.value is None, (
        "a bool matcher must collapse the ranks and lose the limit - if this "
        "changes, the reason for banning matcher= has changed too")


def test_the_drilldown_reports_a_real_utilization_for_the_reviewers_case():
    from app.quota_match import family_hint_from_model_id, resolve_quota
    mid = "mistral.mistral-large-2407-v1:0"
    rows = [q("Mistral AI Mistral Large", 1_000_000, code="L-01447289"),
            q("Mistral Large 2407", 50_000, code="L-1C28E1AB")]
    res = resolve_quota(rows, ACCT, REGION, mid, "TPM",
                        family_hint_from_model_id(mid))
    assert res.value == 50_000
    assert 80_000 / res.value * 100 == pytest.approx(160.0)
