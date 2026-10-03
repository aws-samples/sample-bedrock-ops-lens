"""AIP lookup tests; all AWS responses are stubbed."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.quota_match import resolve_quota
from app.proxy_quota import resolve as resolve_proxy_quota
from ingestion import cw_metrics, inference_profiles as ip

ACCOUNT = "111111111111"
REGION = "us-east-1"
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"


def profile(pid="abcdef123456", *, account=ACCOUNT, region=REGION,
            destinations=(REGION,)):
    return {
        "inferenceProfileId": pid,
        "inferenceProfileArn": f"arn:aws:bedrock:{region}:{account}:application-inference-profile/{pid}",
        "inferenceProfileName": "Support assistant",
        "type": "APPLICATION",
        "status": "ACTIVE",
        "models": [{"modelArn": f"arn:aws:bedrock:{r}::foundation-model/{MODEL}"}
                   for r in destinations],
    }


@pytest.mark.parametrize("destinations", [
    (REGION,), (REGION, "us-west-2"), (REGION, "eu-west-1", "ap-northeast-1"),
])
def test_all_destination_arns_resolve_once_without_inventing_route(destinations):
    row = ip.profile_record(profile(destinations=destinations), ACCOUNT, REGION)
    assert row[5] == MODEL
    assert row[7] == sorted(destinations)
    assert row[2] == "abcdef123456"


@pytest.mark.parametrize("mutation", [
    {"models": []},
    {"models": [{"modelArn": "not-a-foundation-model-arn"}]},
    {"models": profile()["models"] + [
        {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"}]},
])
def test_incomplete_or_multi_model_profile_is_not_guessed(mutation):
    data = profile()
    data.update(mutation)
    assert ip.profile_record(data, ACCOUNT, REGION)[5] is None


@pytest.mark.parametrize("data", [
    profile(account="222222222222"),
    profile(region="eu-west-1"),
    dict(profile(), type="SYSTEM_DEFINED"),
    dict(profile(), inferenceProfileId="wrong1234567"),
])
def test_wrong_owner_region_or_identity_is_rejected(data):
    with pytest.raises(ValueError):
        ip.profile_record(data, ACCOUNT, REGION)


def test_pagination_and_get_fallback_use_documented_sdk_shapes():
    client = boto3.client("bedrock", region_name=REGION,
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    first, second, late = profile(), profile("bbbbbb123456"), profile("cccccc123456")
    with Stubber(client) as stub:
        stub.add_response("list_inference_profiles", {
            "inferenceProfileSummaries": [first], "nextToken": "page2",
        }, {"typeEquals": "APPLICATION", "maxResults": 1000})
        stub.add_response("list_inference_profiles", {
            "inferenceProfileSummaries": [second],
        }, {"typeEquals": "APPLICATION", "maxResults": 1000, "nextToken": "page2"})
        stub.add_response("get_inference_profile", late, {
            "inferenceProfileIdentifier": late["inferenceProfileArn"]})
        rows, warnings = ip.read_profiles(client, ACCOUNT, REGION, {
            first["inferenceProfileId"], first["inferenceProfileArn"],
            late["inferenceProfileArn"], late["inferenceProfileId"], MODEL,
        })
        stub.assert_no_pending_responses()
    assert len(rows) == 3
    assert {r[5] for r in rows} == {MODEL}
    assert warnings == []


def test_partial_list_does_not_return_a_partial_catalog():
    client = Mock()
    client.list_inference_profiles.side_effect = [
        {"inferenceProfileSummaries": [profile()], "nextToken": "more"},
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListInferenceProfiles"),
    ]
    with pytest.raises(ClientError):
        ip.read_profiles(client, ACCOUNT, REGION, set())


def test_repeated_page_token_is_a_failed_refresh():
    client = Mock()
    client.list_inference_profiles.return_value = {
        "inferenceProfileSummaries": [], "nextToken": "same"}
    with pytest.raises(ValueError, match="pagination token"):
        ip.read_profiles(client, ACCOUNT, REGION, set())
    assert client.list_inference_profiles.call_count == 2


def test_get_is_deduped_and_wrong_account_or_region_is_never_requested():
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    client.get_inference_profile.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetInferenceProfile")
    missing = profile()
    rows, warnings = ip.read_profiles(client, ACCOUNT, REGION, {
        missing["inferenceProfileId"], missing["inferenceProfileArn"],
        profile(account="222222222222")["inferenceProfileArn"],
        profile(region="eu-west-1")["inferenceProfileArn"],
        MODEL, "us." + MODEL, "__unknown__",
    })
    assert rows == [] and warnings == []
    assert client.get_inference_profile.call_count == 1


def test_get_access_denial_is_reported():
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    client.get_inference_profile.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "GetInferenceProfile")
    rows, warnings = ip.read_profiles(client, ACCOUNT, REGION, {"abcdef123456"})
    assert rows == []
    assert warnings == ["GetInferenceProfile: AccessDeniedException"]


def test_aip_never_borrows_on_demand_quota_even_if_it_is_the_only_candidate():
    quotas = [{
        "accountid": ACCOUNT, "region": REGION, "model_name": "Claude Sonnet 4.5",
        "traffic_type": "On-demand", "metric": "TPM", "applied_value": 10000,
    }]
    direct = resolve_quota(quotas, ACCOUNT, REGION, MODEL, family_hint="On-demand")
    aip = resolve_quota(quotas, ACCOUNT, REGION, MODEL, family_hint="On-demand",
                        routing_unknown=True)
    assert direct.value == 10000
    assert aip.value is None and aip.routing_unknown and aip.ambiguous
    assert len(aip.candidates) == 1
    proxy = resolve_proxy_quota(quotas, MODEL, REGION, ACCOUNT, routing_unknown=True)
    assert proxy.limit is None and proxy.routing_unknown
    assert "routing family" in proxy.reason


def test_cloudwatch_dimension_copies_are_not_added_to_model_aggregate():
    client = Mock()
    model_dim = [{"Name": "ModelId", "Value": MODEL}]
    client.get_paginator.return_value.paginate.return_value = [{
        "Metrics": [
            {"Dimensions": model_dim},
            {"Dimensions": model_dim + [{"Name": "InferenceProfileId", "Value": "abcdef123456"}]},
            {"Dimensions": model_dim + [{"Name": "ContextWindow", "Value": "200K"}]},
        ],
    }]
    models = cw_metrics._list_models(client)
    for build in (cw_metrics._build_daily_queries, cw_metrics._build_hourly_queries,
                  cw_metrics._build_latency_queries):
        queries, _ = build(models)
        invocations = [q for q in queries
                       if q["MetricStat"]["Metric"]["MetricName"] == "Invocations"]
        assert len(invocations) <= 1
        assert all(q["MetricStat"]["Metric"]["Dimensions"] == model_dim for q in queries)
