#!/usr/bin/env bash
# ============================================================================
# Existing-resource choices for deploy.sh (sourced, not executed).
#
# Decides, for this deployment, whether Lens creates its network and IAM roles
# or uses ones the customer already has. The decision is made ONCE, on the
# first deployment of a stack, from one of:
#
#   1. the `existing_resources:` block in config.yaml        (non-interactive)
#   2. first-run prompts, when the block is absent and a terminal is attached
#   3. EXISTING_RESOURCES=new, or no terminal: create everything (historical)
#
# Rules the prompts and the config loader both enforce:
#   - Optional blank values create that resource on a first deployment.
#     Required identifiers for a selected existing VPC are still required.
#     On updates, omitted choices keep their previous values.
#   - Supplied resources are validated against the live account
#     (scripts/validate-existing-resources.py) BEFORE anything is created. A
#     bad ID or an under-privileged role stops the deployment; Lens never
#     silently falls back to creating a replacement.
#   - The plan is printed as a table and, at a terminal, confirmed with a typed
#     `yes` before CloudFormation is called.
#   - On later runs against the same stack, the network choice is read back
#     from the stack and may not change (Aurora and ElastiCache would be
#     replaced). Role ARNs may change.
#
# Inputs (set by deploy.sh before sourcing):
#   ROOT REGION ACCOUNT_ID MAIN_STACK ACTION
# Outputs (consumed by deploy.sh):
#   ER_VPC_ID ER_VPC_CIDR ER_SUBNET_IDS ER_LAMBDA_SG ER_DB_SG ER_CACHE_SG
#   ER_ROLE_BACKEND ER_ROLE_INGESTER ER_ROLE_SCHEMA_INIT ER_ROLE_PREWARM
#   ER_ROLE_PRE_SIGNUP ER_ROLE_POST_CONFIRM ER_ROLE_EDGE ER_CFN_ROLE
#   ER_USE_PREVIOUS   (1 = update an existing stack without new choices)
#   ER_CFN_ROLE_ARGS  (array: --role-arn <arn>, or empty)
#   ER_EDGE_PARAMS    (array: EdgeRoleArn=<arn>, or empty)
#   ER_NETWORK_MODE   existing|created
#   ER_WAF            new | none | <web ACL ARN> | previous (keep the stack's)
# Values are "" (create), an ID/ARN (use), or for ER_CFN_ROLE "" (none).
# Works with bash 3.2 (macOS default): no associative arrays, no mapfile.
# ============================================================================

ER_PY="$ROOT/.venv/bin/python"
[[ -x "$ER_PY" ]] || ER_PY="python3"

ER_VPC_ID=""; ER_VPC_CIDR=""; ER_SUBNET_IDS=""
ER_LAMBDA_SG=""; ER_DB_SG=""; ER_CACHE_SG=""
ER_ROLE_BACKEND=""; ER_ROLE_INGESTER=""; ER_ROLE_SCHEMA_INIT=""; ER_ROLE_PREWARM=""
ER_ROLE_PRE_SIGNUP=""; ER_ROLE_POST_CONFIRM=""; ER_ROLE_EDGE=""; ER_CFN_ROLE=""
ER_WAF="new"            # new | none | <CLOUDFRONT-scope WebACL ARN>
ER_USE_PREVIOUS=0; ER_SOURCE=""; ER_NETWORK_MODE="created"
ER_CFN_ROLE_ARGS=(); ER_EDGE_PARAMS=()
ER_FACTS_FILE=""
ER_PRIOR_PARAMS=""
ER_PRIOR_EDGE_ROLE=""
ER_PRIOR_EDGE_WAF="true"
ER_STABLE_NAMES="false"
ER_SECRET_ARN=""; ER_SCHEMA_DLQ_ARN=""
ER_STACK_FILE=""

_ER_RX_VPC='^vpc-[0-9a-f]{8,17}$'
_ER_RX_SUBNETS='^subnet-[0-9a-f]{8,17}(,subnet-[0-9a-f]{8,17})+$'
_ER_RX_SG_OR_NEW='^(sg-[0-9a-f]{8,17}|new)$'
_ER_RX_ARN='^arn:[a-z0-9-]+:iam::[0-9]{12}:role/.+$'
_ER_RX_ARN_OR_NEW='^(arn:[a-z0-9-]+:iam::[0-9]{12}:role/.+|new)$'
_ER_RX_ARN_OR_NONE='^(arn:[a-z0-9-]+:iam::[0-9]{12}:role/.+|none)$'
_ER_RX_WAF='^(new|none|arn:[a-z0-9-]+:wafv2:us-east-1:[0-9]{12}:global/webacl/[^/]+/[0-9a-fA-F-]+)$'

