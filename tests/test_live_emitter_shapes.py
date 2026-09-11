"""Defects found only by running REAL emitters — not reproducible with stubs.

An end-to-end run (live LiteLLM against Bedrock, and a real
otel/opentelemetry-collector-contrib v0.160.0 writing to S3) exposed four things
that every fabricated-fixture test had missed. Each is pinned here with the shape
the real software actually produced.

Run: .venv/bin/python -m pytest tests/test_live_emitter_shapes.py -q
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/client-telemetry"))

from ingestion import otel_normalize as on  # noqa: E402
from ingestion import proxy_events as pe  # noqa: E402


# --------------------------------------------------------------------------- #
# 1 — LiteLLM fires success_callback ONCE PER STREAMED CHUNK
# --------------------------------------------------------------------------- #
# Verified against litellm 1.93.0: a five-chunk stream fired the callback five
# times. Four firings carried a per-chunk ModelResponseStream with no usage, so
# one request became five events, four of them with in=0/out=0. Only the final
# firing carries `complete_streaming_response`.
def test_intermediate_stream_chunks_do_not_emit(monkeypatch):
    import litellm_callback as lc
    sent = []
    monkeypatch.setattr(lc, "_submit", lambda ev: sent.append(ev))

    class Chunk:                      # what LiteLLM passes per chunk
        usage = None
        id = "chunk"

    # Four intermediate chunk firings: stream=True, no aggregated response.
    for _ in range(4):
        lc.ops_lens_handler({"model": "m", "stream": True,
                             "litellm_params": {"metadata": {}}},
                            Chunk(), None, None)
    assert sent == [], "an intermediate chunk must not become an event"


def test_the_final_stream_firing_emits_once_with_the_aggregated_usage(monkeypatch):
    import litellm_callback as lc
    sent = []
    monkeypatch.setattr(lc, "_submit", lambda ev: sent.append(ev))

    class Final:
        id = "resp-1"
        usage = {"prompt_tokens": 19, "completion_tokens": 13}

    lc.ops_lens_handler({"model": "m", "stream": True,
                         "litellm_params": {"metadata": {"workload": "w"}},
                         "complete_streaming_response": Final()},
                        None, None, None)
    assert len(sent) == 1, "the aggregated firing must produce exactly one event"
    assert sent[0]["input_tokens"] == 19
    assert sent[0]["output_tokens"] == 13, (
        "usage must come from complete_streaming_response, not the empty chunk")


def test_a_non_streaming_request_still_emits_once(monkeypatch):
    import litellm_callback as lc
    sent = []
    monkeypatch.setattr(lc, "_submit", lambda ev: sent.append(ev))

    class Resp:
        id = "r"
        usage = {"prompt_tokens": 14, "completion_tokens": 4}

    lc.ops_lens_handler({"model": "m", "litellm_params": {"metadata": {}}},
                        Resp(), None, None)
    assert len(sent) == 1 and sent[0]["input_tokens"] == 14


# --------------------------------------------------------------------------- #
# 2 — NDJSON objects must end with a newline
# --------------------------------------------------------------------------- #
# Reading the live bucket back failed with "JSONDecodeError: Extra data" because
# each object's last record ran into the next object's first record.
def test_uploaded_objects_end_with_a_newline():
    import litellm_callback as lc
    captured = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body):
            captured["body"] = Body
            captured["key"] = Key

    lc._client = lambda: FakeS3()
    lc._BUCKET = "b"
    assert lc._upload([{"a": 1}, {"a": 2}]) is True
    raw = gzip.decompress(captured["body"])
    assert raw.endswith(b"\n"), "a missing trailing newline corrupts concatenation"
    # And concatenating two objects must still parse line by line.
    doubled = (raw + raw).decode()
    recs = [json.loads(l) for l in doubled.splitlines() if l.strip()]
    assert len(recs) == 4


# --------------------------------------------------------------------------- #
# 3 — the OTEL collector writes the OTLP ENVELOPE, not flat records
# --------------------------------------------------------------------------- #
# This is the exact shape otel/opentelemetry-collector-contrib v0.160.0 wrote with
# `marshaler: otlp_json`: resource attributes held once per resourceLogs entry,
# several logRecords per envelope, `observedTimeUnixNano` (no `timeUnixNano`), and
# the body as an AnyValue wrapper.
LIVE_ENVELOPE = {
    "resourceLogs": [{
        "resource": {"attributes": [
            {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
            {"key": "service.instance.id", "value": {"stringValue": "0ea3f96a"}},
            {"key": "service.name", "value": {"stringValue": "otel-search-svc"}},
            {"key": "team", "value": {"stringValue": "otel-platform"}},
            {"key": "deployment.environment", "value": {"stringValue": "prod"}},
            {"key": "cost_center", "value": {"stringValue": "cc-otel"}},
        ]},
        "scopeLogs": [{
            "scope": {"name": "bedrock.genai"},
            "logRecords": [
                {"observedTimeUnixNano": "1789056386461412000",
                 "severityText": "INFO",
                 "body": {"stringValue": "gen_ai.client.inference"},
                 "attributes": [
                     {"key": "gen_ai.provider.name", "value": {"stringValue": "aws.bedrock"}},
                     {"key": "gen_ai.request.model", "value": {"stringValue": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"}},
                     {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "14"}},
                     {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "4"}},
                     {"key": "gen_ai.client.operation.duration", "value": {"doubleValue": 1.5164}},
                     {"key": "gen_ai.server.time_to_first_token", "value": {"doubleValue": 0.9098}},
                     {"key": "http.response.status_code", "value": {"intValue": "200"}},
                     {"key": "cloud.region", "value": {"stringValue": "us-west-2"}},
                     {"key": "user.email", "value": {"stringValue": "dave@corp.example"}},
                 ]},
                {"observedTimeUnixNano": "1789056386461465000",
                 "body": {"stringValue": "gen_ai.client.inference"},
                 "attributes": [
                     {"key": "gen_ai.request.model", "value": {"stringValue": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"}},
                     {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "0"}},
                     {"key": "error.type", "value": {"stringValue": "ThrottlingException"}},
                     {"key": "user.email", "value": {"stringValue": "alice@corp.example"}},
                 ]},
                {"observedTimeUnixNano": "1789056386461480000",
                 "body": {"stringValue": "gen_ai.client.inference"},
                 "attributes": [
                     {"key": "http.request.method", "value": {"stringValue": "GET"}},
                     {"key": "url.path", "value": {"stringValue": "/healthz"}},
                 ]},
            ]}]}]}


def test_the_envelope_is_recognised_and_expanded():
    """`normalize()` alone saw no `ts` and no `model` at the envelope level, so the
    ENTIRE batch was dropped - the live collector's output reached S3 and then
    vanished."""
    assert on.is_envelope(LIVE_ENVELOPE) is True
    events = on.expand(LIVE_ENVELOPE)
    assert len(events) == 2, (
        "two inference records expected; the /healthz record must be skipped")


def test_expanded_events_carry_resource_attributes_as_attribution():
    """Resource attributes are held ONCE per resourceLogs entry, which is where
    OTEL_RESOURCE_ATTRIBUTES lands. Each record has to inherit them."""
    ev = on.expand(LIVE_ENVELOPE)[0]
    assert ev["dimensions"] == {
        "workload": "otel-search-svc", "team": "otel-platform",
        "env": "prod", "cost_center": "cc-otel", "user": "dave@corp.example"}


def test_the_anyvalue_body_wrapper_is_not_a_dimension():
    """The body arrives as {"stringValue": ...}; absorbing its keys leaked a
    literal `stringValue` dimension on real output."""
    for ev in on.expand(LIVE_ENVELOPE):
        assert "stringValue" not in ev["dimensions"]
        assert "service_instance_id" not in ev["dimensions"]


def test_observed_time_is_used_when_time_is_absent():
    """The collector emitted only `observedTimeUnixNano`."""
    ev = on.expand(LIVE_ENVELOPE)[0]
    assert ev["ts"].startswith("2026-")


def test_seconds_are_converted_and_an_error_type_becomes_a_throttle():
    a, b = on.expand(LIVE_ENVELOPE)
    assert a["latency_ms"] == pytest.approx(1516.4)
    assert a["ttft_ms"] == pytest.approx(909.8)
    assert a["input_tokens"] == 14 and a["output_tokens"] == 4
    assert b["status"] == 429 and b["throttled"] is True


def test_a_full_pipeline_parse_of_the_live_shape():
    """The ingester's own parser must turn the envelope into usable rows."""
    events = on.expand(LIVE_ENVELOPE)
    parsed = [pe._parse_event(e) for e in events]
    assert all(p is not None for p in parsed)
    endpoints = {p[5] for p in parsed}
    assert endpoints == {"runtime"}


