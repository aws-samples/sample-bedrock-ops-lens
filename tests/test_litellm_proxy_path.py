"""LiteLLM PROXY integration — defects found by running the real proxy.

The earlier live test drove the litellm PYTHON SDK
(`litellm.success_callback = [fn]`). Customers follow the README, which
configures the PROXY (`litellm --config config.yaml`). Running that revealed the
documented install was broken in four separate ways. Verified against
litellm 1.93.0.

Run: .venv/bin/python -m pytest tests/test_litellm_proxy_path.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/client-telemetry"))

import litellm_callback as lc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Capture submissions and reset the dedupe memo between tests."""
    sent: list[dict] = []
    monkeypatch.setattr(lc, "_submit", lambda ev: sent.append(ev))
    lc._seen_calls.clear()
    return sent


@pytest.fixture()
def sent(_isolate):
    return _isolate


class Usage(dict):
    pass


def _resp(rid="r1", prompt=14, completion=4):
    class R:
        id = rid
        usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    return R()


def _chunk(rid="r1"):
    class C:
        id = rid
        usage = None
    return C()


def _kwargs(meta=None, **extra):
    k = {"model": "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
         "litellm_params": {"custom_llm_provider": "bedrock",
                            "metadata": meta or {}},
         "litellm_call_id": extra.pop("call_id", "call-1")}
    k.update(extra)
    return k


# --------------------------------------------------------------------------- #
# 1 — a plain function in success_callback is never invoked by the proxy
# --------------------------------------------------------------------------- #
def test_a_customlogger_instance_is_exported_for_the_proxy():
    """Live: the proxy printed "Initialized Success Callbacks - [<function
    ops_lens_handler>]" and then never called it. Requests returned 200 and the
    only telemetry reaching S3 was the failure events - i.e. a dashboard showing
    100% error rate and no traffic. The mechanism the proxy actually calls is a
    CustomLogger instance registered under `litellm_settings.callbacks`."""
    assert hasattr(lc, "ops_lens_logger"), "no CustomLogger instance exported"
    inst = lc.ops_lens_logger
    assert not isinstance(inst, type), "must be an INSTANCE, not the class"
    for hook in ("log_success_event", "async_log_success_event",
                 "log_failure_event", "async_log_failure_event"):
        assert callable(getattr(inst, hook, None)), f"missing {hook}"


def test_the_readme_documents_the_mechanism_that_works():
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "litellm_callback.ops_lens_logger" in src or \
           "callbacks: [\"litellm_callback.ops_lens_logger\"]" in src, \
           "the README must document the CustomLogger the proxy actually calls"


def test_the_readme_module_name_matches_the_shipped_filename():
    """Following the README verbatim, the proxy refused to START:
    `ImportError: Could not find module file /tmp/llproxy/ops_lens_callback.py`
    because the config said `ops_lens_callback` and the shipped file is
    `litellm_callback.py`."""
    src = (ROOT / "tools/client-telemetry/README.md").read_text()
    assert "ops_lens_callback.ops_lens_handler" not in src
    assert (ROOT / "tools/client-telemetry/litellm_callback.py").exists()


def test_the_logger_hooks_delegate_to_the_same_builder(sent):
    lc.ops_lens_logger.log_success_event(_kwargs({"workload": "w"}), _resp(),
                                        None, None)
    assert len(sent) == 1 and sent[0]["input_tokens"] == 14


# --------------------------------------------------------------------------- #
# 2 — streaming under the proxy: ONE firing, already aggregated
# --------------------------------------------------------------------------- #
def test_proxy_streaming_emits_an_event(sent):
    """The proxy fires once for a streamed request, handing over the AGGREGATED
    response, with no `complete_streaming_response` in kwargs. Requiring that key
    dropped every streamed request through the proxy - live, 2 of 3 successes were
    recorded and the streamed one vanished."""
    lc.ops_lens_logger.log_success_event(
        _kwargs({"workload": "w"}, stream=True), _resp(prompt=18, completion=13),
        None, None)
    assert len(sent) == 1, "a streamed proxy request must produce one event"
    assert sent[0]["input_tokens"] == 18 and sent[0]["output_tokens"] == 13


def test_sdk_intermediate_chunks_are_still_skipped(sent):
    """The SDK fires per chunk. Those firings carry no usage and must not become
    events - the regression this guard originally existed for."""
    for _ in range(4):
        lc.ops_lens_handler(_kwargs({"workload": "w"}, stream=True), _chunk(),
                            None, None)
    assert sent == []


def test_sdk_final_aggregate_still_wins(sent):
    lc.ops_lens_handler(
        _kwargs({"workload": "w"}, stream=True,
                complete_streaming_response=_resp(prompt=19, completion=13)),
        _chunk(), None, None)
    assert len(sent) == 1 and sent[0]["input_tokens"] == 19


# --------------------------------------------------------------------------- #
# 3 — both hooks fire for one call; one request must be one event
# --------------------------------------------------------------------------- #
def test_sync_and_async_failure_hooks_do_not_double_count(sent):
    """Live: one failed request produced TWO identical failure events, because the
    proxy invokes both the sync and the async hook. The ingester dedupes on
    (event_date, request_id, ts), but two firings can straddle a second boundary
    and both persist."""
    import asyncio

    class Boom(Exception):
        status_code = 400

    kw = _kwargs({"workload": "w"}, exception=Boom("bad model"), call_id="dup-1")
    lc.ops_lens_logger.log_failure_event(kw, None, None, None)
    asyncio.run(lc.ops_lens_logger.async_log_failure_event(kw, None, None, None))
    assert len(sent) == 1, f"expected 1 failure event, got {len(sent)}"