# "new"/"none" in config or at a prompt become "" for the template.
_er_norm() { case "$1" in new|none) echo "";; *) echo "$1";; esac; }

# ---------------------------------------------------------------------------
# 1. config.yaml
# ---------------------------------------------------------------------------
_er_read_config() {
    local out
    out="$(mktemp -t bol-er.XXXXXX)"
    if ! "$ER_PY" -c 'import yaml' 2>/dev/null; then
        echo "ERROR: reading existing_resources from config.yaml needs PyYAML in $ER_PY" >&2
        echo "       (pip install pyyaml, or create the .venv described in CONTRIBUTING.md)" >&2
        exit 1
    fi
    if ! "$ER_PY" - "$ROOT/config.yaml" "$ER_PRIOR_PARAMS" \
            "$ER_PRIOR_EDGE_ROLE" "$ER_PRIOR_EDGE_WAF" > "$out" <<'PY'
import re, sys, yaml
path = sys.argv[1]
previous = dict(line.split("=", 1) for line in sys.argv[2].splitlines() if "=" in line)
has_previous = bool(previous)
previous_edge_role, previous_edge_waf = sys.argv[3:]
raw = yaml.safe_load(open(path)) or {}
blk = raw.get("existing_resources")
errors = []
RX = {
    "vpc": r"^vpc-[0-9a-f]{8,17}$", "subnet": r"^subnet-[0-9a-f]{8,17}$",
    "sg": r"^sg-[0-9a-f]{8,17}$", "arn": r"^arn:[a-z0-9-]+:iam::[0-9]{12}:role/.+$",
}
out = {"ER_PRESENT": "0"}
def emit():
    for k, v in out.items():
        assert "'" not in str(v)
        print(f"{k}='{v}'")
if blk is None:
    emit(); sys.exit(0)
out["ER_PRESENT"] = "1"
if not isinstance(blk, dict):
    errors.append("existing_resources must be a mapping (see config.example.yaml)")
    blk = {}

previous_network = "new"
if previous.get("ExistingVpcId"):
    previous_network = {
        "vpc_id": previous["ExistingVpcId"],
        "private_subnet_ids": previous.get("ExistingPrivateSubnetIds", ""),
    }
sg_params = {"lambda": "ExistingLambdaSecurityGroupId",
             "db": "ExistingDbSecurityGroupId", "cache": "ExistingCacheSecurityGroupId"}
net = blk.get("network") or previous_network
sg_keys = ("lambda_security_group_id", "db_security_group_id", "cache_security_group_id")
if net == "new":
    out["ER_NET"] = "new"
elif isinstance(net, dict):
    out["ER_NET"] = "existing"
    vpc = str(net.get("vpc_id") or previous.get("ExistingVpcId") or "").strip()
    if not re.match(RX["vpc"], vpc):
        errors.append(f"existing_resources.network.vpc_id must be a vpc-… ID (got {vpc!r})")
    out["ER_VPC_ID"] = vpc
    subs = net.get("private_subnet_ids") or previous.get("ExistingPrivateSubnetIds")
    if isinstance(subs, str):
        subs = [s.strip() for s in subs.split(",") if s.strip()]
    if not isinstance(subs, list) or len(subs) < 2 or not all(
            isinstance(s, str) and re.match(RX["subnet"], s) for s in subs):
        errors.append("existing_resources.network.private_subnet_ids must list two or more subnet-… IDs "
                      f"(got {subs!r})")
        subs = []
    out["ER_SUBNET_IDS"] = ",".join(subs)
    for key in sg_keys:
        v = net.get(key) or previous.get(sg_params[key.split("_security")[0]]) or "new"
        if v == "new":
            out["ER_" + key.split("_security")[0].upper() + "_SG"] = ""
        elif isinstance(v, str) and re.match(RX["sg"], v.strip()):
            out["ER_" + key.split("_security")[0].upper() + "_SG"] = v.strip()
        else:
            errors.append(f"existing_resources.network.{key} must be an sg-… ID or 'new' (got {v!r})")
else:
    errors.append("existing_resources.network must be 'new' or a mapping")

roles = blk.get("roles") or {}
role_keys = ("backend", "ingester", "schema_init", "prewarm", "pre_signup", "post_confirm", "edge")
role_params = dict(zip(role_keys, ("BackendRoleArn", "IngesterRoleArn", "SchemaInitRoleArn",
                                  "PrewarmRoleArn", "PreSignUpRoleArn", "PostConfirmRoleArn",
                                  "EdgeRoleArn")))
previous["EdgeRoleArn"] = previous_edge_role
if roles == "new":
    for k in role_keys:
        out["ER_ROLE_" + k.upper()] = ""
elif isinstance(roles, dict):
    for k in role_keys:
        v = roles.get(k) or previous.get(role_params[k]) or "new"
        if v == "new":
            out["ER_ROLE_" + k.upper()] = ""
        elif isinstance(v, str) and re.match(RX["arn"], v.strip()):
            out["ER_ROLE_" + k.upper()] = v.strip()
        else:
            errors.append(f"existing_resources.roles.{k} must be an IAM role ARN or 'new' (got {v!r})")
    extra = sorted(set(roles) - set(role_keys))
    if extra:
        errors.append(f"existing_resources.roles has unknown keys: {', '.join(extra)}")
else:
    errors.append("existing_resources.roles must be 'new' or a mapping")

previous_waf = ("new" if previous_edge_waf == "true" else previous.get("WebAclArn") or "none")
waf = blk.get("waf") or (previous_waf if has_previous else "new")
if waf in (None, "new"):
    out["ER_WAF"] = "new"
elif waf == "none":
    out["ER_WAF"] = "none"
elif isinstance(waf, str) and re.match(r"^arn:[a-z0-9-]+:wafv2:us-east-1:[0-9]{12}:global/webacl/[^/]+/[0-9a-fA-F-]+$", waf.strip()):
    out["ER_WAF"] = waf.strip()
else:
    errors.append(f"existing_resources.waf must be 'new', 'none', or a CLOUDFRONT-scope WAFv2 web ACL ARN in us-east-1 (got {waf!r})")

cfn = blk.get("cloudformation_service_role_arn", "none")
if cfn in (None, "none", ""):
    out["ER_CFN_ROLE"] = ""
elif isinstance(cfn, str) and re.match(RX["arn"], cfn.strip()):
    out["ER_CFN_ROLE"] = cfn.strip()
else:
    errors.append(f"existing_resources.cloudformation_service_role_arn must be an IAM role ARN or 'none' (got {cfn!r})")

if errors:
    print("ERROR: config.yaml existing_resources is incomplete. Nothing was created.", file=sys.stderr)
    for e in errors:
        print(f"       - {e}", file=sys.stderr)
    print("       Optional blanks create resources on first deployment and preserve them on updates; supplied IDs must be valid.", file=sys.stderr)
    sys.exit(2)
emit()
PY
    then
        rm -f "$out"
        exit 2
    fi
    # shellcheck disable=SC1090
    source "$out"
    rm -f "$out"
}

