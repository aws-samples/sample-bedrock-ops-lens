#!/usr/bin/env bash
# ============================================================================
# Bedrock Ops Lens — multi-account data pipeline, one-click setup.
#
# Configures ingestion using either existing reader roles or reader roles
# deployed by `scripts/setup-multi-account.py`:
#
#   1. Use existing reader roles (--skip-rollout), or deploy them.
#   2. Ensure the central ingester Lambda is in `discover-org` mode (or
#      `explicit` for --scope accounts), so it actually uses the new roles.
#   3. Trigger one ingest run synchronously and report what landed.
#
# This is the "data pipeline" companion to deploy.sh:
#
#   ./deploy.sh --yes              # central stack: VPC, Aurora, Lambda, SPA
#   ./setup-pipeline.sh --scope ou --ou-id ou-xxxx-yyyyyyyy   # multi-account
#   ./setup-pipeline.sh --scope org-root
#   ./setup-pipeline.sh --scope accounts --accounts 111111111111,222222222222
#   ./setup-pipeline.sh --scope accounts --accounts-file accounts.txt --skip-rollout
#   ./setup-pipeline.sh --scope single   # dashboard sees only the central acct
#
# Re-runnable. Removing an explicit account stops future monitoring, but does
# not delete its reader role or historical data. See docs/multi-account-setup.md.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_CALLER_DIR="$PWD"
cd "$ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCOPE=""
OU_ID=""
ACCOUNTS=""
ACCOUNTS_FILE=""
DELEGATED_ADMIN=""
SKIP_ROLLOUT=""
SKIP_INGEST=""
DRY_RUN=""
REQUESTED_ROLE_NAME="${BEDROCK_OPS_LENS_ROLE_NAME:-}"
# Region resolution mirrors deploy.sh: prefer DEPLOY_REGION env var, else
# config.yaml's deploy_region, else us-east-1. Deliberately ignores
# AWS_REGION / AWS_DEFAULT_REGION from the shell — they're a footgun (the
# central stack is region-pinned at deploy time).
if [[ -n "${DEPLOY_REGION:-}" ]]; then
    PIPELINE_REGION="$DEPLOY_REGION"
elif [[ -r "$ROOT/config.yaml" ]]; then
    PIPELINE_REGION="$(grep -E '^deploy_region:' "$ROOT/config.yaml" | awk '{print $2}' | tr -d '"' | tr -d "'" | head -1)"
    PIPELINE_REGION="${PIPELINE_REGION:-us-east-1}"
else
    PIPELINE_REGION="us-east-1"
fi
export AWS_REGION="$PIPELINE_REGION"
export AWS_DEFAULT_REGION="$PIPELINE_REGION"

usage() {
    cat <<EOF
Usage:
  ./setup-pipeline.sh --scope <single|ou|org-root|accounts> [opts]

Scopes:
  --scope single
        Pull data only from the account that owns this dashboard. No
        StackSet, no cross-account roles.

  --scope ou --ou-id ou-xxxx-yyyyyyyy [--delegated-admin]
        Service-managed StackSet across every account in the OU. Auto-deploy
        is ON, so accounts joining the OU later are auto-onboarded. Run from
        the management account, or pass --delegated-admin from a delegated
        administrator account.

  --scope org-root [--delegated-admin]
        Same as --scope ou but targets every account in the org root.

  --scope accounts --accounts 111111111111,222222222222
  --scope accounts --accounts-file accounts.txt
        Explicit account list; no AWS Organizations required.
        Add --skip-rollout to use reader roles created by each account owner,
        with no StackSets or StackSet administration/execution roles.
        Otherwise deploy via a self-managed StackSet: each target needs the
        AWSCloudFormationStackSetExecutionRole pre-provisioned. The central
        account also needs AWSCloudFormationStackSetAdministrationRole and
        the operator needs iam:PassRole. See docs/multi-account-setup.md.

Options:
  --skip-rollout      Use existing reader roles; accounts scope only.
                      Does not create, update, or verify target IAM roles.
  --role-name NAME    Must match ReaderRoleName in the central stack.
                      If omitted, setup uses the deployed parameter's value.
  --skip-ingest       Skip the ingest run after configuring the pipeline.
  --dry-run           Validate inputs and read central configuration; no writes.

Environment:
  DEPLOY_REGION       Override config.yaml deploy_region (else us-east-1).
  STACK_NAME_SUFFIX   Override the central-stack suffix lookup. Normally
                      read from .deploy-stack-name.
  BEDROCK_OPS_LENS_ROLE_NAME
                        Same check as --role-name (the flag takes precedence).
                        To change the central permission, set this for deploy.sh
                        first, then deploy matching target reader roles.
  BEDROCK_OPS_LENS_EXTERNAL_ID
                        Optional trust condition. If unset, preserve the
                        ingester's existing value. With --skip-rollout, target
                        owners must keep their existing trust conditions in sync.
EOF
    exit "${1:-1}"
}

