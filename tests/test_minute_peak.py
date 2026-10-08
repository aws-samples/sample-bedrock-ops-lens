"""Busiest-minute reduction: the cases where a peak is easy to get wrong.

Run: python -m pytest tests/test_minute_peak.py -q

The collector's whole reason for reducing in Python is that per-identifier
maxima cannot be recombined afterwards. These tests pin that, plus the
native-vs-reconstructed selection and the refusal to invent zeros.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ingestion.cw_minute_peak import (  # noqa: E402
    MAX_MINUTE_DAYS, minute_window, reduce_day, utc_day_bounds,
)

M0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def series(**per_id):
    """series(a={'Invocations': [100, 0]}) -> minute-keyed CloudWatch shape.

    A list position is a minute; None means AWS reported no datapoint for that
    minute, which is NOT the same as a reported zero.
    """
    out = {}
    for ident, metrics in per_id.items():
        out[ident] = {}
        for metric, values in metrics.items():
            out[ident][metric] = {M0 + timedelta(minutes=i): v
                                  for i, v in enumerate(values) if v is not None}
    return out


# --------------------------------------------------------------------------- #
# The two traps: coincident vs non-coincident sources
# --------------------------------------------------------------------------- #
def test_non_coincident_sources_do_not_add_their_peaks():
    """direct [100, 0] + profile [0, 100] -> 100, never 200.

    Summing per-identifier maxima would claim 200 requests in one minute that
    never happened, and would be read as capacity risk that does not exist.
    """
    s = series(direct={"Invocations": [100, 0]}, profile={"Invocations": [0, 100]})
    out = reduce_day(["direct", "profile"], s, rate=5)
    assert out["peak_rpm"] == 100


def test_coincident_sources_are_summed_before_the_peak():
    """direct [100, 0] + profile [100, 0] -> 200, never 100.

    Taking the max of per-identifier maxima would under-report the real
    simultaneous peak and hide genuine throttling risk."""
    s = series(direct={"Invocations": [100, 0]}, profile={"Invocations": [100, 0]})
    out = reduce_day(["direct", "profile"], s, rate=5)
    assert out["peak_rpm"] == 200
    assert out["peak_rpm_at"] == M0


# --------------------------------------------------------------------------- #
# Reconstruction: weight and combine per minute, then reduce
# --------------------------------------------------------------------------- #
def test_reconstruction_weights_output_then_reduces_per_minute():
    """input [1000, 10000], output [1000, 0], rate 10, no cache writes.

    minute 0 = 1000 + 1000*10 = 11,000; minute 1 = 10,000 + 0 = 10,000.
    The peak is 11,000 - the busiest INPUT minute is not the busiest quota
    minute, so the weighting has to happen before the maximum.
    """
    s = series(a={"InputTokenCount": [1000, 10000], "OutputTokenCount": [1000, 0]})
    out = reduce_day(["a"], s, rate=10)
    assert out["peak_quota_tpm"] == 11000
    assert out["quota_tpm_source"] == "reconstructed"
    # Raw input/output peaks are reported separately and legitimately differ.
    assert out["peak_input_tpm"] == 10000
    assert out["peak_output_tpm"] == 1000


def test_cache_write_counts_toward_quota_and_cache_read_does_not():
    """Per the AWS burndown doc, cache WRITE consumes quota; cache READ does
    not (it is still billed, which is a cost concern, not a quota one)."""
    s = series(a={"InputTokenCount": [100], "CacheWriteInputTokenCount": [50],
                  "OutputTokenCount": [10]})
    out = reduce_day(["a"], s, rate=5)
    assert out["peak_quota_tpm"] == 100 + 50 + 10 * 5
    assert out["peak_input_tpm"] == 150  # input + cache write


# --------------------------------------------------------------------------- #
# Native vs reconstructed, chosen per minute
# --------------------------------------------------------------------------- #
def test_partial_native_coverage_does_not_suppress_a_higher_reconstructed_minute():
    """native [0, missing], reconstruction [500, 2000] -> peak 2000, mixed.

    Preferring native wherever it exists for the whole series would have picked
    the observed zero and reported no usage. The choice must be per minute, and
    an observed zero must stay an observation.
    """
    s = series(a={"EstimatedTPMQuotaUsage": [0, None],
                  "InputTokenCount": [500, 2000]})
    out = reduce_day(["a"], s, rate=10)
    assert out["peak_quota_tpm"] == 2000
    assert out["quota_tpm_source"] == "mixed"


def test_native_is_authoritative_and_the_rate_is_not_applied_twice():
    """EstimatedTPMQuotaUsage already includes cache write and the output
    multiplier, so the reconstruction must not be layered on top."""
    s = series(a={"EstimatedTPMQuotaUsage": [9000],
                  "InputTokenCount": [100], "OutputTokenCount": [100]})
    out = reduce_day(["a"], s, rate=10)
    assert out["peak_quota_tpm"] == 9000
    assert out["quota_tpm_source"] == "aws_estimate"


def test_an_observed_zero_is_an_observation_not_a_gap():
    s = series(a={"EstimatedTPMQuotaUsage": [0], "Invocations": [0]})
    out = reduce_day(["a"], s, rate=5)
    assert out["active_minutes"] == 1
    assert out["peak_quota_tpm"] == 0
    assert out["quota_tpm_source"] == "aws_estimate"


def test_no_observations_reports_unavailable_rather_than_zero():
    out = reduce_day(["a"], {}, rate=5)
    assert out["active_minutes"] == 0
    assert out["quota_tpm_source"] == "unavailable"
    assert out["peak_quota_tpm"] is None
    assert out["peak_rpm"] is None


# --------------------------------------------------------------------------- #
# Timestamps and grain
# --------------------------------------------------------------------------- #
def test_quota_and_request_peaks_keep_their_own_timestamps():
    """They genuinely occur in different minutes; one timestamp for both would
    be a fabrication."""
    s = series(a={"Invocations": [10, 1],
                  "InputTokenCount": [0, 50000]})
    out = reduce_day(["a"], s, rate=5)
    assert out["peak_rpm_at"] == M0
    assert out["peak_quota_tpm_at"] == M0 + timedelta(minutes=1)


def test_utc_day_bounds_are_midnight_to_midnight():
    from datetime import date
    start, end = utc_day_bounds(date(2026, 10, 3))
    assert start.isoformat() == "2026-10-03T00:00:00+00:00"
    assert end.isoformat() == "2026-10-04T00:00:00+00:00"
    assert (end - start) == timedelta(days=1)


def test_window_is_capped_at_cloudwatch_minute_retention():
    """1-minute datapoints live 15 days. Asking for more would silently return a
    coarser period, which must never be stored as a minute peak."""
    assert len(minute_window(90)) == MAX_MINUTE_DAYS
    assert len(minute_window(14)) == 14
    days = minute_window(3)
    assert days == sorted(days), "newest last"


def test_active_minutes_counts_reported_samples_only():
    """It is NOT collection coverage: a quiet application is sparse by nature.
    Coverage is tracked in f_minute_collection."""
    s = series(a={"Invocations": [1, None, None, None, 1]})
    out = reduce_day(["a"], s, rate=5)
    assert out["active_minutes"] == 2


def test_main_is_a_coroutine_like_every_other_ingester():
    """lambda_handler._run_module awaits main() from inside a running loop.

    A sync main() that calls asyncio.run() works from the CLI and fails in Lambda
    with "asyncio.run() cannot be called from a running event loop" — found in
    live testing, not by any unit test, so pin it here.
    """
    import asyncio as _asyncio
    import importlib
    from ingestion import cw_minute_peak
    assert _asyncio.iscoroutinefunction(cw_minute_peak.main)
    for name in ("cw_metrics", "inference_profiles", "cw_minute_peak"):
        mod = importlib.import_module(f"ingestion.{name}")
        assert _asyncio.iscoroutinefunction(mod.main), f"{name}.main must be awaitable"


def test_a_cris_identifier_does_not_abort_profile_discovery():
    """GetInferenceProfile on "us.anthropic.claude-sonnet-5" succeeds and returns
    type=SYSTEM_DEFINED. Letting profile_record raise on that aborted discovery
    for the whole Region, leaving every application profile hash unresolved
    there. Observed live 2026-10-06: us-east-1 resolved nothing while us-west-2
    resolved 19 profiles.
    """
    from unittest.mock import Mock
    from ingestion import inference_profiles as ip

    account, region = "111111111111", "us-east-1"
    app_arn = f"arn:aws:bedrock:{region}:{account}:application-inference-profile/aaaaaa111111"
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": [{
        "inferenceProfileArn": app_arn, "inferenceProfileId": "aaaaaa111111",
        "inferenceProfileName": "expense-assistant", "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [{"modelArn": f"arn:aws:bedrock:{region}::foundation-model/amazon.nova-lite-v1:0"}],
    }]}
    # The observed CRIS id is probed via Get and comes back SYSTEM_DEFINED.
    client.get_inference_profile.return_value = {
        "inferenceProfileArn": f"arn:aws:bedrock:{region}:{account}:inference-profile/us.anthropic.claude-sonnet-5",
        "inferenceProfileId": "us.anthropic.claude-sonnet-5",
        "inferenceProfileName": "US Claude Sonnet 5", "type": "SYSTEM_DEFINED",
        "models": [{"modelArn": f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-5"}],
    }
    system_arn = client.get_inference_profile.return_value["inferenceProfileArn"]
    rows, warnings = ip.read_profiles(client, account, region, {system_arn, app_arn})
    client.get_inference_profile.assert_called_once_with(inferenceProfileIdentifier=system_arn)
    ids = {r[2] for r in rows}
    assert "aaaaaa111111" in ids, "the real application profile must still be cached"
    assert "us.anthropic.claude-sonnet-5" not in ids, "a CRIS id is not an application profile"
    assert warnings == [], "an expected SYSTEM_DEFINED answer is not a warning"