# ---------------------------------------------------------------------------
# 2. Prompts (first deployment, terminal attached, no config block)
# ---------------------------------------------------------------------------
_er_ask() {   # $1 prompt  $2 ERE  $3 var  $4 hint  [$5 blank default]
    local tries=0 ans
    while :; do
        read -r -p "$1" ans
        ans="$(printf '%s' "$ans" | tr -d '[:space:]')"
        ans="${ans:-${5:-}}"
        if [[ -n "$ans" && "$ans" =~ $2 ]]; then
            eval "$3=\"\$ans\""
            return 0
        fi
        tries=$((tries + 1))
        if (( tries >= 3 )); then
            echo "ERROR: no valid value after 3 attempts. Nothing was created." >&2
            echo "       Supply the required identifier for the existing resource." >&2
            exit 2
        fi
        echo "      $4"
    done
}

_er_prompt() {
    local mode
    echo
    echo "Resource choices — first deployment of $MAIN_STACK"
    echo "  Lens can create its own network and IAM roles, or use ones you already have."
    echo "  Your answers are validated against account $ACCOUNT_ID and saved to config.yaml."
    echo
    echo "Networking"
    _er_ask "  Use existing networking resources, or create new? [existing/new; Enter=new]: " '^(existing|new)$' mode \
        "type 'existing' or 'new'" new
    if [[ "$mode" == "existing" ]]; then
        _er_ask "    VPC ID: " "$_ER_RX_VPC" ER_VPC_ID "a VPC ID is required, e.g. vpc-0123456789abcdef0"
        _er_ask "    Private subnet IDs (2 or more, comma-separated): " "$_ER_RX_SUBNETS" ER_SUBNET_IDS \
            "two or more subnet IDs in distinct Availability Zones, e.g. subnet-aaa,subnet-bbb"
        _er_ask "    Lambda security group ID [Enter=new]: " "$_ER_RX_SG_OR_NEW" ER_LAMBDA_SG "an sg-… ID or the word new" new
        _er_ask "    Database security group ID, or 'new' (must admit TCP 5432 from the Lambda group): " \
            "$_ER_RX_SG_OR_NEW" ER_DB_SG "an sg-… ID or the word new" new
        _er_ask "    Cache security group ID, or 'new' (must admit TCP 11211 from the Lambda group): " \
            "$_ER_RX_SG_OR_NEW" ER_CACHE_SG "an sg-… ID or the word new" new
        ER_LAMBDA_SG="$(_er_norm "$ER_LAMBDA_SG")"; ER_DB_SG="$(_er_norm "$ER_DB_SG")"; ER_CACHE_SG="$(_er_norm "$ER_CACHE_SG")"
    fi
    echo
    echo "IAM roles"
    echo "  'existing' or 'some' = supply any ARNs you have; Enter creates that role;"
    echo "  'new' = Lens creates all of them. infra/examples/existing-execution-roles.yaml"
    echo "  lists the permissions each role needs."
    _er_ask "  Use existing IAM roles? [existing/new/some; Enter=new]: " '^(existing|new|some)$' mode \
        "type 'existing', 'new' or 'some'" new
    if [[ "$mode" != "new" ]]; then
        local rx hint
        rx="$_ER_RX_ARN_OR_NEW"; hint="an IAM role ARN or Enter to create"
        local suffix=" [Enter=new]"
        _er_ask "    Backend role ARN$suffix: "               "$rx" ER_ROLE_BACKEND "$hint" new
        _er_ask "    Ingester role ARN$suffix: "              "$rx" ER_ROLE_INGESTER "$hint" new
        _er_ask "    Schema-init role ARN$suffix: "           "$rx" ER_ROLE_SCHEMA_INIT "$hint" new
        _er_ask "    Pre-warm role ARN$suffix: "              "$rx" ER_ROLE_PREWARM "$hint" new
        _er_ask "    Cognito pre-sign-up role ARN$suffix: "   "$rx" ER_ROLE_PRE_SIGNUP "$hint" new
        _er_ask "    Cognito post-confirm role ARN$suffix: "  "$rx" ER_ROLE_POST_CONFIRM "$hint" new
        _er_ask "    Lambda@Edge role ARN$suffix (must also trust edgelambda.amazonaws.com): " "$rx" ER_ROLE_EDGE "$hint" new
        ER_ROLE_BACKEND="$(_er_norm "$ER_ROLE_BACKEND")"; ER_ROLE_INGESTER="$(_er_norm "$ER_ROLE_INGESTER")"
        ER_ROLE_SCHEMA_INIT="$(_er_norm "$ER_ROLE_SCHEMA_INIT")"; ER_ROLE_PREWARM="$(_er_norm "$ER_ROLE_PREWARM")"
        ER_ROLE_PRE_SIGNUP="$(_er_norm "$ER_ROLE_PRE_SIGNUP")"; ER_ROLE_POST_CONFIRM="$(_er_norm "$ER_ROLE_POST_CONFIRM")"
        ER_ROLE_EDGE="$(_er_norm "$ER_ROLE_EDGE")"
    fi
    echo
    _er_ask "  CloudFormation service role ARN for the stacks, or 'none': " "$_ER_RX_ARN_OR_NONE" ER_CFN_ROLE \
        "an IAM role ARN that trusts cloudformation.amazonaws.com, or the word none" none
    ER_CFN_ROLE="$(_er_norm "$ER_CFN_ROLE")"
    echo
    echo "Web application firewall"
    echo "  'new' creates an AWS WAF web ACL with the AWS managed rule sets in front of"
    echo "  CloudFront; 'none' deploys without a web ACL (for example when WAF is managed"
    echo "  centrally); or paste an existing CLOUDFRONT-scope web ACL ARN (us-east-1)."
    _er_ask "  WAF for CloudFront [new/none/<web ACL ARN>; Enter=new]: " "$_ER_RX_WAF" ER_WAF \
        "type new, none, or an arn:aws:wafv2:us-east-1:...:global/webacl/... ARN" new
    ER_SOURCE="prompt"
}