def test_sync_and_async_success_hooks_do_not_double_count(sent):
    import asyncio
    kw = _kwargs({"workload": "w"}, call_id="dup-2")
    lc.ops_lens_logger.log_success_event(kw, _resp(), None, None)
    asyncio.run(lc.ops_lens_logger.async_log_success_event(kw, _resp(), None, None))
    assert len(sent) == 1


def test_distinct_calls_are_not_suppressed(sent):
    for i in range(3):
        lc.ops_lens_logger.log_success_event(
            _kwargs({"workload": "w"}, call_id=f"c{i}"), _resp(rid=f"r{i}"),
            None, None)
    assert len(sent) == 3


def test_a_success_and_a_failure_for_one_call_are_distinct(sent):
    """Outcome is part of the dedupe key: a retried call that fails then succeeds
    must not have its second outcome suppressed."""
    class Boom(Exception):
        status_code = 500
    kw = _kwargs({"workload": "w"}, call_id="same", exception=Boom("x"))
    lc.ops_lens_logger.log_failure_event(kw, None, None, None)
    lc.ops_lens_logger.log_success_event(_kwargs({"workload": "w"}, call_id="same"),
                                        _resp(), None, None)
    assert len(sent) == 2


def test_the_dedupe_memo_is_bounded():
    lc._seen_calls.clear()
    for i in range(lc._SEEN_MAX + 200):
        lc._first_time(_kwargs(call_id=f"k{i}"), None, "ok")
    assert len(lc._seen_calls) <= lc._SEEN_MAX


# --------------------------------------------------------------------------- #
# 4 — the proxy injects metadata of its own; some of it collides
# --------------------------------------------------------------------------- #
PROXY_META = {
    "workload": "proxy-search", "team": "ml-platform", "env": "prod",
    "user": "pat@corp.example",
    # everything below is injected BY THE PROXY, observed live
    "endpoint": "http://localhost:4000/v1/chat/completions",
    "requester_ip_address": "127.0.0.1",
    "user_agent": "Python-urllib/3.14",
    "queue_time_seconds": "5.29e-05",
    "attempted_retries": 0,
    "max_retries": 2,
    "deployment_model_name": "claude-sonnet",
}


def test_the_proxys_endpoint_metadata_does_not_become_the_endpoint(sent):
    """`metadata["endpoint"]` is our documented way to stamp mantle - and the
    proxy injects a key of the same name holding its own URL. Live, that URL was
    recorded as the endpoint and coerced to "unknown" downstream."""
    lc.ops_lens_logger.log_success_event(_kwargs(PROXY_META), _resp(), None, None)
    assert sent[0]["endpoint"] == "runtime"


def test_an_explicit_mantle_stamp_still_works(sent):
    lc.ops_lens_logger.log_success_event(
        _kwargs({"workload": "w", "endpoint": "mantle"}), _resp(), None, None)
    assert sent[0]["endpoint"] == "mantle"


def test_proxy_bookkeeping_does_not_become_attribution(sent):
    """requester_ip_address and user_agent are per-request values that would make
    every request its own dimension value - and this emitter is explicitly not in
    the business of shipping client IPs."""
    lc.ops_lens_logger.log_success_event(_kwargs(PROXY_META), _resp(), None, None)
    assert set(sent[0]["dimensions"]) == {"workload", "team", "env", "user"}


def test_operator_supplied_extra_metadata_is_still_kept(sent):
    lc.ops_lens_logger.log_success_event(
        _kwargs({"workload": "w", "cost_center": "cc-1", "project": "p9"}),
        _resp(), None, None)
    dims = sent[0]["dimensions"]
    assert dims["cost_center"] == "cc-1" and dims["project"] == "p9"


def test_attempted_retries_is_used_as_the_retry_count(sent):
    lc.ops_lens_logger.log_success_event(
        _kwargs({"workload": "w", "attempted_retries": 2}), _resp(), None, None)
    assert sent[0]["retry_attempts"] == 2


# --------------------------------------------------------------------------- #
# 5 — fail-open must not mean fail-silent
# --------------------------------------------------------------------------- #
def test_a_raising_handler_does_not_propagate(monkeypatch, sent):
    """Telemetry must never break inference."""
    monkeypatch.setattr(lc, "_build_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    lc.ops_lens_logger.log_success_event(_kwargs({"workload": "w"}), _resp(),
                                        None, None)   # must not raise
    assert sent == []


def test_failures_are_surfaceable_with_a_debug_flag(monkeypatch, capsys):
    """A handler raising on every request while the proxy returns 200 is
    indistinguishable from "no traffic" - which is exactly how the broken proxy
    integration hid. OPS_LENS_DEBUG=1 must surface it."""
    monkeypatch.setenv("OPS_LENS_DEBUG", "1")
    monkeypatch.setattr(lc, "_build_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    lc.ops_lens_logger.log_success_event(_kwargs({"workload": "w"}), _resp(),
                                        None, None)
    out = capsys.readouterr().out
    assert "ops-lens" in out and "boom" in out
