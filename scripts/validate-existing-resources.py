#!/usr/bin/env python3
"""Validate the existing resources a Lens deployment was told to use.

deploy.sh runs this before it creates anything, whenever config.yaml (or the
first-run prompts) point Lens at a pre-existing VPC, security groups, execution
roles, or a CloudFormation service role. The checks are against the live
account, never against another stack, because the customer's network and roles
usually predate Lens by years.

A finding is an ERROR when the deployment would certainly fail or misbehave,
and a WARNING when it might. Errors make the script exit 2 and deploy.sh stops;
it never falls back to creating a replacement for a bad resource.

Input: a JSON plan on stdin (written by deploy.sh):

    {
      "network": {"vpc_id": "vpc-...", "private_subnet_ids": ["subnet-a", "subnet-b"],
                  "lambda_security_group_id": "sg-... | new",
                  "db_security_group_id": "sg-... | new",
                  "cache_security_group_id": "sg-... | new"} | null,
      "roles": {"backend": "arn | new", "ingester": ..., "schema_init": ...,
                "prewarm": ..., "pre_signup": ..., "post_confirm": ..., "edge": ...},
      "secret_arn": "actual existing secret ARN (optional)",
      "schema_dlq_arn": "actual existing schema-init DLQ ARN (optional)",
      "cloudformation_service_role_arn": "arn | none"
    }

Output: JSON facts on stdout for the deploy summary (VPC CIDR, subnet AZs,
egress facts, warnings, errors). Human-readable findings go to stderr.

Usage:
    scripts/validate-existing-resources.py --region us-east-2 \
        --stack-name BedrockOpsLens-x < plan.json
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROLE_LABELS = {
    "backend": "backend",
    "ingester": "ingester",
    "schema_init": "schema-init",
    "prewarm": "pre-warm",
    "pre_signup": "Cognito pre-sign-up",
    "post_confirm": "Cognito post-confirmation",
    "edge": "Lambda@Edge",
}
# Roles whose Lambdas run inside the VPC need ENI management permissions.
VPC_ROLES = ("backend", "ingester", "schema_init", "prewarm")
# Roles that read the Aurora secret.
SECRET_ROLES = ("backend", "ingester", "schema_init")
ENI_ACTIONS = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
               "ec2:DeleteNetworkInterface", "ec2:DescribeSubnets",
               "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"    ERROR   {msg}", file=sys.stderr)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"    WARNING {msg}", file=sys.stderr)

    def ok(self, msg: str) -> None:
        print(f"    ok      {msg}", file=sys.stderr)


def _is_id(value: str | None) -> bool:
    return bool(value) and value not in ("new", "none")


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def _describe_all(describe, key: str, **kwargs) -> list[dict]:
    items = []
    while True:
        page = describe(**kwargs)
        items.extend(page[key])
        token = page.get("NextToken")
        if not token:
            return items
        kwargs["NextToken"] = token


def check_network(net: dict, region: str, f: Findings) -> dict:
    ec2 = boto3.client("ec2", region_name=region)
    facts: dict = {"vpc_cidr": "", "subnets": [], "egress": "unknown",
                  "connectivity": "unverified"}
    vpc_id = net.get("vpc_id") or ""
    subnet_ids = [s for s in (net.get("private_subnet_ids") or []) if s]

    try:
        vpcs = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
    except ClientError as e:
        f.error(f"VPC {vpc_id}: {e.response['Error']['Message']}")
        return facts
    if not vpcs:
        f.error(f"VPC {vpc_id} not found")
        return facts
    vpc = vpcs[0]
    facts["vpc_cidr"] = vpc["CidrBlock"]
    f.ok(f"VPC {vpc_id} exists ({vpc['CidrBlock']})")
    attrs = {}
    for attr in ("enableDnsSupport", "enableDnsHostnames"):
        try:
            attrs[attr] = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attr)[
                attr[0].upper() + attr[1:]]["Value"]
        except ClientError:
            attrs[attr] = None
    if attrs.get("enableDnsSupport") is False or attrs.get("enableDnsHostnames") is False:
        f.error(f"VPC {vpc_id} must have DNS support and DNS hostnames enabled "
                "(Aurora and ElastiCache endpoints are resolved by name)")

    if len(subnet_ids) < 2:
        f.error("existing network needs at least two private subnet IDs in distinct Availability Zones")
        return facts
    try:
        subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    except ClientError as e:
        f.error(f"subnets {','.join(subnet_ids)}: {e.response['Error']['Message']}")
        return facts
    by_id = {s["SubnetId"]: s for s in subnets}
    azs = set()
    for sid in subnet_ids:
        s = by_id.get(sid)
        if s is None:
            f.error(f"subnet {sid} not found")
            continue
        if s["VpcId"] != vpc_id:
            f.error(f"subnet {sid} belongs to {s['VpcId']}, not {vpc_id}")
        azs.add(s["AvailabilityZone"])
        if s.get("AvailableIpAddressCount", 0) < 16:
            f.warn(f"subnet {sid} has only {s.get('AvailableIpAddressCount')} free addresses; "
                   "Lambda ENIs, Aurora, and ElastiCache each need some")
        if s.get("MapPublicIpOnLaunch"):
            f.warn(f"subnet {sid} auto-assigns public IPs; Lens expects private subnets")
    if len(azs) < 2:
        f.error(f"private subnets must span at least two Availability Zones (found {sorted(azs) or 'none'}); "
                "Aurora and ElastiCache subnet groups require it")

    # These are local observations, not an end-to-end connectivity test. Private
    # routes and customer DNS can reach endpoints in other VPCs or Regions.
    filters = [{"Name": "vpc-id", "Values": [vpc_id]}]
    try:
        rts = _describe_all(ec2.describe_route_tables, "RouteTables", Filters=filters)
    except (BotoCoreError, ClientError) as e:
        f.warn(f"could not inspect route tables for {vpc_id}: {e}; connectivity is unverified")
        rts = []
    main_rt = next((rt for rt in rts if any(a.get("Main") for a in rt.get("Associations", []))), None)
    try:
        endpoints = _describe_all(ec2.describe_vpc_endpoints, "VpcEndpoints", Filters=filters)
    except (BotoCoreError, ClientError) as e:
        f.warn(f"could not inspect VPC endpoints for {vpc_id}: {e}; connectivity is unverified")
        endpoints = []
    available_eps = [ep for ep in endpoints if ep.get("State") == "available"]
    for ep in endpoints:
        if ep.get("State") != "available":
            f.warn(f"endpoint {ep['VpcEndpointId']} is {ep.get('State', 'unknown')}; "
                   "it is not counted as an available connectivity path")
    s3_service = f"com.amazonaws.{region}.s3"
    s3_eps = [ep for ep in available_eps
              if ep.get("VpcEndpointType") == "Gateway" and ep["ServiceName"] == s3_service]
    s3_rt_ids = {rt for ep in s3_eps for rt in ep.get("RouteTableIds", [])}
    iface_eps = [ep for ep in available_eps if ep.get("VpcEndpointType") == "Interface"]
    iface_services = sorted({ep["ServiceName"].split(".")[-1] for ep in iface_eps})
    s3_iface_ids = [ep["VpcEndpointId"] for ep in iface_eps if ep["ServiceName"] == s3_service]
    facts["s3_interface_endpoints"] = s3_iface_ids

    egress_all = True
    private_routes_any = False
    s3_gateway_any = False
    for sid in subnet_ids:
        s = by_id.get(sid)
        if s is None:
            continue
        rt = next((r for r in rts if any(a.get("SubnetId") == sid for a in r.get("Associations", []))), main_rt)
        default_route = None
        private_routes = []
        if rt:
            for route in rt.get("Routes", []):
                if route.get("State", "active") != "active":
                    destination = (route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock")
                                   or route.get("DestinationPrefixListId"))
                    f.warn(f"subnet {sid} route to {destination} is {route.get('State')}; "
                           "it is not an active connectivity path")
                    continue
                if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                    default_route = (route.get("NatGatewayId") or route.get("GatewayId")
                                     or route.get("TransitGatewayId") or route.get("NetworkInterfaceId")
                                     or route.get("VpcPeeringConnectionId") or "yes")
                elif (any(route.get(k) for k in ("NatGatewayId", "TransitGatewayId",
                                                "NetworkInterfaceId", "VpcPeeringConnectionId",
                                                "CoreNetworkArn"))
                      or str(route.get("GatewayId", "")).startswith("vgw-")):
                    private_routes.append(route)
        else:
            f.warn(f"subnet {sid} has no observed route table association or main route table; "
                   "its routing is unverified")
        has_s3 = bool(rt and rt["RouteTableId"] in s3_rt_ids)
        if default_route and str(default_route).startswith("igw-"):
            f.warn(f"subnet {sid} routes 0.0.0.0/0 to an internet gateway; it is a public subnet. "
                   "Lambda ENIs have no public IPv4 address, so this route does not provide "
                   "public IPv4 egress. Private routes and endpoints may still provide connectivity")
            default_route = None
        egress_all &= bool(default_route)
        private_routes_any |= bool(private_routes)
        s3_gateway_any |= has_s3
        facts["subnets"].append({
            "id": sid, "az": s["AvailabilityZone"], "cidr": s["CidrBlock"],
            "route_table": rt["RouteTableId"] if rt else None,
            "default_route": str(default_route) if default_route else None,
            "s3_gateway_endpoint": has_s3,
            "private_routes": private_routes,
        })
        f.ok(f"subnet {sid} in {s['AvailabilityZone']} ({s['CidrBlock']}); "
             f"default route: {default_route or 'none'}; S3 gateway endpoint: {'yes' if has_s3 else 'no'}; "
             f"available S3 interface endpoints in VPC: {', '.join(s3_iface_ids) or 'none'}")

    if egress_all and len(facts["subnets"]) == len(subnet_ids):
        facts["egress"] = "default-route"
    elif private_routes_any:
        facts["egress"] = "private-routing"
    elif s3_gateway_any or iface_eps:
        facts["egress"] = "endpoints-only"
    else:
        facts["egress"] = "unverified"
    if facts["egress"] != "default-route":
        f.warn("at least one selected subnet has no usable IPv4 default route observed. "
               f"Available local interface endpoint services: {', '.join(iface_services) or 'none'}. "
               "Customer private routing and DNS may reach centrally hosted endpoints or other Regions; "
               "absence of a default route does not establish that those services are unreachable")
    f.warn("end-to-end connectivity is unverified by these read-only checks. Verify every selected "
           "Lambda subnet can reach the presigned S3 CloudFormation callback URL, required AWS APIs, "
           "and monitored Regions. Routes and available endpoints alone do not verify DNS "
           "(including S3 interface endpoint private DNS settings), endpoint policies/security groups, "
           "network ACLs, or upstream/return routing")

    # Security groups
    sgs_wanted = {k: net.get(k) for k in ("lambda_security_group_id", "db_security_group_id",
                                           "cache_security_group_id")}
    ids = list(dict.fromkeys(v for v in sgs_wanted.values() if _is_id(v)))
    sg_by_id: dict[str, dict] = {}
    if ids:
        try:
            for sg in ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"]:
                sg_by_id[sg["GroupId"]] = sg
        except ClientError as e:
            f.error(f"security groups {','.join(ids)}: {e.response['Error']['Message']}")
            return facts
    for key, gid in sgs_wanted.items():
        if not _is_id(gid):
            continue
        sg = sg_by_id.get(gid)
        if sg is None:
            f.error(f"security group {gid} ({key}) not found")
            continue
        if sg["VpcId"] != vpc_id:
            f.error(f"security group {gid} ({key}) belongs to {sg['VpcId']}, not {vpc_id}")
        else:
            f.ok(f"security group {gid} ({key}) exists in {vpc_id}")

    source_subnets = [ipaddress.ip_network(by_id[sid]["CidrBlock"])
                      for sid in subnet_ids if sid in by_id]
    prefix_lists: dict[str, list[ipaddress.IPv4Network] | None] = {}

    def _prefix_cidrs(prefix_id: str) -> list[ipaddress.IPv4Network] | None:
        if prefix_id not in prefix_lists:
            try:
                entries = _describe_all(ec2.get_managed_prefix_list_entries, "Entries",
                                        PrefixListId=prefix_id)
                cidrs = [ipaddress.ip_network(entry["Cidr"]) for entry in entries]
                prefix_lists[prefix_id] = [cidr for cidr in cidrs if cidr.version == 4]
            except (BotoCoreError, ClientError, KeyError, ValueError):
                # Cache failures too: the same list may appear in several rules/groups.
                prefix_lists[prefix_id] = None
                f.warn(f"could not inspect prefix list {prefix_id}; its ingress coverage is unverified")
        return prefix_lists[prefix_id]

    def _admits(sg: dict, port: int, source_sg: str | None) -> bool | None:
        allowed_cidrs = []
        unverified = False
        for perm in sg.get("IpPermissions", []):
            proto = perm.get("IpProtocol")
            if proto not in ("tcp", "6", "-1"):
                continue
            if proto != "-1" and not (perm.get("FromPort", -1) <= port <= perm.get("ToPort", -1)):
                continue
            if source_sg and any(p.get("GroupId") == source_sg for p in perm.get("UserIdGroupPairs", [])):
                return True
            for item in perm.get("IpRanges", []):
                try:
                    cidr = ipaddress.ip_network(item["CidrIp"])
                except (KeyError, ValueError):
                    continue
                if cidr.version == 4:
                    allowed_cidrs.append(cidr)
            for item in perm.get("PrefixListIds", []):
                cidrs = _prefix_cidrs(item["PrefixListId"])
                if cidrs is None:
                    unverified = True
                else:
                    allowed_cidrs.extend(cidrs)
        # Multiple rules can collectively cover a subnet (e.g. two /25s for a
        # /24). An overlap or a single host does not cover future Lambda ENIs.
        allowed_cidrs = list(ipaddress.collapse_addresses(allowed_cidrs))
        if source_subnets and all(
            any(subnet.subnet_of(cidr) for cidr in allowed_cidrs)
            for subnet in source_subnets
        ):
            return True
        return None if unverified else False

    lambda_sg = sgs_wanted["lambda_security_group_id"]
    if lambda_sg in sg_by_id and not sg_by_id[lambda_sg].get("IpPermissionsEgress"):
        f.error(f"{lambda_sg} has no outbound rules; Lambda cannot initiate database, cache, or AWS API "
                "connections. Lens never edits a supplied group")
    for key, port, label in (("db_security_group_id", 5432, "Aurora"),
                             ("cache_security_group_id", 11211, "ElastiCache")):
        gid = sgs_wanted[key]
        if _is_id(gid) and gid in sg_by_id:
            src = lambda_sg if _is_id(lambda_sg) else None
            admitted = _admits(sg_by_id[gid], port, src)
            if admitted is True:
                f.ok(f"{gid} admits TCP {port} ({label}) from the Lambda security group "
                     "or all selected Lambda subnet CIDRs")
            elif admitted is None:
                f.warn(f"{gid} ingress for TCP {port} ({label}) is unverified because a referenced "
                       "prefix list could not be inspected; verify coverage of all selected Lambda "
                       "subnet CIDRs")
            else:
                f.error(f"{gid} ingress for TCP {port} ({label}) does not admit "
                        f"{src or 'the new Lambda security group'} or cover all selected Lambda subnet "
                        "CIDRs. Lens never edits a supplied group; Lambda ENIs in uncovered ranges "
                        "cannot reach the database/cache. Add the rule or let Lens create this group")
    return facts


# ---------------------------------------------------------------------------
# IAM roles
# ---------------------------------------------------------------------------
def _trusts(role: dict, service: str) -> bool:
    doc = role.get("AssumeRolePolicyDocument") or {}
    for st in doc.get("Statement", []):
        if st.get("Effect") != "Allow":
            continue
        svc = (st.get("Principal") or {}).get("Service")
        if svc is None:
            continue
        if isinstance(svc, str):
            svc = [svc]
        if service in svc:
            return True
    return False


def _simulate(iam, role_arn: str, actions: list[str], resources: list[str] | None,
              f: Findings, what: str, hard: bool, *, region: str) -> None:
    # These checks simulate regional EC2, Secrets Manager, SQS and Cognito calls.
    # Other condition values (tags, source endpoints, etc.) are not inferred.
    kwargs = dict(PolicySourceArn=role_arn, ActionNames=actions, ContextEntries=[{
        "ContextKeyName": "aws:RequestedRegion",
        "ContextKeyValues": [region],
        "ContextKeyType": "string",
    }])
    if resources:
        kwargs["ResourceArns"] = resources
    try:
        res = iam.simulate_principal_policy(**kwargs)["EvaluationResults"]
    except (BotoCoreError, ClientError) as e:
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
        f.warn(f"could not simulate {what} for {role_arn.rsplit('/', 1)[-1]} ({code}); "
               "permissions are unverified")
        return
    denied = []
    unverified = False
    for result in res:
        details = result.get("ResourceSpecificResults", [])
        top_missing = set(result.get("MissingContextValues", []))
        missing = top_missing.union(*(set(d.get("MissingContextValues", [])) for d in details))
        explicit_deny = result["EvalDecision"] == "explicitDeny" or any(
            d.get("EvalResourceDecision") == "explicitDeny" for d in details
        )
        # An incomplete result for one resource must not hide a complete denial
        # for another resource in the same action's aggregate result.
        complete_deny = (result["EvalDecision"] == "implicitDeny" and not missing) or any(
            d.get("EvalResourceDecision") == "implicitDeny"
            and not (top_missing or d.get("MissingContextValues"))
            for d in details
        )
        if explicit_deny or complete_deny:
            denied.append(result["EvalActionName"])
        elif missing and (result["EvalDecision"] == "implicitDeny" or any(
            d.get("EvalResourceDecision") == "implicitDeny" for d in details
        )):
            unverified = True
            f.warn(f"{role_arn.rsplit('/', 1)[-1]}: {result['EvalActionName']} ({what}) is unverified; "
                   f"simulation is missing context: {', '.join(sorted(missing))}")
    if denied:
        msg = f"{role_arn.rsplit('/', 1)[-1]} is not allowed {', '.join(denied)} ({what})"
        (f.error if hard else f.warn)(msg)
    elif not unverified:
        f.ok(f"{role_arn.rsplit('/', 1)[-1]}: {what}")


def check_roles(roles: dict, region: str, partition: str, account: str, stack: str, f: Findings,
                *, secret_arn: str | None = None, schema_dlq_arn: str | None = None) -> None:
    iam = boto3.client("iam")
    # Existing stacks may have random resource names. For new stacks, use a
    # representative secret suffix to simulate policies covering <stack>/db-*.
    secret_arn = secret_arn or f"arn:{partition}:secretsmanager:{region}:{account}:secret:{stack}/db-AbCdEf"
    schema_dlq_arn = schema_dlq_arn or f"arn:{partition}:sqs:{region}:{account}:{stack}-schema-init-dlq"
    for key, arn in roles.items():
        if not _is_id(arn):
            continue
        label = ROLE_LABELS.get(key, key)
        name = arn.rsplit("/", 1)[-1]
        try:
            role = iam.get_role(RoleName=name)["Role"]
        except ClientError as e:
            f.error(f"{label} role {arn}: {e.response['Error']['Message']}")
            continue
        if role["Arn"] != arn:
            f.error(f"{label} role: {arn} resolved to {role['Arn']} (path mismatch?)")
            continue
        if not _trusts(role, "lambda.amazonaws.com"):
            f.error(f"{label} role {name} does not trust lambda.amazonaws.com")
        if key == "edge" and not _trusts(role, "edgelambda.amazonaws.com"):
            f.error(f"{label} role {name} must also trust edgelambda.amazonaws.com (Lambda@Edge requirement)")
        if key == "edge" and not (_trusts(role, "lambda.amazonaws.com") and _trusts(role, "edgelambda.amazonaws.com")):
            continue
        f.ok(f"{label} role {name} exists and trusts the Lambda service")
        if key in VPC_ROLES:
            _simulate(iam, arn, ENI_ACTIONS, None, f,
                      "VPC access (AWSLambdaVPCAccessExecutionRole or equivalent)", hard=True, region=region)
        if key in SECRET_ROLES:
            _simulate(iam, arn, ["secretsmanager:GetSecretValue"], [secret_arn], f,
                      f"read secret {secret_arn}", hard=True, region=region)
        if key == "schema_init":
            _simulate(iam, arn, ["sqs:SendMessage"], [schema_dlq_arn], f,
                      f"sqs:SendMessage on {schema_dlq_arn} (Lambda checks this at creation)",
                      hard=True, region=region)
        if key == "post_confirm":
            _simulate(iam, arn, ["cognito-idp:AdminAddUserToGroup"],
                      [f"arn:{partition}:cognito-idp:{region}:{account}:userpool/{region}_AbCdEfGhI"], f,
                      "add the first user to the admin group", hard=False, region=region)


def check_cfn_role(arn: str, f: Findings) -> None:
    iam = boto3.client("iam")
    name = arn.rsplit("/", 1)[-1]
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except ClientError as e:
        f.error(f"CloudFormation service role {arn}: {e.response['Error']['Message']}")
        return
    if not _trusts(role, "cloudformation.amazonaws.com"):
        f.error(f"CloudFormation service role {name} does not trust cloudformation.amazonaws.com")
    else:
        f.ok(f"CloudFormation service role {name} exists and trusts CloudFormation")


def check_waf(arn: str, f: Findings) -> None:
    # CLOUDFRONT-scope web ACLs live in us-east-1: arn:...:global/webacl/<name>/<id>
    try:
        _, rest = arn.split(":global/webacl/", 1)
        name, acl_id = rest.split("/", 1)
    except ValueError:
        f.error(f"web ACL ARN {arn} is not a CLOUDFRONT-scope WAFv2 ARN")
        return
    waf = boto3.client("wafv2", region_name="us-east-1")
    try:
        acl = waf.get_web_acl(Name=name, Scope="CLOUDFRONT", Id=acl_id)["WebACL"]
    except ClientError as e:
        f.error(f"web ACL {arn}: {e.response['Error']['Message']}")
        return
    f.ok(f"web ACL {acl['Name']} exists (CLOUDFRONT scope, {len(acl.get('Rules', []))} rules)")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", required=True)
    ap.add_argument("--stack-name", required=True, help="main stack name, used for secret/queue patterns")
    ap.add_argument("--partition", default="aws")
    args = ap.parse_args()

    plan = json.load(sys.stdin)
    f = Findings()
    facts: dict = {"network": None, "warnings": [], "errors": [], "ok": False}
    try:
        account = boto3.client("sts").get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: cannot read caller identity: {e}", file=sys.stderr)
        return 2

    net = plan.get("network")
    if net and _is_id(net.get("vpc_id")):
        print("  network", file=sys.stderr)
        facts["network"] = check_network(net, args.region, f)
    roles = plan.get("roles") or {}
    if any(_is_id(v) for v in roles.values()):
        print("  roles", file=sys.stderr)
        check_roles(roles, args.region, args.partition, account, args.stack_name, f,
                    secret_arn=plan.get("secret_arn"), schema_dlq_arn=plan.get("schema_dlq_arn"))
    cfn_role = plan.get("cloudformation_service_role_arn")
    if _is_id(cfn_role):
        print("  cloudformation service role", file=sys.stderr)
        check_cfn_role(cfn_role, f)
    waf = plan.get("waf") or "new"
    if waf.startswith("arn:"):
        print("  waf", file=sys.stderr)
        check_waf(waf, f)

    facts["warnings"] = f.warnings
    facts["errors"] = f.errors
    facts["ok"] = not f.errors
    print(json.dumps(facts))
    return 0 if not f.errors else 2


if __name__ == "__main__":
    sys.exit(main())