# ---------------------------------------------------------------------------
# 3. Validation against the account
# ---------------------------------------------------------------------------
_er_any_existing() {
    [[ -n "$ER_VPC_ID$ER_ROLE_BACKEND$ER_ROLE_INGESTER$ER_ROLE_SCHEMA_INIT$ER_ROLE_PREWARM$ER_ROLE_PRE_SIGNUP$ER_ROLE_POST_CONFIRM$ER_ROLE_EDGE$ER_CFN_ROLE" ]] \
        || [[ "$ER_WAF" == arn:* ]]
}

_er_plan_json() {
    "$ER_PY" - "$ER_VPC_ID" "$ER_SUBNET_IDS" "$ER_LAMBDA_SG" "$ER_DB_SG" "$ER_CACHE_SG" \
        "$ER_ROLE_BACKEND" "$ER_ROLE_INGESTER" "$ER_ROLE_SCHEMA_INIT" "$ER_ROLE_PREWARM" \
        "$ER_ROLE_PRE_SIGNUP" "$ER_ROLE_POST_CONFIRM" "$ER_ROLE_EDGE" "$ER_CFN_ROLE" "$ER_WAF" \
        "$ER_SECRET_ARN" "$ER_SCHEMA_DLQ_ARN" <<'PY'
import json, sys
(vpc, subs, lsg, dsg, csg, rb, ri, rs, rp, rps, rpc, re_, cfn, waf, secret, dlq) = sys.argv[1:]
new = lambda v: v or "new"
plan = {
    "network": ({"vpc_id": vpc, "private_subnet_ids": [s for s in subs.split(",") if s],
                 "lambda_security_group_id": new(lsg), "db_security_group_id": new(dsg),
                 "cache_security_group_id": new(csg)} if vpc else None),
    "roles": {"backend": new(rb), "ingester": new(ri), "schema_init": new(rs), "prewarm": new(rp),
              "pre_signup": new(rps), "post_confirm": new(rpc), "edge": new(re_)},
    "cloudformation_service_role_arn": cfn or "none",
    "waf": waf or "new",
    "secret_arn": secret,
    "schema_dlq_arn": dlq,
}
print(json.dumps(plan))
PY
}

