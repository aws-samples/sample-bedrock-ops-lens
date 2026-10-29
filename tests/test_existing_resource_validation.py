"""Existing-resource validation uses only mocked AWS reads and policy simulation."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from botocore.exceptions import ClientError, EndpointConnectionError
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate-existing-resources.py"
SPEC = importlib.util.spec_from_file_location("existing_resource_validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

REGION = "us-east-2"
ACCOUNT = "123456789012"
READ_METHODS = {
    "ec2": ("describe_vpcs", "describe_vpc_attribute", "describe_subnets",
            "describe_route_tables", "describe_vpc_endpoints", "describe_security_groups",
            "get_managed_prefix_list_entries"),
    "iam": ("get_role", "simulate_principal_policy"),
    "sts": ("get_caller_identity",),
    "wafv2": ("get_web_acl",),
}


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    # A mutation or an unexpected service fails immediately instead of reaching AWS.
    clients = {service: Mock(spec_set=methods) for service, methods in READ_METHODS.items()}
    factory = Mock(side_effect=lambda service, **kwargs: clients[service])
    monkeypatch.setattr(validator.boto3, "client", factory)
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    yield SimpleNamespace(factory=factory, **clients)
    for service, client in clients.items():
        assert all(call[0] in READ_METHODS[service] for call in client.method_calls)


def ingress(port, *, cidrs=(), source_sg=None, protocol="tcp", prefix_ids=()):
    rule = {"IpProtocol": protocol, "FromPort": port, "ToPort": port,
            "IpRanges": [{"CidrIp": cidr} for cidr in cidrs]}
    if source_sg:
        rule["UserIdGroupPairs"] = [{"GroupId": source_sg}]
    if prefix_ids:
        rule["PrefixListIds"] = [{"PrefixListId": prefix_id} for prefix_id in prefix_ids]
    return rule


def endpoint(kind, **overrides):
    return {
        "VpcEndpointId": f"vpce-s3-{kind.lower()}", "VpcEndpointType": kind,
        "VpcId": "vpc-lens", "ServiceName": f"com.amazonaws.{REGION}.s3",
        "State": "available", **overrides,
    }


@pytest.fixture
def network(aws):
    plan = {
        "vpc_id": "vpc-lens", "private_subnet_ids": ["subnet-a", "subnet-b"],
        "lambda_security_group_id": "sg-lambda", "db_security_group_id": "sg-db",
        "cache_security_group_id": "sg-cache",
    }
    subnets = [
        {"SubnetId": sid, "VpcId": "vpc-lens", "AvailabilityZone": az,
         "CidrBlock": cidr, "AvailableIpAddressCount": 100, "MapPublicIpOnLaunch": False}
        for sid, az, cidr in (
            ("subnet-a", f"{REGION}a", "10.60.1.0/24"),
            ("subnet-b", f"{REGION}b", "10.60.2.0/24"),
        )
    ]
    groups = [
        {"GroupId": "sg-lambda", "VpcId": "vpc-lens", "IpPermissions": [],
         "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
        {"GroupId": "sg-db", "VpcId": "vpc-lens",
         "IpPermissions": [ingress(5432, source_sg="sg-lambda")]},
        {"GroupId": "sg-cache", "VpcId": "vpc-lens",
         "IpPermissions": [ingress(11211, source_sg="sg-lambda")]},
    ]
    data = {
        "vpcs": [{"VpcId": "vpc-lens", "CidrBlock": "10.60.0.0/16"}],
        "dns": {"enableDnsSupport": True, "enableDnsHostnames": True},
        "subnets": subnets, "groups": groups, "endpoints": [],
        "route_tables": [{
            "RouteTableId": "rtb-main", "Associations": [{"Main": True}],
            "Routes": [{"DestinationCidrBlock": "10.60.0.0/16", "GatewayId": "local",
                        "State": "active"}],
        }],
    }

    def attribute(**kwargs):
        attr = kwargs["Attribute"]
        return {attr[0].upper() + attr[1:]: {"Value": data["dns"][attr]}}

    aws.ec2.describe_vpcs.side_effect = lambda **kw: {"Vpcs": data["vpcs"]}
    aws.ec2.describe_vpc_attribute.side_effect = attribute
    aws.ec2.describe_subnets.side_effect = lambda **kw: {"Subnets": data["subnets"]}
    aws.ec2.describe_route_tables.side_effect = lambda **kw: {"RouteTables": data["route_tables"]}
    aws.ec2.describe_vpc_endpoints.side_effect = lambda **kw: {"VpcEndpoints": data["endpoints"]}
    aws.ec2.describe_security_groups.side_effect = lambda **kw: {
        "SecurityGroups": [group for group in data["groups"] if group["GroupId"] in kw["GroupIds"]]
    }

    def check():
        before = deepcopy((plan, data))
        findings = validator.Findings()
        facts = validator.check_network(plan, REGION, findings)
        # Check both the plan and the supplied AWS objects, including SG rules.
        assert (plan, data) == before
        return facts, findings

    return SimpleNamespace(plan=plan, data=data, check=check)


@pytest.mark.parametrize("target", [
    {"NatGatewayId": "nat-example"}, {"TransitGatewayId": "tgw-central"},
    {"NetworkInterfaceId": "eni-firewall"},
])
def test_default_routes_remain_supported_but_connectivity_is_unverified(network, target):
    network.data["route_tables"][0]["Routes"].append({
        "DestinationCidrBlock": "0.0.0.0/0", "State": "active", **target,
    })
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "default-route"
    assert facts["connectivity"] == "unverified"
    assert any("end-to-end connectivity is unverified" in warning for warning in findings.warnings)


def test_s3_gateway_endpoint_without_default_route(network):
    network.data["endpoints"] = [endpoint("Gateway", RouteTableIds=["rtb-main"])]
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "endpoints-only"
    assert all(subnet["s3_gateway_endpoint"] for subnet in facts["subnets"])
    assert all(subnet["route_table"] == "rtb-main" for subnet in facts["subnets"])


@pytest.mark.parametrize("dns_options", [
    {"PrivateDnsEnabled": True, "DnsOptions": {"PrivateDnsOnlyForInboundResolverEndpoint": False}},
    {"PrivateDnsEnabled": True, "DnsOptions": {"PrivateDnsOnlyForInboundResolverEndpoint": True}},
    {"PrivateDnsEnabled": False},
])
def test_s3_interface_endpoint_without_default_route_is_not_rejected(network, dns_options):
    # An endpoint in another subnet of this VPC may still be reachable.
    network.data["endpoints"] = [endpoint("Interface", SubnetIds=["subnet-endpoint"], **dns_options)]
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "endpoints-only"
    assert facts["s3_interface_endpoints"] == ["vpce-s3-interface"]
    assert not any(subnet["s3_gateway_endpoint"] for subnet in facts["subnets"])
    assert any("private DNS settings" in warning for warning in findings.warnings)


@pytest.mark.parametrize("target", [
    {"TransitGatewayId": "tgw-central"}, {"VpcPeeringConnectionId": "pcx-central"},
    {"NetworkInterfaceId": "eni-router"}, {"GatewayId": "vgw-customer"},
    {"CoreNetworkArn": "arn:aws:networkmanager::123456789012:core-network/core-network-example"},
])
def test_centrally_routed_endpoints_without_local_endpoints_or_default(network, target):
    route = {"DestinationCidrBlock": "10.80.0.0/16", "State": "active", **target}
    network.data["route_tables"][0]["Routes"].append(route)
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "private-routing"
    assert all(subnet["private_routes"] == [route] for subnet in facts["subnets"])
    assert any("centrally hosted endpoints or other Regions" in warning for warning in findings.warnings)
    assert not any("ingestion is limited" in warning for warning in findings.warnings)


def test_no_visible_path_is_unverified_instead_of_a_claimed_failure(network):
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "unverified"
    assert any("presigned S3 CloudFormation callback URL" in warning for warning in findings.warnings)


def test_gateway_endpoint_association_does_not_apply_to_other_route_tables(network):
    network.data["route_tables"].append({
        "RouteTableId": "rtb-b", "Associations": [{"SubnetId": "subnet-b"}], "Routes": [],
    })
    network.data["endpoints"] = [endpoint("Gateway", RouteTableIds=["rtb-main"])]
    facts, findings = network.check()
    assert findings.errors == []
    assert [subnet["s3_gateway_endpoint"] for subnet in facts["subnets"]] == [True, False]
    assert facts["subnets"][1]["route_table"] == "rtb-b"
    assert facts["connectivity"] == "unverified"


@pytest.mark.parametrize("state", ["pending", "failed", "deleted"])
def test_unavailable_endpoints_are_not_counted_as_available_paths(network, state):
    network.data["endpoints"] = [
        endpoint("Gateway", State=state, RouteTableIds=["rtb-main"]),
        endpoint("Interface", State=state),
    ]
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "unverified"
    assert facts["s3_interface_endpoints"] == []
    assert not any(subnet["s3_gateway_endpoint"] for subnet in facts["subnets"])
    assert any(f"is {state}" in warning for warning in findings.warnings)


def test_blackhole_routes_are_warned_about_and_not_counted(network):
    network.data["route_tables"][0]["Routes"].extend([
        {"DestinationCidrBlock": "0.0.0.0/0", "State": "blackhole", "NatGatewayId": "nat-gone"},
        {"DestinationCidrBlock": "10.80.0.0/16", "State": "blackhole", "TransitGatewayId": "tgw-gone"},
    ])
    facts, findings = network.check()
    assert facts["egress"] == "unverified"
    assert all(subnet["default_route"] is None and not subnet["private_routes"]
               for subnet in facts["subnets"])
    assert any("blackhole" in warning for warning in findings.warnings)


def test_igw_cannot_supply_lambda_ipv4_egress_but_private_endpoints_can(network):
    network.data["route_tables"][0]["Routes"].append({
        "DestinationCidrBlock": "0.0.0.0/0", "State": "active", "GatewayId": "igw-example",
    })
    network.data["endpoints"] = [endpoint("Interface")]
    facts, findings = network.check()
    assert findings.errors == []
    assert facts["egress"] == "endpoints-only"
    assert all(subnet["default_route"] is None for subnet in facts["subnets"])
    assert any("does not provide public IPv4 egress" in warning for warning in findings.warnings)
    assert not any("NO outbound connectivity" in warning for warning in findings.warnings)


def test_paginated_reads_find_main_route_table_and_s3_interface_endpoint(network, aws):
    aws.ec2.describe_route_tables.side_effect = [
        {"RouteTables": [], "NextToken": "routes-next"},
        {"RouteTables": network.data["route_tables"]},
    ]
    aws.ec2.describe_vpc_endpoints.side_effect = [
        {"VpcEndpoints": [], "NextToken": "endpoints-next"},
        {"VpcEndpoints": [endpoint("Interface")]},
    ]
    facts, findings = network.check()
    assert findings.errors == []
    assert all(subnet["route_table"] == "rtb-main" for subnet in facts["subnets"])
    assert facts["s3_interface_endpoints"] == ["vpce-s3-interface"]
    for method, token in ((aws.ec2.describe_route_tables, "routes-next"),
                          (aws.ec2.describe_vpc_endpoints, "endpoints-next")):
        first, second = method.call_args_list
        assert second.kwargs == {**first.kwargs, "NextToken": token}


@pytest.mark.parametrize("method", ["describe_route_tables", "describe_vpc_endpoints"])
def test_unreadable_routing_is_warned_without_skipping_definite_sg_errors(network, aws, method):
    getattr(aws.ec2, method).side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "fixture denied"}}, method,
    )
    network.data["groups"][1]["IpPermissions"] = []
    facts, findings = network.check()
    assert facts["connectivity"] == "unverified"
    assert any("could not inspect" in warning for warning in findings.warnings)
    assert any("sg-db" in error for error in findings.errors)


@pytest.mark.parametrize("attr", ["enableDnsSupport", "enableDnsHostnames"])
def test_disabled_vpc_dns_remains_a_hard_error(network, attr):
    network.data["dns"][attr] = False
    _, findings = network.check()
    assert any("must have DNS support and DNS hostnames enabled" in error for error in findings.errors)


@pytest.mark.parametrize("fault,expected", [
    ("missing-vpc", "VPC vpc-lens not found"),
    ("one-subnet", "at least two private subnet IDs"),
    ("one-az", "at least two Availability Zones"),
    ("missing-subnet", "subnet subnet-b not found"),
    ("wrong-vpc", "belongs to vpc-other"),
])
def test_invalid_local_network_structure_remains_a_hard_error(network, fault, expected):
    if fault == "missing-vpc":
        network.data["vpcs"] = []
    elif fault == "one-subnet":
        network.plan["private_subnet_ids"] = ["subnet-a"]
    elif fault == "one-az":
        network.data["subnets"][1]["AvailabilityZone"] = f"{REGION}a"
    elif fault == "missing-subnet":
        network.data["subnets"].pop()
    else:
        network.data["subnets"][1]["VpcId"] = "vpc-other"
    _, findings = network.check()
    assert any(expected in error for error in findings.errors)


@pytest.mark.parametrize("cidrs,admitted", [
    (["0.0.0.0/0"], True),
    (["10.60.0.0/16"], True),
    (["10.60.1.0/24", "10.60.2.0/24"], True),
    (["10.60.1.0/25", "10.60.1.128/25", "10.60.2.0/24"], True),
    (["192.0.2.0/24"], False),
    (["10.60.3.0/24"], False),
    (["10.60.1.0/24"], False),
    (["10.60.1.0/25", "10.60.2.0/24"], False),
    (["10.60.1.10/32", "10.60.2.10/32"], False),
    (["malformed-cidr"], False),
])
@pytest.mark.parametrize("lambda_sg", ["sg-lambda", "new"])
def test_ingress_requires_full_selected_subnet_coverage(network, cidrs, admitted, lambda_sg):
    network.plan["lambda_security_group_id"] = lambda_sg
    for group, port in zip(network.data["groups"][1:], (5432, 11211)):
        # Separate rules must be combined as well as separate ranges in one rule.
        group["IpPermissions"] = [ingress(port, cidrs=[cidr]) for cidr in cidrs]
    _, findings = network.check()
    assert (not findings.errors) is admitted
    if not admitted:
        assert len(findings.errors) == 2
        assert all("cover all selected Lambda subnet CIDRs" in error for error in findings.errors)


@pytest.mark.parametrize("protocol,port,admitted", [
    ("tcp", 5432, True), ("6", 5432, True), ("-1", None, True),
    ("udp", 5432, False), ("tcp", 5433, False),
])
@pytest.mark.parametrize("source", ["sg", "cidr"])
def test_ingress_protocol_and_port_must_match(network, protocol, port, admitted, source):
    options = {"source_sg": "sg-lambda"} if source == "sg" else {"cidrs": ["10.60.0.0/16"]}
    rule = ingress(port, protocol=protocol, **options)
    if protocol == "-1":
        rule.pop("FromPort")
        rule.pop("ToPort")
    network.data["groups"][1]["IpPermissions"] = [rule]
    _, findings = network.check()
    assert (not findings.errors) is admitted


def test_matching_actual_lambda_group_is_sufficient_despite_unrelated_cidr(network):
    network.data["groups"][1]["IpPermissions"] = [
        ingress(5432, source_sg="sg-lambda", cidrs=["192.0.2.0/24"]),
    ]
    _, findings = network.check()
    assert findings.errors == []


@pytest.mark.parametrize("lambda_sg", ["sg-lambda", "new"])
def test_other_group_and_ipv6_do_not_admit_lambda_ipv4_sources(network, lambda_sg):
    network.plan["lambda_security_group_id"] = lambda_sg
    rule = ingress(5432, source_sg="sg-unrelated")
    rule["Ipv6Ranges"] = [{"CidrIpv6": "::/0"}]
    network.data["groups"][1]["IpPermissions"] = [rule]
    _, findings = network.check()
    assert any("sg-db" in error for error in findings.errors)


def test_subnet_coverage_uses_selected_subnets_in_secondary_vpc_cidr(network):
    for subnet, cidr in zip(network.data["subnets"], ("172.16.1.0/24", "172.16.2.0/24")):
        subnet["CidrBlock"] = cidr
    for group, port in zip(network.data["groups"][1:], (5432, 11211)):
        group["IpPermissions"] = [ingress(port, cidrs=["172.16.0.0/16"])]
    _, findings = network.check()
    assert findings.errors == []


@pytest.mark.parametrize("lambda_sg", ["sg-lambda", "new"])
def test_prefix_list_pages_combine_with_cidrs_and_are_cached_across_groups(network, aws, lambda_sg):
    network.plan["lambda_security_group_id"] = lambda_sg
    for group, port in zip(network.data["groups"][1:], (5432, 11211)):
        group["IpPermissions"] = [
            ingress(port, cidrs=["10.60.1.128/25"], prefix_ids=["pl-shared", "pl-shared"]),
        ]
    aws.ec2.get_managed_prefix_list_entries.side_effect = [
        {"Entries": [{"Cidr": "10.60.1.0/25"}], "NextToken": "page-2"},
        {"Entries": [], "NextToken": "page-3"},
        {"Entries": [{"Cidr": "10.60.2.0/24"}, {"Cidr": "2001:db8::/32"}]},
    ]
    _, findings = network.check()
    assert findings.errors == []
    assert not any("prefix list" in warning for warning in findings.warnings)
    assert aws.ec2.get_managed_prefix_list_entries.call_args_list == [
        call(PrefixListId="pl-shared"),
        call(PrefixListId="pl-shared", NextToken="page-2"),
        call(PrefixListId="pl-shared", NextToken="page-3"),
    ]


def test_distinct_prefix_lists_collectively_cover_selected_subnets(network, aws):
    network.data["groups"][1]["IpPermissions"] = [
        ingress(5432, prefix_ids=["pl-a"]),
        ingress(5432, prefix_ids=["pl-b"]),
    ]
    aws.ec2.get_managed_prefix_list_entries.side_effect = lambda **kw: {
        "Entries": [{"Cidr": {"pl-a": "10.60.1.0/24", "pl-b": "10.60.2.0/24"}[kw["PrefixListId"]]}],
    }
    _, findings = network.check()
    assert findings.errors == []
    assert aws.ec2.get_managed_prefix_list_entries.call_count == 2


@pytest.mark.parametrize("cidrs", [
    [], ["192.0.2.0/24"], ["10.60.1.0/24"],
    ["10.60.1.0/25", "10.60.2.0/24"], ["2001:db8::/32"],
])
def test_readable_prefix_lists_without_full_ipv4_coverage_remain_hard_errors(network, aws, cidrs):
    for group, port in zip(network.data["groups"][1:], (5432, 11211)):
        group["IpPermissions"] = [ingress(port, prefix_ids=["pl-noncovering"])]
    aws.ec2.get_managed_prefix_list_entries.return_value = {
        "Entries": [{"Cidr": cidr} for cidr in cidrs],
    }
    _, findings = network.check()
    assert len(findings.errors) == 2
    assert all("cover all selected Lambda subnet CIDRs" in error for error in findings.errors)
    assert not any("prefix list" in warning for warning in findings.warnings)
    aws.ec2.get_managed_prefix_list_entries.assert_called_once_with(PrefixListId="pl-noncovering")


@pytest.mark.parametrize("transport_error", [False, True])
@pytest.mark.parametrize("fail_after_first_page", [False, True])
def test_unreadable_prefix_lists_warn_and_cache_failures(
    network, aws, transport_error, fail_after_first_page,
):
    for group, port in zip(network.data["groups"][1:], (5432, 11211)):
        group["IpPermissions"] = [ingress(port, prefix_ids=["pl-unreadable", "pl-unreadable"])]
    error = (EndpointConnectionError(endpoint_url="https://ec2.example.invalid") if transport_error
             else ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "fixture denied"}},
                              "GetManagedPrefixListEntries"))
    pages = [{"Entries": [{"Cidr": "10.60.1.0/24"}], "NextToken": "page-2"}]
    aws.ec2.get_managed_prefix_list_entries.side_effect = (pages if fail_after_first_page else []) + [error]
    _, findings = network.check()
    assert findings.errors == []
    assert sum("could not inspect prefix list pl-unreadable" in w for w in findings.warnings) == 1
    for group, port in (("sg-db", 5432), ("sg-cache", 11211)):
        assert any(f"{group} ingress for TCP {port}" in w and "unverified" in w for w in findings.warnings)
    assert aws.ec2.get_managed_prefix_list_entries.call_count == (2 if fail_after_first_page else 1)


@pytest.mark.parametrize("protocol,port", [("udp", 5432), ("tcp", 22)])
def test_irrelevant_prefix_list_rule_cannot_mask_missing_ingress(network, aws, protocol, port):
    network.data["groups"][1]["IpPermissions"] = [
        ingress(port, protocol=protocol, prefix_ids=["pl-unreadable"]),
    ]
    _, findings = network.check()
    assert any("sg-db" in error for error in findings.errors)
    aws.ec2.get_managed_prefix_list_entries.assert_not_called()


def test_unreadable_prefix_list_does_not_mask_another_groups_missing_ingress(network, aws):
    network.data["groups"][1]["IpPermissions"] = [ingress(5432, prefix_ids=["pl-unreadable"])]
    network.data["groups"][2]["IpPermissions"] = []
    aws.ec2.get_managed_prefix_list_entries.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "fixture denied"}},
        "GetManagedPrefixListEntries",
    )
    _, findings = network.check()
    assert len(findings.errors) == 1
    assert "sg-cache" in findings.errors[0]
    assert any("sg-db ingress" in w and "unverified" in w for w in findings.warnings)


def test_known_cidr_coverage_is_sufficient_alongside_unreadable_prefix_list(network, aws, capsys):
    network.data["groups"][1]["IpPermissions"] = [
        ingress(5432, cidrs=["10.60.0.0/16"], prefix_ids=["pl-unreadable"]),
    ]
    aws.ec2.get_managed_prefix_list_entries.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "fixture denied"}},
        "GetManagedPrefixListEntries",
    )
    _, findings = network.check()
    assert findings.errors == []
    assert any("pl-unreadable" in w and "unverified" in w for w in findings.warnings)
    assert "sg-db admits TCP 5432" in capsys.readouterr().err


@pytest.mark.parametrize("fault,expected", [
    ("missing", "security group sg-db (db_security_group_id) not found"),
    ("all-missing", "not found"),
    ("wrong-vpc", "belongs to vpc-other"),
    ("no-egress", "has no outbound rules"),
])
def test_definite_security_group_errors_are_preserved(network, fault, expected):
    if fault == "missing":
        network.data["groups"].pop(1)
    elif fault == "all-missing":
        network.data["groups"] = []
    elif fault == "wrong-vpc":
        network.data["groups"][1]["VpcId"] = "vpc-other"
    else:
        network.data["groups"][0]["IpPermissionsEgress"] = []
    _, findings = network.check()
    assert any(expected in error for error in findings.errors)


def test_new_security_groups_require_no_group_reads(network, aws):
    for key in ("lambda_security_group_id", "db_security_group_id", "cache_security_group_id"):
        network.plan[key] = "new"
    _, findings = network.check()
    assert findings.errors == []
    aws.ec2.describe_security_groups.assert_not_called()


def configure_role(aws, denied=(), role_name="prewarm"):
    arn = f"arn:aws:iam::{ACCOUNT}:role/customer/{role_name}"
    aws.iam.get_role.return_value = {"Role": {
        "Arn": arn, "AssumeRolePolicyDocument": {"Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }]},
    }}
    aws.iam.simulate_principal_policy.side_effect = lambda **kw: {
        "EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": "implicitDeny" if action in denied else "allowed"}
            for action in kw["ActionNames"]
        ],
    }
    return arn


def test_vpc_role_simulation_includes_all_lambda_eni_permissions(aws):
    arn = configure_role(aws)
    findings = validator.Findings()
    validator.check_roles({"prewarm": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings)
    assert findings.errors == []
    request = aws.iam.simulate_principal_policy.call_args.kwargs
    assert request["PolicySourceArn"] == arn
    assert set(request["ActionNames"]) == {
        "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
        "ec2:DeleteNetworkInterface", "ec2:DescribeSubnets",
        "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
    }
    assert "ResourceArns" not in request


@pytest.mark.parametrize("role", ["prewarm", "schema_init", "post_confirm"])
def test_regional_condition_uses_deployment_region_for_every_simulated_service(aws, role):
    arn = configure_role(aws, role_name=role)

    def simulate(**kwargs):
        assert kwargs["ContextEntries"] == [{
            "ContextKeyName": "aws:RequestedRegion", "ContextKeyValues": [REGION], "ContextKeyType": "string",
        }]
        return {"EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": "allowed"} for action in kwargs["ActionNames"]
        ]}

    aws.iam.simulate_principal_policy.side_effect = simulate
    findings = validator.Findings()
    validator.check_roles({role: arn}, REGION, "aws", ACCOUNT, "Lens-test", findings)
    assert findings.errors == []
    assert findings.warnings == []
    assert aws.iam.simulate_principal_policy.call_count == (3 if role == "schema_init" else 1)


@pytest.mark.parametrize("result,missing", [
    ({"EvalDecision": "implicitDeny", "MissingContextValues": ["aws:SourceVpce"]}, ["aws:SourceVpce"]),
    ({"EvalDecision": "implicitDeny", "ResourceSpecificResults": [{
        "EvalResourceDecision": "implicitDeny", "MissingContextValues": ["aws:ResourceTag/Application"],
    }]}, ["aws:ResourceTag/Application"]),
    ({"EvalDecision": "implicitDeny", "MissingContextValues": ["aws:SourceVpce"],
      "ResourceSpecificResults": [{
          "EvalResourceDecision": "implicitDeny", "MissingContextValues": ["aws:ResourceTag/Application"],
      }]}, ["aws:SourceVpce", "aws:ResourceTag/Application"]),
])
def test_incomplete_implicit_denials_are_unverified_at_both_result_levels(aws, capsys, result, missing):
    action = "secretsmanager:GetSecretValue"
    aws.iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [{"EvalActionName": action, **result}],
    }
    findings = validator.Findings()
    validator._simulate(aws.iam, f"arn:aws:iam::{ACCOUNT}:role/customer/backend", [action],
                        ["arn:aws:secretsmanager:us-east-2:123456789012:secret:example-AbCdEf"],
                        findings, "read secret", hard=True, region=REGION)
    assert findings.errors == []
    assert len(findings.warnings) == 1
    assert action in findings.warnings[0] and "unverified" in findings.warnings[0]
    assert all(key in findings.warnings[0] for key in missing)
    assert "    ok " not in capsys.readouterr().err


@pytest.mark.parametrize("result", [
    {"EvalDecision": "implicitDeny"},
    {"EvalDecision": "implicitDeny", "ResourceSpecificResults": [{"EvalResourceDecision": "implicitDeny"}]},
    {"EvalDecision": "explicitDeny", "MissingContextValues": ["aws:SourceVpce"]},
    {"EvalDecision": "explicitDeny", "ResourceSpecificResults": [{
        "EvalResourceDecision": "explicitDeny", "MissingContextValues": ["aws:SourceVpce"],
    }]},
    {"EvalDecision": "implicitDeny", "ResourceSpecificResults": [
        {"EvalResourceDecision": "implicitDeny", "MissingContextValues": ["aws:SourceVpce"]},
        {"EvalResourceDecision": "implicitDeny"},
    ]},
    {"EvalDecision": "explicitDeny", "ResourceSpecificResults": [
        {"EvalResourceDecision": "implicitDeny", "MissingContextValues": ["aws:SourceVpce"]},
        {"EvalResourceDecision": "explicitDeny"},
    ]},
])
def test_complete_or_explicit_denials_remain_hard_errors_even_with_incomplete_resources(aws, result):
    action = "secretsmanager:GetSecretValue"
    aws.iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [{"EvalActionName": action, **result}],
    }
    findings = validator.Findings()
    validator._simulate(aws.iam, f"arn:aws:iam::{ACCOUNT}:role/customer/backend", [action], ["*"],
                        findings, "read secret", hard=True, region=REGION)
    assert len(findings.errors) == 1
    assert action in findings.errors[0]


def test_incomplete_action_does_not_hide_complete_denial_for_another_action(aws):
    arn = configure_role(aws)

    def simulate(**kwargs):
        return {"EvaluationResults": [
            {"EvalActionName": "ec2:CreateNetworkInterface", "EvalDecision": "implicitDeny",
             "MissingContextValues": ["aws:SourceVpce"]},
            {"EvalActionName": "ec2:DescribeSubnets", "EvalDecision": "implicitDeny"},
            *[{"EvalActionName": action, "EvalDecision": "allowed"} for action in kwargs["ActionNames"]
              if action not in ("ec2:CreateNetworkInterface", "ec2:DescribeSubnets")],
        ]}

    aws.iam.simulate_principal_policy.side_effect = simulate
    findings = validator.Findings()
    validator.check_roles({"prewarm": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings)
    assert len(findings.errors) == 1 and "ec2:DescribeSubnets" in findings.errors[0]
    assert "ec2:CreateNetworkInterface" not in findings.errors[0]
    assert len(findings.warnings) == 1 and "ec2:CreateNetworkInterface" in findings.warnings[0]


@pytest.mark.parametrize("transport_error", [False, True])
def test_inaccessible_iam_simulation_is_unverified(aws, transport_error):
    arn = configure_role(aws)
    aws.iam.simulate_principal_policy.side_effect = (
        EndpointConnectionError(endpoint_url="https://iam.example.invalid") if transport_error
        else ClientError({"Error": {"Code": "AccessDenied", "Message": "fixture denied"}},
                         "SimulatePrincipalPolicy")
    )
    findings = validator.Findings()
    validator.check_roles({"prewarm": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings)
    assert findings.errors == []
    assert len(findings.warnings) == 1 and "unverified" in findings.warnings[0]


@pytest.mark.parametrize("denied", [
    "ec2:DescribeSubnets", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
])
def test_missing_required_eni_permission_remains_a_hard_error(aws, denied):
    arn = configure_role(aws, denied=[denied])
    findings = validator.Findings()
    validator.check_roles({"prewarm": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings)
    assert any(denied in error for error in findings.errors)


@pytest.mark.parametrize("role", ["backend", "ingester", "schema_init"])
def test_existing_resource_arns_are_used_for_resource_scoped_role_permissions(aws, role):
    arn = configure_role(aws, role_name=role)
    secret = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:old-stack-AuroraSecret-random-AbCdEf"
    dlq = f"arn:aws:sqs:{REGION}:{ACCOUNT}:old-stack-SchemaInitDLQ-random"
    allowed_resources = {
        "secretsmanager:GetSecretValue": [secret],
        "sqs:SendMessage": [dlq],
    }

    def simulate(**kwargs):
        return {"EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": (
                "allowed" if action not in allowed_resources
                or kwargs.get("ResourceArns") == allowed_resources[action] else "implicitDeny"
            )}
            for action in kwargs["ActionNames"]
        ]}

    aws.iam.simulate_principal_policy.side_effect = simulate
    findings = validator.Findings()
    validator.check_roles({role: arn}, REGION, "aws", ACCOUNT, "Lens-test", findings,
                          secret_arn=secret, schema_dlq_arn=dlq)
    assert findings.errors == []
    resource_calls = [call.kwargs["ResourceArns"] for call in aws.iam.simulate_principal_policy.call_args_list
                      if "ResourceArns" in call.kwargs]
    assert resource_calls == ([[secret], [dlq]] if role == "schema_init" else [[secret]])


@pytest.mark.parametrize("denied_action,resource_name", [
    ("secretsmanager:GetSecretValue", "old-secret"),
    ("sqs:SendMessage", "old-queue"),
])
def test_denied_existing_resource_does_not_fall_back_to_a_different_resource(
    aws, denied_action, resource_name,
):
    arn = configure_role(aws, denied=[denied_action], role_name="schema_init")
    findings = validator.Findings()
    validator.check_roles(
        {"schema_init": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings,
        secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:old-secret-AbCdEf",
        schema_dlq_arn=f"arn:aws:sqs:{REGION}:{ACCOUNT}:old-queue",
    )
    assert any(denied_action in error and resource_name in error for error in findings.errors)
    requests = [call.kwargs for call in aws.iam.simulate_principal_policy.call_args_list
                if denied_action in call.kwargs["ActionNames"]]
    assert len(requests) == 1
    assert resource_name in requests[0]["ResourceArns"][0]


@pytest.mark.parametrize("supplied", [{}, {"secret_arn": "existing"}, {"schema_dlq_arn": "existing"}])
def test_resource_arn_fallbacks_are_independent(aws, supplied):
    arn = configure_role(aws, role_name="schema_init")
    existing = {
        "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:legacy-secret-AbCdEf",
        "schema_dlq_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:legacy-dlq",
    }
    defaults = {
        "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:Lens-test/db-AbCdEf",
        "schema_dlq_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:Lens-test-schema-init-dlq",
    }
    overrides = {key: existing[key] for key in supplied}
    findings = validator.Findings()
    validator.check_roles({"schema_init": arn}, REGION, "aws", ACCOUNT, "Lens-test", findings, **overrides)
    assert findings.errors == []
    resource_calls = [call.kwargs["ResourceArns"] for call in aws.iam.simulate_principal_policy.call_args_list
                      if "ResourceArns" in call.kwargs]
    assert resource_calls == [
        [overrides.get("secret_arn", defaults["secret_arn"])],
        [overrides.get("schema_dlq_arn", defaults["schema_dlq_arn"])],
    ]


@pytest.mark.parametrize("invalid,expected_exit", [(False, 0), (True, 2)])
@pytest.mark.parametrize("existing_resources", [False, True])
def test_main_reports_warnings_or_errors_without_mutating_supplied_groups(
    network, aws, monkeypatch, capsys, invalid, expected_exit, existing_resources,
):
    network.data["endpoints"] = [endpoint("Interface")]
    if invalid:
        network.data["groups"][1]["IpPermissions"] = [ingress(5432, cidrs=["192.0.2.0/24"])]
    before = deepcopy(network.data)
    plan = {"network": network.plan, "roles": {"schema_init": configure_role(aws, role_name="schema_init")},
            "cloudformation_service_role_arn": "none", "waf": "none"}
    if existing_resources:
        plan.update(
            secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:old-stack-secret-AbCdEf",
            schema_dlq_arn=f"arn:aws:sqs:{REGION}:{ACCOUNT}:old-stack-dlq",
        )
    monkeypatch.setattr(validator.sys, "stdin", io.StringIO(json.dumps(plan)))
    monkeypatch.setattr(validator.sys, "argv", [
        str(SCRIPT), "--region", REGION, "--stack-name", "Lens-test",
    ])
    assert validator.main() == expected_exit
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is (not invalid)
    assert bool(output["errors"]) is invalid
    assert output["warnings"]
    assert output["network"]["s3_interface_endpoints"] == ["vpce-s3-interface"]
    assert network.data == before
    assert {call.args[0] for call in aws.factory.call_args_list} == {"ec2", "iam", "sts"}
    resource_calls = [call.kwargs["ResourceArns"] for call in aws.iam.simulate_principal_policy.call_args_list
                      if "ResourceArns" in call.kwargs]
    assert resource_calls == [
        [plan.get("secret_arn", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:Lens-test/db-AbCdEf")],
        [plan.get("schema_dlq_arn", f"arn:aws:sqs:{REGION}:{ACCOUNT}:Lens-test-schema-init-dlq")],
    ]
