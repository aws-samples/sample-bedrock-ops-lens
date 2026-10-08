"""Live per-minute pull: request validation, CloudWatch error mapping, and the
per-minute series it charts.

Run: python -m pytest tests/test_live_pull.py -q

The live chart and the stored busiest-minute peak must never disagree about the
same minutes, so combine_minutes is pinned against reduce_day here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import sys

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from ingestion import live_pull  # noqa: E402
from ingestion.cw_minute_peak import METRICS, combine_minutes, reduce_day  # noqa: E402
from test_minute_peak import M0, series  # noqa: E402

ACCOUNT, REGION = "111111111111", "us-east-1"
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
PROFILE = "abcdef123456"
ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/{PROFILE}"
NOW = datetime(2026, 10, 6, 12, 30, 45, tzinfo=timezone.utc)


def event(**extra):
    return {"account_id": ACCOUNT, "region": REGION, "model_id": MODEL,
            "identifiers": [MODEL, ARN, PROFILE], "hours": 3, "rate": 5,
            "rate_source": "bundled_default", **extra}


class FakeCloudWatch:
    """GetMetricData over {identifier: {metric: {minute: value}}}."""

    def __init__(self, data, status="Complete", error=None):
        self.data, self.status, self.error = data, status, error
        self.calls = []

    def get_metric_data(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        results = []
        for q in kw["MetricDataQueries"]:
            metric = q["MetricStat"]["Metric"]
            ident = metric["Dimensions"][0]["Value"]
            points = sorted((t, v) for t, v in
                            self.data.get(ident, {}).get(metric["MetricName"], {}).items()
                            if kw["StartTime"] <= t < kw["EndTime"])
            results.append({"Id": q["Id"], "StatusCode": self.status,
                            "Timestamps": [t for t, _ in points],
                            "Values": [float(v) for _, v in points]})
        return {"MetricDataResults": results}


class FakeSession:
    def __init__(self, cw):
        self.cw, self.regions = cw, []

    def client(self, name, region_name=None, config=None):
        assert name == "cloudwatch"
        self.regions.append(region_name)
        return self.cw


def opener(cw):
    session = FakeSession(cw)
    return session, (lambda account: session)


def at(hh, mm):
    return datetime(2026, 10, 6, hh, mm, tzinfo=timezone.utc)


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "detail that must not leak"}},
                       "GetMetricData")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_validate_normalises_and_always_includes_the_model():
    req = live_pull.validate(event(identifiers=[ARN, PROFILE, ARN]))
    assert req["identifiers"] == [MODEL, ARN, PROFILE]
    assert req["hours"] == 3 and req["rate"] == 5
    assert live_pull.validate(event(identifiers=None))["identifiers"] == [MODEL]


@pytest.mark.parametrize("bad", [
    {"account_id": "12345"}, {"account_id": "11111111111a"},
    {"account_id": "\u0661" * 12},                           # non-ASCII digits
    {"region": "US-EAST-1"}, {"region": "us-east-1; rm"},
    {"model_id": ""}, {"model_id": "-leading-dash"}, {"model_id": "a b"},
    {"hours": 2}, {"hours": 48}, {"hours": "three"},
    {"identifiers": "not-a-list"}, {"identifiers": ["ok", "bad value"]},
    {"identifiers": [f"id{i}" for i in range(live_pull.MAX_IDENTIFIERS + 1)]},
    {"rate": 0}, {"rate": 101}, {"rate": "five"},
])
def test_validate_rejects_bad_fields(bad):
    with pytest.raises(ValueError):
        live_pull.validate(event(**bad))


def test_handler_rejects_an_invalid_event_before_any_aws_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no AWS call may happen for an invalid request")
    monkeypatch.setattr(live_pull, "open_session", boom)
    out = live_pull.handler(event(hours=5), None)
    assert out == {"ok": False, "error": "invalid_request",
                   "detail": "hours must be one of (1, 3, 6, 12, 24)"}


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #
def test_pull_combines_identifiers_per_minute_over_a_closed_window():
    cw = FakeCloudWatch({
        MODEL: {"Invocations": {at(12, 29): 2, at(12, 30): 50, at(9, 29): 70},
                "InputTokenCount": {at(12, 29): 100},
                "OutputTokenCount": {at(12, 29): 10}},
        PROFILE: {"Invocations": {at(12, 29): 3, at(11, 0): 4},
                  "InputTokenCount": {at(12, 29): 200, at(11, 0): 300},
                  "OutputTokenCount": {at(12, 29): 20, at(11, 0): 30},
                  "EstimatedTPMQuotaUsage": {at(12, 29): 500, at(11, 0): 700}},
    })
    session, open_ = opener(cw)
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=open_)
    assert session.regions == [REGION]
    assert out["ok"] and out["status"] == "complete"
    # [09:30, 12:30): the open minute 12:30 and 09:29 are outside.
    assert out["window"] == {"start": "2026-10-06T09:30:00+00:00",
                             "end": "2026-10-06T12:30:00+00:00", "hours": 3}
    assert out["pulled_at"] == "2026-10-06T12:30:45+00:00"
    assert [m["minute"] for m in out["minutes"]] == [
        "2026-10-06T11:00:00+00:00", "2026-10-06T12:29:00+00:00"]
    m11, m1229 = out["minutes"]
    assert m11 == {"minute": "2026-10-06T11:00:00+00:00", "requests": 4,
                   "input_tokens": 300, "output_tokens": 30, "quota_tpm": 700,
                   "quota_source": "aws_estimate"}
    # Direct call reconstructed (100 + 10 x 5) plus the profile's native 500.
    assert m1229["requests"] == 5 and m1229["quota_tpm"] == 650
    assert m1229["input_tokens"] == 300 and m1229["output_tokens"] == 30
    assert m1229["quota_source"] == "mixed"
    assert out["peak"]["quota_tpm"] == {"value": 700, "at": "2026-10-06T11:00:00+00:00"}
    assert out["peak"]["rpm"] == {"value": 5, "at": "2026-10-06T12:29:00+00:00"}
    assert out["unknown_minutes"] == {"quota_tpm": 0, "requests": 0,
                                      "input_tokens": 0, "output_tokens": 0}
    assert out["quota_sources"] == {"aws_estimate": 1, "reconstructed": 0, "mixed": 1}
    assert out["identifiers_with_data"] == [MODEL, PROFILE]
    assert out["active_minutes"] == 2
    query = cw.calls[0]
    assert query["StartTime"] == at(9, 30) and query["EndTime"] == at(12, 30)
    assert {q["MetricStat"]["Period"] for q in query["MetricDataQueries"]} == {60}


def test_a_minute_that_cannot_be_established_is_unknown_and_never_the_peak():
    cw = FakeCloudWatch({MODEL: {
        "Invocations": {at(12, 0): 9, at(12, 1): 1},
        "InputTokenCount": {at(12, 1): 1000}}})
    out = live_pull.pull(live_pull.validate(event(identifiers=[MODEL])), now=NOW,
                         opener=opener(cw)[1])
    first, second = out["minutes"]
    assert first["quota_tpm"] is None and first["input_tokens"] is None
    assert second["quota_tpm"] == 1000
    assert out["unknown_minutes"]["quota_tpm"] == 1
    assert out["peak"]["quota_tpm"]["value"] == 1000
    assert out["peak"]["rpm"]["value"] == 9


def test_incomplete_cloudwatch_data_marks_the_pull_partial():
    cw = FakeCloudWatch({MODEL: {"Invocations": {at(12, 0): 1}}}, status="PartialData")
    out = live_pull.pull(live_pull.validate(event(identifiers=[MODEL])), now=NOW,
                         opener=opener(cw)[1])
    assert out["ok"] and out["status"] == "partial"
    assert out["incomplete_series"]


def test_no_traffic_is_an_empty_successful_pull():
    out = live_pull.pull(live_pull.validate(event()), now=NOW,
                         opener=opener(FakeCloudWatch({}))[1])
    assert out["ok"] and out["active_minutes"] == 0 and out["minutes"] == []
    assert out["peak"]["quota_tpm"] is None and out["identifiers_with_data"] == []


def test_many_profiles_keep_the_last_short_id_and_combine_batches_by_minute():
    ids = [MODEL]
    for i in range(30):
        short = f"p{i:011d}"
        ids += [f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/{short}", short]
    cw = FakeCloudWatch({
        MODEL: {"Invocations": {at(12, 0): 6, at(12, 1): 12},
                "EstimatedTPMQuotaUsage": {at(12, 0): 900, at(12, 1): 1400}},
        ids[-1]: {"Invocations": {at(12, 0): 7, at(12, 2): 10},
                  "EstimatedTPMQuotaUsage": {at(12, 0): 1000, at(12, 2): 1600}},
    })
    out = live_pull.pull(live_pull.validate(event(identifiers=ids)), now=NOW, opener=opener(cw)[1])
    assert out["status"] == "complete" and out["identifiers_with_data"] == [MODEL, ids[-1]]
    assert out["peak"]["rpm"] == {"value": 13, "at": at(12, 0).isoformat()}
    assert out["peak"]["quota_tpm"] == {"value": 1900, "at": at(12, 0).isoformat()}
    assert len(cw.calls) > 1
    assert all(len(c["MetricDataQueries"]) <= live_pull.IDENTIFIERS_PER_BATCH * len(METRICS)
               for c in cw.calls)


def test_unknown_contribution_in_a_later_batch_stays_unknown():
    ids = [MODEL] + [f"p{i:011d}" for i in range(25)]
    cw = FakeCloudWatch({
        MODEL: {"Invocations": {at(12, 0): 5}, "EstimatedTPMQuotaUsage": {at(12, 0): 100}},
        ids[-1]: {"Invocations": {at(12, 0): 4}},
    })
    out = live_pull.pull(live_pull.validate(event(identifiers=ids)), now=NOW, opener=opener(cw)[1])
    assert out["minutes"][0]["requests"] == 9
    assert out["minutes"][0]["quota_tpm"] is None
    assert out["unknown_minutes"]["quota_tpm"] == 1


@pytest.mark.parametrize("code,error", [
    ("AccessDenied", "no_access"), ("AccessDeniedException", "no_access"),
    ("ExpiredToken", "no_access"), ("ThrottlingException", "throttled"),
    ("Throttling", "throttled"), ("InvalidParameterValue", "failed"),
])
def test_cloudwatch_errors_map_to_safe_codes(code, error):
    cw = FakeCloudWatch({}, error=client_error(code))
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=opener(cw)[1])
    assert out["ok"] is False and out["error"] == error
    assert "must not leak" not in out["detail"]


def test_an_assume_role_denial_is_reported_as_no_access():
    def denied(account):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "AssumeRole")
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=denied)
    assert out["error"] == "no_access" and ACCOUNT in out["detail"]


def test_an_unexpected_failure_names_only_its_type():
    def broken(account):
        raise RuntimeError("a private hostname and secret values")
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=broken)
    assert out == {"ok": False, "error": "failed", "detail": "Live pull failed (RuntimeError)."}


def test_an_account_outside_the_scope_is_refused_by_name():
    def outside(account):
        raise live_pull.NotMonitored(account)
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=outside)
    assert out == {"ok": False, "error": "not_monitored", "detail":
                   f"Account {ACCOUNT} is not one of the accounts this Lens deployment monitors."}


def test_an_unknown_scope_fails_safely():
    def unknown(account):
        raise live_pull.ScopeUnavailable("AccessDeniedException")
    out = live_pull.pull(live_pull.validate(event()), now=NOW, opener=unknown)
    assert out == {"ok": False, "error": "failed", "detail":
                   "Live pull could not determine the monitored accounts (AccessDeniedException)."}


# --------------------------------------------------------------------------- #
# Opening the session exactly as the ingester does
# --------------------------------------------------------------------------- #
CENTRAL, MEMBER, STRANGER = "999999999999", ACCOUNT, "222222222222"


class FakeSessions:
    """new_session_cache() stand-in: records every assume-role request."""

    def __init__(self):
        self.self_account, self.assumed = CENTRAL, []

    def session_for(self, account, role_name, external_id):
        self.assumed.append((account, role_name, external_id))
        return FakeSession(FakeCloudWatch({}))


class FakeLambda:
    def __init__(self, env):
        self.env, self.names = env, []

    def get_function_configuration(self, FunctionName):
        self.names.append(FunctionName)
        return {"Environment": {"Variables": dict(self.env)}}


@pytest.fixture
def scope(monkeypatch):
    """Ingester settings, the session cache, Organizations and config.yaml, all faked."""
    state = {"env": {}, "org": [CENTRAL, MEMBER], "org_calls": 0,
             "config": live_pull.MonitoredAccountsConfig()}
    sessions = FakeSessions()
    monkeypatch.setenv("INGESTER_FUNCTION_NAME", "lens-ingester")
    monkeypatch.setattr(live_pull, "ingester_settings",
                        lambda client=None: _settings_from(state["env"]))
    monkeypatch.setattr(live_pull, "new_session_cache", lambda: sessions)
    monkeypatch.setattr(live_pull, "_org_cache", None)

    def org():
        state["org_calls"] += 1
        if isinstance(state["org"], Exception):
            raise state["org"]
        return [live_pull.discover_from_list(a)[0] for a in state["org"]]
    monkeypatch.setattr(live_pull, "discover_from_org", org)

    class Cfg:
        monitored_accounts = property(lambda self: state["config"])
    monkeypatch.setattr(live_pull, "load_config", lambda: Cfg())
    state["sessions"] = sessions
    return state


def _settings_from(env):
    return {k: str(env.get(k) or "").strip() for k in live_pull._INGESTER_KEYS}


def test_ingester_settings_reads_only_the_scope_keys(monkeypatch):
    monkeypatch.setenv("INGESTER_FUNCTION_NAME", "lens-ingester")
    client = FakeLambda({"MONITORED_ACCOUNTS_MODE": "explicit", "DB_SECRET_ARN": "arn:secret",
                         "BEDROCK_OPS_LENS_EXTERNAL_ID": " ext-1 "})
    settings = live_pull.ingester_settings(client)
    assert client.names == ["lens-ingester"]
    assert settings == {"MONITORED_ACCOUNTS_MODE": "explicit", "MONITORED_ACCOUNTS_IDS": "",
                        "BEDROCK_OPS_LENS_ROLE_NAME": "", "BEDROCK_OPS_LENS_EXTERNAL_ID": "ext-1"}
    monkeypatch.delenv("INGESTER_FUNCTION_NAME")
    with pytest.raises(RuntimeError):
        live_pull.ingester_settings(client)


def test_explicit_scope_uses_the_ingesters_role_and_external_id(scope):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "explicit",
                    "MONITORED_ACCOUNTS_IDS": f"{MEMBER},333333333333",
                    "BEDROCK_OPS_LENS_ROLE_NAME": "CustomReader",
                    "BEDROCK_OPS_LENS_EXTERNAL_ID": "ext-1"}
    live_pull.open_session(MEMBER)
    assert scope["sessions"].assumed == [(MEMBER, "CustomReader", "ext-1")]
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(STRANGER)
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(CENTRAL)   # explicit lists do not add the central account
    assert len(scope["sessions"].assumed) == 1


def test_an_account_outside_the_scope_never_reaches_sts(scope):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "single"}
    out = live_pull.handler(event(account_id=STRANGER), None)
    assert out["error"] == "not_monitored"
    assert scope["sessions"].assumed == []


def test_single_scope_is_the_central_account_only(scope):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "single"}
    live_pull.open_session(CENTRAL)
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)
    assert scope["sessions"].assumed == [(CENTRAL, live_pull.DEFAULT_ROLE_NAME, "")]


def test_explicit_mode_without_ids_means_the_central_account_only(scope):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "explicit"}
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)
    live_pull.open_session(CENTRAL)


def test_organization_scope_is_listed_once_per_cache_window(scope, monkeypatch):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "discover-org"}
    clock = [100.0]
    monkeypatch.setattr(live_pull.time, "monotonic", lambda: clock[0])
    live_pull.open_session(MEMBER)
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(STRANGER)
    assert scope["org_calls"] == 1
    clock[0] += live_pull.ORG_CACHE_SECONDS
    scope["org"] = [CENTRAL, MEMBER, STRANGER]
    live_pull.open_session(STRANGER)
    assert scope["org_calls"] == 2


def test_a_failed_organization_listing_falls_back_to_the_central_account(scope):
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "discover-org"}
    scope["org"] = client_error("AccessDeniedException")
    live_pull.open_session(CENTRAL)
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)


def test_without_ingester_overrides_config_yaml_decides(scope):
    scope["config"] = live_pull.MonitoredAccountsConfig(mode="explicit", ids=(MEMBER,))
    live_pull.open_session(MEMBER)
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(CENTRAL)
    # The ingester's environment overrides the baked-in file.
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "single"}
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)


def test_unreadable_ingester_settings_stop_the_pull(scope, monkeypatch):
    def denied(client=None):
        raise client_error("AccessDeniedException")
    monkeypatch.setattr(live_pull, "ingester_settings", denied)
    with pytest.raises(live_pull.ScopeUnavailable):
        live_pull.open_session(CENTRAL)
    assert scope["sessions"].assumed == []


def test_every_pull_opens_a_fresh_session_cache(scope, monkeypatch):
    """A warm container must not reuse an assumed-role session past its expiry."""
    created = []

    def fresh():
        created.append(FakeSessions())
        return created[-1]
    monkeypatch.setattr(live_pull, "new_session_cache", fresh)
    scope["env"] = {"MONITORED_ACCOUNTS_MODE": "single"}
    live_pull.handler(event(account_id=CENTRAL), None)
    live_pull.handler(event(account_id=CENTRAL), None)
    assert len(created) == 2 and all(c.assumed for c in created)


# --------------------------------------------------------------------------- #
# combine_minutes
# --------------------------------------------------------------------------- #
def test_combine_sums_identifiers_and_chooses_native_per_identifier():
    s = series(direct={"Invocations": [2, None], "InputTokenCount": [100, None],
                       "OutputTokenCount": [10, None]},
               profile={"Invocations": [3, 4], "EstimatedTPMQuotaUsage": [500, 700],
                        "InputTokenCount": [200, 300], "OutputTokenCount": [20, 30]})
    first, second = combine_minutes(["direct", "profile"], s, rate=5)
    assert first == {"minute": M0, "requests": 5, "input_tokens": 300,
                     "output_tokens": 30, "quota_tpm": 650, "quota_source": "mixed"}
    assert second["quota_tpm"] == 700 and second["quota_source"] == "aws_estimate"


def test_combine_cache_write_counts_toward_quota_and_input():
    s = series(a={"InputTokenCount": [100], "CacheWriteInputTokenCount": [50],
                  "OutputTokenCount": [10], "Invocations": [1]})
    [m] = combine_minutes(["a"], s, rate=10)
    assert m["input_tokens"] == 150 and m["quota_tpm"] == 250


@pytest.mark.parametrize("metrics,field", [
    ({"InputTokenCount": [10]}, "requests"),                 # tokens, no Invocations
    ({"Invocations": [3]}, "quota_tpm"),                     # requests, nothing else
    ({"Invocations": [3]}, "input_tokens"),
    ({"Invocations": [3], "EstimatedTPMQuotaUsage": [90]}, "output_tokens"),
])
def test_combine_reports_a_gap_as_none_not_zero(metrics, field):
    [m] = combine_minutes(["a"], series(a=metrics), rate=5)
    assert m[field] is None


def test_combine_a_gap_in_one_identifier_makes_the_combined_minute_unknown():
    s = series(a={"Invocations": [3], "InputTokenCount": [100]}, b={"Invocations": [2]})
    [m] = combine_minutes(["a", "b"], s, rate=5)
    assert m["requests"] == 5
    assert m["quota_tpm"] is None and m["input_tokens"] is None


def test_combine_an_observed_idle_minute_is_zero_not_unknown():
    [m] = combine_minutes(["a"], series(a={"Invocations": [0]}), rate=5)
    assert m["requests"] == 0 and m["quota_tpm"] == 0
    assert m["input_tokens"] == 0 and m["output_tokens"] == 0


def test_combine_skips_minutes_without_any_datapoint():
    s = series(a={"Invocations": [1, None, None, 2], "InputTokenCount": [5, None, None, 5]})
    assert len(combine_minutes(["a"], s, rate=5)) == 2


def _random_series(rng):
    ids = [f"id{i}" for i in range(rng.randint(1, 3))]
    n = rng.randint(1, 8)
    choices = (None, None, 0, 1, 7, 40, 300, 5000)
    return ids, series(**{i: {metric: [rng.choice(choices) for _ in range(n)]
                              for metric in METRICS if rng.random() < 0.8}
                          for i in ids})


def test_combine_maxima_equal_reduce_day_peaks_wherever_it_reports_one():
    """Property check over random identifier mixes, gaps and zeros."""
    rng = random.Random(20261006)
    fields = {"peak_rpm": "requests", "peak_quota_tpm": "quota_tpm",
              "peak_input_tpm": "input_tokens", "peak_output_tpm": "output_tokens"}
    checked = 0
    for _ in range(3000):
        ids, s = _random_series(rng)
        rate = rng.choice((1, 5, 10, 15))
        day = reduce_day(ids, s, rate)
        minutes = combine_minutes(ids, s, rate)
        assert len(minutes) == day["active_minutes"]
        for peak_key, field in fields.items():
            if day[peak_key] is None:
                continue
            checked += 1
            values = [m[field] for m in minutes if m[field] is not None]
            assert max(values) == day[peak_key], (s, peak_key)
        for peak_key, field in (("peak_rpm", "requests"), ("peak_quota_tpm", "quota_tpm")):
            if day[peak_key]:
                best = live_pull._peak(minutes, field)
                assert best["at"] == day[f"{peak_key}_at"].isoformat()
    assert checked > 3000