_er_validate() {
    _er_any_existing || return 0
    echo
    echo "Validating supplied resources against account $ACCOUNT_ID ($REGION)..."
    ER_FACTS_FILE="$(mktemp -t bol-er-facts.XXXXXX)"
    if ! _er_plan_json | "$ER_PY" "$ROOT/scripts/validate-existing-resources.py" \
            --region "$REGION" --stack-name "$MAIN_STACK" --partition "${PARTITION:-aws}" > "$ER_FACTS_FILE"; then
        echo
        echo "ERROR: existing-resource validation failed. Nothing was created." >&2
        echo "       Fix the items marked ERROR above (or change the value to 'new'), then re-run." >&2
        exit 2
    fi
    if [[ -n "$ER_VPC_ID" ]]; then
        ER_VPC_CIDR="$("$ER_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["vpc_cidr"])' "$ER_FACTS_FILE")"
    fi
}

# ---------------------------------------------------------------------------
# 4. Summary + confirmation
# ---------------------------------------------------------------------------
_er_row() {   # $1 label  $2 value ("" = create)  [$3 detail]
    if [[ -n "$2" ]]; then printf '    %-22s EXISTING    %s %s\n' "$1" "$2" "${3:-}"
    else                   printf '    %-22s CREATE NEW\n' "$1"; fi
}

_er_summary() {
    local detail=""
    echo
    echo "Resource plan for $MAIN_STACK"
    if [[ -n "$ER_VPC_ID" ]]; then
        _er_row "VPC" "$ER_VPC_ID" "($ER_VPC_CIDR)"
        if [[ -n "$ER_FACTS_FILE" ]]; then
            detail="$("$ER_PY" - "$ER_FACTS_FILE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))["network"]
print(", ".join(f"{s['id']} ({s['az']})" for s in d["subnets"]))
PY
)"
        fi
        _er_row "Private subnets" "${detail:-$ER_SUBNET_IDS}"
        _er_row "Lambda security group" "$ER_LAMBDA_SG"
        _er_row "Database security group" "$ER_DB_SG"
        _er_row "Cache security group" "$ER_CACHE_SG"
        printf '    %-22s NOT CREATED (existing network supplies routing)\n' "NAT / IGW / endpoints"
    else
        printf '    %-22s CREATE NEW  (VPC 10.50.0.0/16, 2 private + 2 public subnets, NAT, S3 endpoint, 3 SGs)\n' "Network"
    fi
    _er_row "Backend role" "$ER_ROLE_BACKEND"
    _er_row "Ingester role" "$ER_ROLE_INGESTER"
    _er_row "Schema-init role" "$ER_ROLE_SCHEMA_INIT"
    _er_row "Pre-warm role" "$ER_ROLE_PREWARM"
    _er_row "Pre-sign-up role" "$ER_ROLE_PRE_SIGNUP"
    _er_row "Post-confirm role" "$ER_ROLE_POST_CONFIRM"
    _er_row "Lambda@Edge role" "$ER_ROLE_EDGE"
    if [[ -n "$ER_CFN_ROLE" ]]; then printf '    %-22s %s\n' "CloudFormation role" "$ER_CFN_ROLE"
    else printf '    %-22s deployment credentials (no service role)\n' "CloudFormation role"; fi
    case "$ER_WAF" in
        new)  printf '    %-22s CREATE NEW  (AWS managed rule sets, CloudFront scope)\n' "WAF web ACL";;
        none) printf '    %-22s NOT CREATED (CloudFront without a web ACL)\n' "WAF web ACL";;
        *)    printf '    %-22s EXISTING    %s\n' "WAF web ACL" "$ER_WAF";;
    esac
    if [[ -n "$ER_FACTS_FILE" ]]; then
        "$ER_PY" - "$ER_FACTS_FILE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for w in d.get("warnings", []):
    print(f"    WARNING {w}")