require_value() {
    if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
        echo "ERROR: $1 requires a value" >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scope)              require_value "$@"; SCOPE="$2"; shift 2;;
        --ou-id)              require_value "$@"; OU_ID="$2"; shift 2;;
        --accounts)           require_value "$@"; ACCOUNTS="$2"; shift 2;;
        --accounts-file)      require_value "$@"; ACCOUNTS_FILE="$2"; shift 2;;
        --role-name)          require_value "$@"; REQUESTED_ROLE_NAME="$2"; shift 2;;
        --delegated-admin)    DELEGATED_ADMIN="--delegated-admin"; shift;;
        --skip-rollout)       SKIP_ROLLOUT=1; shift;;
        --skip-ingest)        SKIP_INGEST=1; shift;;
        --dry-run)            DRY_RUN=1; shift;;
        -h|--help)            usage 0;;
        *)                    echo "ERROR: unknown arg: $1" >&2; usage;;
    esac
done

if [[ -z "$SCOPE" ]]; then
    echo "ERROR: --scope is required" >&2
    usage
fi

case "$SCOPE" in
    single|ou|org-root|accounts) ;;
    *) echo "ERROR: --scope must be one of: single, ou, org-root, accounts" >&2; exit 1;;
esac
if [[ -n "$SKIP_ROLLOUT" && "$SCOPE" != "accounts" ]]; then
    echo "ERROR: --skip-rollout requires --scope accounts" >&2
    exit 2