# --------------------------------------------------------------------------- #
# 4 — one S3 object can hold CONCATENATED JSON documents with no newlines
# --------------------------------------------------------------------------- #
def test_the_reader_handles_concatenated_json_documents():
    """The collector wrote 4,477 bytes containing zero newlines. Splitting on
    newlines produced one unparseable blob and the file was skipped entirely."""
    blob = (json.dumps(LIVE_ENVELOPE) + json.dumps(LIVE_ENVELOPE)).encode()

    class FakeS3:
        def get_object(self, Bucket, Key):
            return {"Body": type("B", (), {"read": staticmethod(lambda: blob)})()}

    events = list(pe._read_event_lines(FakeS3(), "b", "logs_1.json"))
    assert len(events) == 4, (
        "two concatenated envelopes x two inference records each")
    assert all(e.get("model") for e in events)


def test_ndjson_still_works_and_a_corrupt_line_costs_only_itself():
    good = json.dumps({"ts": "2026-09-08T10:00:00Z", "model": "m",
                       "endpoint": "runtime", "region": "us-west-2",
                       "input_tokens": 1, "output_tokens": 2, "request_id": "a"})
    blob = (good + "\n{ this is not json }\n" + good.replace('"a"', '"b"')).encode()

    class FakeS3:
        def get_object(self, Bucket, Key):
            return {"Body": type("B", (), {"read": staticmethod(lambda: blob)})()}

    events = list(pe._read_event_lines(FakeS3(), "b", "x.jsonl"))
    assert len(events) == 2, "the good records either side must survive"


def test_gzipped_objects_still_work():
    rec = json.dumps({"ts": "2026-09-08T10:00:00Z", "model": "m",
                      "endpoint": "runtime", "region": "us-west-2",
                      "input_tokens": 1, "output_tokens": 2, "request_id": "a"})
    blob = gzip.compress((rec + "\n").encode())

    class FakeS3:
        def get_object(self, Bucket, Key):
            return {"Body": type("B", (), {"read": staticmethod(lambda: blob)})()}

    assert len(list(pe._read_event_lines(FakeS3(), "b", "x.jsonl.gz"))) == 1


# --------------------------------------------------------------------------- #
# 5 — the documented collector setting must be the one that works
# --------------------------------------------------------------------------- #
def test_the_readme_does_not_recommend_the_marshaler_that_discards_attributes():
    """`marshaler: body` wrote a 4,477-byte record to S3 as the 23-byte string
    "gen_ai.client.inference" - every attribute gone."""
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "marshaler: otlp_json" in src
    assert "marshaler: body   #" not in src
    assert "marshaler: body\n" not in src