PY
    fi
    echo "    Aurora, ElastiCache, Cognito, CloudFront, S3, Lambda functions and the"
    echo "    schedules are created by Lens in every configuration."
}

_er_confirm() {
    if [[ "$ACTION" != "--yes" ]]; then
        echo "  (dry run: re-run with --yes to deploy this plan)"
        return 0
    fi
    if [[ -t 0 ]]; then
        local ans
        read -r -p "  Proceed with this plan? Type 'yes' to continue: " ans
        if [[ "$ans" != "yes" ]]; then
            echo "    aborted; nothing was created."
            exit 0
        fi
    else
        echo "  (non-interactive: proceeding with the plan above)"
    fi
}

# ---------------------------------------------------------------------------
# 5. Persist prompted answers so the next run is non-interactive
# ---------------------------------------------------------------------------
_er_persist() {
    [[ "$ER_SOURCE" == "prompt" ]] || return 0
    local yn
    yn() { if [[ -n "$1" ]]; then echo "$1"; else echo "new"; fi; }
    {
        echo
        echo "# -----------------------------------------------------------------------------"
        echo "# Existing resources — written by deploy.sh from your first-run answers on"
        echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ). 'new' means Lens creates that resource."
        echo "# The network choice is fixed for the life of the stack; role ARNs may change."
        echo "# -----------------------------------------------------------------------------"
        echo "existing_resources:"
        if [[ -n "$ER_VPC_ID" ]]; then
            echo "  network:"
            echo "    vpc_id: $ER_VPC_ID"
            echo "    private_subnet_ids: [$ER_SUBNET_IDS]"
            echo "    lambda_security_group_id: $(yn "$ER_LAMBDA_SG")"
            echo "    db_security_group_id: $(yn "$ER_DB_SG")"
            echo "    cache_security_group_id: $(yn "$ER_CACHE_SG")"
        else
            echo "  network: new"
        fi
        echo "  roles:"
        echo "    backend: $(yn "$ER_ROLE_BACKEND")"
        echo "    ingester: $(yn "$ER_ROLE_INGESTER")"
        echo "    schema_init: $(yn "$ER_ROLE_SCHEMA_INIT")"
        echo "    prewarm: $(yn "$ER_ROLE_PREWARM")"
        echo "    pre_signup: $(yn "$ER_ROLE_PRE_SIGNUP")"
        echo "    post_confirm: $(yn "$ER_ROLE_POST_CONFIRM")"
        echo "    edge: $(yn "$ER_ROLE_EDGE")"
        if [[ -n "$ER_CFN_ROLE" ]]; then echo "  cloudformation_service_role_arn: $ER_CFN_ROLE"
        else echo "  cloudformation_service_role_arn: none"; fi
        echo "  waf: $ER_WAF"
    } >> "$ROOT/config.yaml"
    echo "    saved your answers to config.yaml (existing_resources:)"
}