fi
if [[ -n "$REQUESTED_ROLE_NAME" && ! "$REQUESTED_ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]]; then
    echo "ERROR: --role-name / BEDROCK_OPS_LENS_ROLE_NAME must be a valid IAM role name (1-64 characters, no path or wildcards)." >&2
    exit 2
fi

# Validate and normalize explicit IDs once, before any AWS call. Both the
# StackSet and Lambda receive this exact list, including --accounts-file users.
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c 'import boto3' >/dev/null 2>&1 || {
    echo "ERROR: python3 needs boto3. Install it in your Python environment first." >&2
    exit 1
}
PY_ARGS=( "scripts/setup-multi-account.py" "--scope" "$SCOPE" "--region" "$PIPELINE_REGION" )
case "$SCOPE" in
    accounts)
        if [[ -z "$ACCOUNTS" && -z "$ACCOUNTS_FILE" ]]; then
            echo "ERROR: --scope accounts requires --accounts or --accounts-file" >&2
            exit 2
        fi
        if [[ -n "$ACCOUNTS" && -n "$ACCOUNTS_FILE" ]]; then
            echo "ERROR: use only one of --accounts and --accounts-file" >&2
            exit 2
        fi
        if [[ -n "$ACCOUNTS_FILE" ]]; then
            if [[ "$ACCOUNTS_FILE" != /* ]]; then
                ACCOUNTS_FILE="$PIPELINE_CALLER_DIR/$ACCOUNTS_FILE"
            fi
            ACCOUNTS="$(python3 "${PY_ARGS[@]}" --accounts-file "$ACCOUNTS_FILE" --print-account-ids)"
        else
            ACCOUNTS="$(python3 "${PY_ARGS[@]}" --accounts "$ACCOUNTS" --print-account-ids)"
        fi
        PY_ARGS+=( "--accounts" "$ACCOUNTS" )
        ;;
    ou)
        if [[ -z "$OU_ID" ]]; then echo "ERROR: --scope ou requires --ou-id" >&2; exit 2; fi
        PY_ARGS+=( "--ou-id" "$OU_ID" )
        ;;
esac
if [[ "$SCOPE" != "accounts" && ( -n "$ACCOUNTS" || -n "$ACCOUNTS_FILE" ) ]]; then
    echo "ERROR: account-list options require --scope accounts" >&2; exit 2
fi
if [[ "$SCOPE" != "ou" && -n "$OU_ID" ]]; then
    echo "ERROR: --ou-id requires --scope ou" >&2; exit 2
fi
if [[ -n "$DELEGATED_ADMIN" ]]; then
    if [[ "$SCOPE" != "ou" && "$SCOPE" != "org-root" ]]; then
        echo "ERROR: --delegated-admin requires --scope ou or org-root" >&2; exit 2
    fi
    PY_ARGS+=( "$DELEGATED_ADMIN" )
fi

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
echo "[1/4] pre-flight..."
command -v aws >/dev/null    || { echo "ERROR: aws CLI not found"; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "    central account: $ACCOUNT_ID"
echo "    region:          $PIPELINE_REGION"
echo "    scope:           $SCOPE"

# Resolve central stack suffix (so we know which Lambda to talk to).
PIN_FILE="$ROOT/.deploy-stack-name"
SUFFIX="${STACK_NAME_SUFFIX:-}"
if [[ -z "$SUFFIX" && -r "$PIN_FILE" ]]; then
    SUFFIX="$(cat "$PIN_FILE")"
fi
if [[ -z "$SUFFIX" ]]; then
    echo "ERROR: cannot resolve stack suffix. Run ./deploy.sh first or set STACK_NAME_SUFFIX." >&2
    exit 1
fi
MAIN_STACK="BedrockOpsLens-$SUFFIX"
INGESTER_FN="${MAIN_STACK}-ingester"
echo "    central stack:   $MAIN_STACK"
echo "    ingester:        $INGESTER_FN"

# Confirm the central stack exists and read only its discovery setting.
if ! STACK_DISCOVERY_SETTING="$(aws cloudformation describe-stacks \
    --stack-name "$MAIN_STACK" --region "$PIPELINE_REGION" \
    --query 'Stacks[0].Parameters[?ParameterKey==`EnableOrganizationsDiscovery`].ParameterValue' \
    --output text 2>/dev/null)"; then
    echo "ERROR: stack $MAIN_STACK not found in $PIPELINE_REGION." >&2
    echo "       Run ./deploy.sh --yes first." >&2
    exit 1
fi
if [[ "$STACK_DISCOVERY_SETTING" == "false" && ( "$SCOPE" == "ou" || "$SCOPE" == "org-root" ) ]]; then
    echo "ERROR: Organizations discovery is disabled on $MAIN_STACK." >&2
    echo "       Use --scope accounts, or redeploy with ENABLE_ORGANIZATIONS_DISCOVERY=true before selecting an Organizations scope." >&2
    exit 1
fi
if ! PIPELINE_ROLE_NAME="$(aws cloudformation describe-stacks \
    --stack-name "$MAIN_STACK" --region "$PIPELINE_REGION" \
    --query 'Stacks[0].Parameters[?ParameterKey==`ReaderRoleName`].ParameterValue | [0]' \
    --output json 2>/dev/null)"; then
    echo "ERROR: cannot read ReaderRoleName from $MAIN_STACK; resolve CloudFormation access before setup." >&2
    exit 1
fi
# Older templates granted only this name; an absent parameter is not permission
# to select a different role based on a Lambda environment override.
PIPELINE_ROLE_NAME="$(python3 -c \
    'import json,sys; value=json.loads(sys.argv[1]); print("BedrockOpsLensReader" if value is None else value)' \
    "$PIPELINE_ROLE_NAME")"
if [[ ! "$PIPELINE_ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]]; then
    echo "ERROR: the central stack has an invalid ReaderRoleName." >&2
    exit 2
fi
if [[ -n "$REQUESTED_ROLE_NAME" && "$REQUESTED_ROLE_NAME" != "$PIPELINE_ROLE_NAME" ]]; then
    echo "ERROR: requested reader role $REQUESTED_ROLE_NAME does not match central ReaderRoleName=$PIPELINE_ROLE_NAME." >&2
    echo "       Redeploy the central stack with BEDROCK_OPS_LENS_ROLE_NAME=$REQUESTED_ROLE_NAME before onboarding that name." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Resolve the actual runtime principal before creating any cross-account trust.
# ---------------------------------------------------------------------------
umask 077
PIPELINE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/bedrock-ops-lens-pipeline.XXXXXX")"
trap 'rm -rf "$PIPELINE_TMP"' EXIT
aws lambda get-function-configuration \
    --function-name "$INGESTER_FN" --region "$PIPELINE_REGION" \
    --output json > "$PIPELINE_TMP/function.json"
INGESTER_ROLE_ARN="$(python3 - "$PIPELINE_TMP/function.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    config = json.load(stream)
if config.get("Environment", {}).get("Error"):
    raise SystemExit("ERROR: cannot read the ingester environment; resolve its access error before setup")
print(config["Role"])
PY
)"
PIPELINE_EXTERNAL_ID="${BEDROCK_OPS_LENS_EXTERNAL_ID-$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1])).get("Environment", {}).get("Variables", {}).get("BEDROCK_OPS_LENS_EXTERNAL_ID", ""))' \
    "$PIPELINE_TMP/function.json")}"
PY_ARGS+=( "--role-name" "$PIPELINE_ROLE_NAME" "--ingester-role-arn" "$INGESTER_ROLE_ARN" "--external-id" "$PIPELINE_EXTERNAL_ID" )
echo "    runtime role:    $INGESTER_ROLE_ARN"
echo "    reader role:     $PIPELINE_ROLE_NAME"
[[ -z "$ACCOUNTS" ]] || echo "    target accounts: $ACCOUNTS"
[[ -z "$OU_ID" ]] || echo "    target OU:       $OU_ID"

# Decide what MONITORED_ACCOUNTS_MODE we want the ingester to be in.
# - single       -> single
# - accounts     -> explicit (with MONITORED_ACCOUNTS_IDS)
# - ou/org-root  -> discover-org
case "$SCOPE" in
    single)            INGEST_MODE="single";      INGEST_IDS="";;
    accounts)          INGEST_MODE="explicit";    INGEST_IDS="$ACCOUNTS";;
    ou|org-root)       INGEST_MODE="discover-org"; INGEST_IDS="";;
esac

if [[ -n "$DRY_RUN" ]]; then
    echo
    echo "DRY RUN — would run:"
    if [[ -n "$SKIP_ROLLOUT" ]]; then
        echo "    Use existing $PIPELINE_ROLE_NAME roles; no StackSet or IAM rollout"
        echo "    Target owners must already authorize $INGESTER_ROLE_ARN"
        [[ -z "$PIPELINE_EXTERNAL_ID" ]] || echo "    Supply the configured external ID; existing target trust conditions must match"
    else
        echo "    Roll out $PIPELINE_ROLE_NAME (scope=$SCOPE, region=$PIPELINE_REGION)"
        echo "    Trust only $INGESTER_ROLE_ARN"
        [[ -z "$PIPELINE_EXTERNAL_ID" ]] || echo "    Require the configured external ID on reader roles and ingester"
    fi
    echo "    aws lambda update-function-configuration  (mode=$INGEST_MODE)"
    [[ -z "$SKIP_INGEST" ]] && echo "    aws lambda invoke $INGESTER_FN"
    exit 0
fi

# ---------------------------------------------------------------------------
# 2/4: Use existing reader roles, or roll them out
# ---------------------------------------------------------------------------
echo
if [[ -n "$SKIP_ROLLOUT" ]]; then
    echo "[2/4] using existing $PIPELINE_ROLE_NAME roles (--skip-rollout)."
    echo "      Target roles and trust policies are managed by their account owners."
    echo "      Runtime access will be checked by ingestion unless --skip-ingest is set."
else
    echo "[2/4] rolling out $PIPELINE_ROLE_NAME (scope: $SCOPE)..."
    python3 "${PY_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# 3/4: Reconfigure the central ingester so it actually uses the new roles
# ---------------------------------------------------------------------------
echo
echo "[3/4] reconfiguring ingester to mode=$INGEST_MODE..."
aws lambda get-function-configuration \
    --function-name "$INGESTER_FN" --region "$PIPELINE_REGION" \
    --output json > "$PIPELINE_TMP/function.json"

# Use argv, not unexported shell variables. Keep unrelated environment entries,
# and use RevisionId so a concurrent update cannot be silently overwritten.
PIPELINE_REVISION="$(python3 - "$PIPELINE_TMP/function.json" \
    "$PIPELINE_TMP/environment.json" "$INGEST_MODE" "$INGEST_IDS" \
    "$PIPELINE_EXTERNAL_ID" "$INGESTER_ROLE_ARN" "$PIPELINE_ROLE_NAME" <<'PY'
import json
import sys

source, destination, mode, ids, external_id, expected_role, reader_role_name = sys.argv[1:]
with open(source) as stream:
    config = json.load(stream)
if config["Role"] != expected_role:
    raise SystemExit("ERROR: the ingester role changed during setup; re-run before changing its scope")
if config.get("Environment", {}).get("Error"):
    raise SystemExit("ERROR: cannot read the ingester environment; refusing to overwrite it")
v = dict(config.get("Environment", {}).get("Variables", {}))
v["MONITORED_ACCOUNTS_MODE"] = mode
v["BEDROCK_OPS_LENS_ROLE_NAME"] = reader_role_name
if mode == "explicit":
    if not ids:
        raise SystemExit("ERROR: refusing to configure explicit mode without account IDs")
    v["MONITORED_ACCOUNTS_IDS"] = ids
else:
    v.pop("MONITORED_ACCOUNTS_IDS", None)
if external_id:
    v["BEDROCK_OPS_LENS_EXTERNAL_ID"] = external_id
else:
    v.pop("BEDROCK_OPS_LENS_EXTERNAL_ID", None)
with open(destination, "w") as stream:
    json.dump({"Variables": v}, stream)
print(config["RevisionId"])
PY
)"
aws lambda update-function-configuration \
        --function-name "$INGESTER_FN" \
        --environment "file://$PIPELINE_TMP/environment.json" \
        --revision-id "$PIPELINE_REVISION" \
        --region "$PIPELINE_REGION" \
        --query 'Environment.Variables.MONITORED_ACCOUNTS_MODE' --output text >/dev/null
aws lambda wait function-updated --function-name "$INGESTER_FN" --region "$PIPELINE_REGION"
echo "    ingester reconfigured."

# ---------------------------------------------------------------------------
# 4/4: Trigger one ingest run + report results
# ---------------------------------------------------------------------------
if [[ -n "$SKIP_INGEST" ]]; then
    echo
    echo "[4/4] Pipeline configured. --skip-ingest set; ingestion has not been verified."
    echo "      The existing EventBridge schedule will run ingestion."
    exit 0
fi

echo
echo "[4/4] running first ingest..."
INGEST_OUT="$PIPELINE_TMP/ingest.json"

START="$(date +%s)"
set +e
AWS_MAX_ATTEMPTS=1 aws lambda invoke \
    --function-name "$INGESTER_FN" \
    --invocation-type RequestResponse \
    --cli-read-timeout 910 \
    --region "$PIPELINE_REGION" \
    "$INGEST_OUT" > "$PIPELINE_TMP/invoke.json"
RC=$?
set -e
END="$(date +%s)"
DURATION="$((END-START))s"

if [[ $RC -ne 0 ]]; then
    echo "    WARNING: ingest invoke failed (rc=$RC). Check CloudWatch logs:" >&2
    echo "      aws logs tail /aws/lambda/$INGESTER_FN --since 5m --region $PIPELINE_REGION" >&2
    exit 1
fi

if ! python3 - "$PIPELINE_TMP/invoke.json" "$INGEST_OUT" \
    "$PIPELINE_TMP/ingest-status.txt" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as stream:
        invocation = json.load(stream)
    with open(sys.argv[2]) as stream:
        result = json.load(stream)
    print(json.dumps(result, indent=2))
    if invocation.get("StatusCode") != 200 or invocation.get("FunctionError"):
        raise ValueError("Lambda invocation returned a function error")
    runs = result.get("runs", [])
    # Plain rc=2 is an error (including argparse failures and empty account
    # discovery). A resumable budget stop must carry the runner's explicit mark.
    incomplete = [
        run for run in runs
        if run.get("rc") == 2 and run.get("incomplete_reason") == "time_budget"
    ]
    expected_status = "incomplete" if incomplete else "ok"
    if (result.get("status") != expected_status or result.get("failed_count") != 0
            or not runs
            or any(run.get("rc") not in (0, None) and run not in incomplete for run in runs)):
        raise ValueError("ingestion did not complete successfully in every reported module")
    if incomplete:
        print("NOTE: ingestion reached its time budget; data coverage is incomplete for: "
              + ", ".join(str(run.get("module")) for run in incomplete))
        print("      Completed log objects are recorded. Re-run the ingester or wait "
              "for the existing schedule to resume the remaining work.")
    with open(sys.argv[3], "w") as stream:
        stream.write(expected_status + "\n")
except (OSError, ValueError, TypeError, AttributeError) as error:
    raise SystemExit(f"ERROR: {error}")
PY
then
    echo "    Inspect ingestion logs before relying on data coverage:" >&2
    echo "      aws logs tail /aws/lambda/$INGESTER_FN --since 30m --region $PIPELINE_REGION" >&2
    exit 1
fi

echo
echo "============================================================================"
IFS= read -r INGEST_STATUS < "$PIPELINE_TMP/ingest-status.txt"
if [[ "$INGEST_STATUS" == "incomplete" ]]; then
    echo "⏳ PIPELINE CONFIGURED — ingestion incomplete; remaining log objects can resume"
else
    echo "✅ PIPELINE CONFIGURED — ingestion modules completed successfully"
fi
echo "   Mode:     $INGEST_MODE"
echo "   Run took: $DURATION"
echo "   Schedule: Uses the existing EventBridge ingestion schedule."
echo "   Verify:   Check the expected accounts and Regions in Settings and the logs."
echo "   Re-run:   Use the same setup command, including its account or OU arguments."
echo "============================================================================"