# ---------------------------------------------------------------------------
# 6. Existing stack: read back what it was deployed with
# ---------------------------------------------------------------------------
_er_stack_params() {   # echoes "KEY=VALUE" lines for the stack's parameters, or nothing
    aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
        --query 'Stacks[0].Parameters[].[ParameterKey,ParameterValue]' --output text 2>/dev/null \
        | awk -F'\t' '{ printf "%s=%s\n", $1, ($2 == "None" ? "" : $2) }'
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
existing_resources_plan() {
    local stack_exists=0 params="" prev_vpc="" prev_subnets="" prev_waf="" error_file edge_file
    ER_STACK_FILE="$(mktemp -t bol-stack.XXXXXX)"
    error_file="$(mktemp -t bol-stack-error.XXXXXX)"
    if aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
            --output json > "$ER_STACK_FILE" 2>"$error_file"; then
        stack_exists=1
        params="$("$ER_PY" - "$ER_STACK_FILE" <<'PY'
import json, sys
for p in json.load(open(sys.argv[1]))["Stacks"][0].get("Parameters", []):
    print(f'{p["ParameterKey"]}={p["ParameterValue"]}')
PY
)"
        ER_PRIOR_PARAMS="$params"
        prev_vpc="$(printf '%s\n' "$params" | awk -F= '$1=="ExistingVpcId"{print $2}')"
        prev_subnets="$(printf '%s\n' "$params" | awk -F= '$1=="ExistingPrivateSubnetIds"{print $2}')"
        edge_file="$(mktemp -t bol-edge.XXXXXX)"
        if ! aws cloudformation describe-stacks --stack-name "$EDGE_STACK" --region "$EDGE_REGION" \
                --output json > "$edge_file"; then
            echo "ERROR: cannot read the existing edge stack; resource choices were not changed." >&2
            exit 2
        fi
        ER_PRIOR_EDGE_ROLE="$("$ER_PY" -c 'import json,sys; p={x["ParameterKey"]:x["ParameterValue"] for x in json.load(open(sys.argv[1]))["Stacks"][0].get("Parameters", [])}; print(p.get("EdgeRoleArn",""))' "$edge_file")"
        ER_PRIOR_EDGE_WAF="$("$ER_PY" -c 'import json,sys; p={x["ParameterKey"]:x["ParameterValue"] for x in json.load(open(sys.argv[1]))["Stacks"][0].get("Parameters", [])}; print(p.get("CreateWebAcl","true"))' "$edge_file")"
        rm -f "$edge_file"
        # A role change must not rename the database secret or reset its password.
        # Older templates used "any existing role" as the naming condition.
        ER_STABLE_NAMES="$("$ER_PY" - "$ER_STACK_FILE" <<'PY'
import json, sys
p = {x["ParameterKey"]: x["ParameterValue"] for x in json.load(open(sys.argv[1]))["Stacks"][0].get("Parameters", [])}
keys = ("BackendRoleArn", "IngesterRoleArn", "SchemaInitRoleArn", "PrewarmRoleArn", "PreSignUpRoleArn", "PostConfirmRoleArn")
print(p.get("StableResourceNames", str(any(p.get(k) for k in keys)).lower()))
PY
)"
    elif grep -q 'does not exist' "$error_file"; then
        printf '{"Stacks":[]}\n' > "$ER_STACK_FILE"
    else
        cat "$error_file" >&2
        echo "ERROR: cannot determine whether the stack exists; refusing to treat an access failure as a new deployment." >&2
        exit 2
    fi
    rm -f "$error_file"

    ER_PRESENT=0
    if [[ "${EXISTING_RESOURCES:-}" == "new" ]]; then
        ER_SOURCE="env"
    else
        _er_read_config
        [[ "${ER_PRESENT:-0}" == "1" ]] && ER_SOURCE="config"
    fi

    if [[ "$ER_SOURCE" == "config" ]]; then
        if [[ "${ER_NET:-new}" == "existing" ]]; then
            ER_LAMBDA_SG="${ER_LAMBDA_SG:-}"; ER_DB_SG="${ER_DB_SG:-}"; ER_CACHE_SG="${ER_CACHE_SG:-}"
        else
            ER_VPC_ID=""; ER_SUBNET_IDS=""
        fi
        ER_WAF="${ER_WAF:-new}"
        if (( stack_exists )); then
            # A Lens-created web ACL cannot be dropped or swapped in one run: the
            # edge stack deploys first and WAF refuses to delete an ACL that is
            # still attached to the distribution.
            prev_waf="$(printf '%s\n' "$params" | awk -F= '$1=="WebAclArn"{print $2}')"
            if [[ -n "$prev_waf" && "$ER_PRIOR_EDGE_WAF" == "true" && "$ER_WAF" != "new" && "$prev_waf" != "$ER_WAF" ]]; then
                echo "ERROR: config.yaml existing_resources.waf='$ER_WAF' but $MAIN_STACK already uses web ACL" >&2
                echo "       $prev_waf. Removing or replacing it in one run is not supported; keep 'waf: new'" >&2
                echo "       (or the current ARN), or deploy a new stack with STACK_NAME_SUFFIX=..." >&2
                exit 2
            fi
            # Naming the current Lens-owned ACL does not transfer ownership.
            if [[ -n "$prev_waf" && "$ER_PRIOR_EDGE_WAF" == "true" && "$prev_waf" == "$ER_WAF" ]]; then
                ER_WAF="new"
            fi
            # The network is immutable once the stack exists.
            if [[ "$prev_vpc" != "$ER_VPC_ID" || ( -n "$ER_VPC_ID" && "$prev_subnets" != "$ER_SUBNET_IDS" ) ]]; then
                echo "ERROR: config.yaml existing_resources.network differs from what $MAIN_STACK was created with." >&2
                echo "       stack:  vpc='${prev_vpc:-<created by Lens>}' subnets='${prev_subnets}'" >&2
                echo "       config: vpc='${ER_VPC_ID:-<create>}' subnets='${ER_SUBNET_IDS}'" >&2
                echo "       Changing the network would replace Aurora and ElastiCache (data loss). Deploy a new" >&2
                echo "       stack (STACK_NAME_SUFFIX=...) for a different network, or restore the previous values." >&2
                exit 2
            fi
        fi
    elif (( stack_exists )); then
        # No new choices and the stack exists: keep whatever it was created with.
        ER_USE_PREVIOUS=1
        ER_VPC_ID="$prev_vpc"; ER_SUBNET_IDS="$prev_subnets"
        echo "    resource choices: keeping $MAIN_STACK's current network and role settings"
        [[ -n "$prev_vpc" ]] && ER_NETWORK_MODE="existing"
        ER_WAF="previous"
        export ER_VPC_ID ER_VPC_CIDR ER_SUBNET_IDS ER_LAMBDA_SG ER_DB_SG ER_CACHE_SG \
               ER_ROLE_BACKEND ER_ROLE_INGESTER ER_ROLE_SCHEMA_INIT ER_ROLE_PREWARM \
               ER_ROLE_PRE_SIGNUP ER_ROLE_POST_CONFIRM ER_ROLE_EDGE ER_CFN_ROLE ER_USE_PREVIOUS ER_WAF ER_STABLE_NAMES
        return 0
    elif [[ "$ER_SOURCE" == "env" ]]; then
        echo "    resource choices: EXISTING_RESOURCES=new — Lens creates its network and roles"
    elif [[ -t 0 ]]; then
        _er_prompt
    else
        echo "    resource choices: no existing_resources block in config.yaml and no terminal;"
        echo "                      Lens will create its network and IAM roles. To use existing"
        echo "                      resources non-interactively, add the block (see config.example.yaml)."
        ER_SOURCE="default"
    fi

    if (( stack_exists )); then
        # Validate policies against the deployed identifiers, including stacks
        # that predate deterministic names. Neither operation reads secret values.
        ER_SECRET_ARN="$(aws cloudformation describe-stack-resource --stack-name "$MAIN_STACK" \
            --logical-resource-id DbSecret --region "$REGION" \
            --query 'StackResourceDetail.PhysicalResourceId' --output text)"
        local queue_url
        queue_url="$(aws cloudformation describe-stack-resource --stack-name "$MAIN_STACK" \
            --logical-resource-id SchemaInitDlq --region "$REGION" \
            --query 'StackResourceDetail.PhysicalResourceId' --output text)"
        ER_SCHEMA_DLQ_ARN="arn:${PARTITION:-aws}:sqs:${REGION}:${ACCOUNT_ID}:${queue_url##*/}"
    elif [[ -n "$ER_ROLE_BACKEND$ER_ROLE_INGESTER$ER_ROLE_SCHEMA_INIT$ER_ROLE_PREWARM$ER_ROLE_PRE_SIGNUP$ER_ROLE_POST_CONFIRM" ]]; then
        ER_STABLE_NAMES="true"
    fi
    _er_validate
    _er_summary
    if _er_any_existing || [[ "$ER_SOURCE" == "prompt" ]]; then
        _er_confirm
    fi
    _er_persist

    [[ -n "$ER_VPC_ID" ]] && ER_NETWORK_MODE="existing"
    if [[ -n "$ER_CFN_ROLE" ]]; then ER_CFN_ROLE_ARGS=(--role-arn "$ER_CFN_ROLE"); fi
    # Explicit empty/true values are essential on updates: the CLI otherwise
    # reuses a previously supplied role or a previously disabled web ACL.
    ER_EDGE_PARAMS=("EdgeRoleArn=$ER_ROLE_EDGE" "CreateWebAcl=$([[ "$ER_WAF" == "new" ]] && echo true || echo false)")
    export ER_VPC_ID ER_VPC_CIDR ER_SUBNET_IDS ER_LAMBDA_SG ER_DB_SG ER_CACHE_SG \
           ER_ROLE_BACKEND ER_ROLE_INGESTER ER_ROLE_SCHEMA_INIT ER_ROLE_PREWARM \
           ER_ROLE_PRE_SIGNUP ER_ROLE_POST_CONFIRM ER_ROLE_EDGE ER_CFN_ROLE ER_USE_PREVIOUS ER_WAF ER_STABLE_NAMES
    return 0
}
