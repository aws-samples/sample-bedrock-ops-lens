#!/usr/bin/env bash
# ============================================================================
# Bedrock Ops Lens — multi-account data pipeline, one-click setup.
#
# Wraps `scripts/setup-multi-account.py` (which deploys BedrockOpsLensReader
# via CloudFormation StackSets) with a verify-and-trigger flow:
#
#   1. Roll out the reader role into the chosen scope.
#   2. Ensure the central ingester Lambda is in `discover-org` mode (or
#      `explicit` for --scope accounts), so it actually uses the new roles.
#   3. Trigger one ingest run synchronously and report what landed.
#
# This is the "data pipeline" companion to deploy.sh:
#
#   ./deploy.sh --yes              # central stack: VPC, Aurora, Lambda, SPA
#   ./setup-pipeline.sh --scope ou --ou-id ou-xxxx-yyyyyyyy   # multi-account
#   ./setup-pipeline.sh --scope org-root
#   ./setup-pipeline.sh --scope accounts --accounts 111,222,333
#   ./setup-pipeline.sh --scope single   # dashboard sees only the central acct
#
# Idempotent. Re-runnable any time accounts are added or removed from the OU.
# Tear-down runs through CloudFormation; this script doesn't delete anything.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCOPE=""
OU_ID=""
ACCOUNTS=""
ACCOUNTS_FILE=""
DELEGATED_ADMIN=""
SKIP_INGEST=""
DRY_RUN=""
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

  --scope accounts --accounts 111,222,333
  --scope accounts --accounts-file accounts.txt
        Self-managed StackSet against an explicit account list. Doesn't
        require AWS Organizations, but each member account must have the
        AWSCloudFormationStackSetExecutionRole pre-provisioned.

Options:
  --skip-ingest       Skip the post-rollout ingest run.
  --dry-run           Print what would happen, don't execute.

Environment:
  DEPLOY_REGION       Override the deploy region. Defaults to us-east-1.
  STACK_NAME_SUFFIX   Override the central-stack suffix lookup. Normally
                      read from .deploy-stack-name.
EOF
    exit 1
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scope)              SCOPE="$2"; shift 2;;
        --ou-id)              OU_ID="$2"; shift 2;;
        --accounts)           ACCOUNTS="$2"; shift 2;;
        --accounts-file)      ACCOUNTS_FILE="$2"; shift 2;;
        --delegated-admin)    DELEGATED_ADMIN="--delegated-admin"; shift;;
        --skip-ingest)        SKIP_INGEST=1; shift;;
        --dry-run)            DRY_RUN=1; shift;;
        -h|--help)            usage;;
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

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
echo "[1/4] pre-flight..."
command -v aws >/dev/null    || { echo "ERROR: aws CLI not found"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

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

# Confirm the central stack actually exists.
if ! aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$PIPELINE_REGION" >/dev/null 2>&1; then
    echo "ERROR: stack $MAIN_STACK not found in $PIPELINE_REGION." >&2
    echo "       Run ./deploy.sh --yes first." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Scope-specific arg validation + pretty-print of what we'll run
# ---------------------------------------------------------------------------
PY_ARGS=( "scripts/setup-multi-account.py" "--scope" "$SCOPE" )
case "$SCOPE" in
    ou)
        if [[ -z "$OU_ID" ]]; then echo "ERROR: --scope ou requires --ou-id" >&2; exit 1; fi
        PY_ARGS+=( "--ou-id" "$OU_ID" )
        [[ -n "$DELEGATED_ADMIN" ]] && PY_ARGS+=( "$DELEGATED_ADMIN" )
        echo "    target OU:       $OU_ID"
        ;;
    org-root)
        [[ -n "$DELEGATED_ADMIN" ]] && PY_ARGS+=( "$DELEGATED_ADMIN" )
        echo "    target:          entire org root"
        ;;
    accounts)
        if [[ -z "$ACCOUNTS" && -z "$ACCOUNTS_FILE" ]]; then
            echo "ERROR: --scope accounts requires --accounts or --accounts-file" >&2; exit 1
        fi
        if [[ -n "$ACCOUNTS" ]];      then PY_ARGS+=( "--accounts" "$ACCOUNTS" ); fi
        if [[ -n "$ACCOUNTS_FILE" ]]; then PY_ARGS+=( "--accounts-file" "$ACCOUNTS_FILE" ); fi
        echo "    target accounts: ${ACCOUNTS:-(from $ACCOUNTS_FILE)}"
        ;;
    single)
        echo "    target:          central account only"
        ;;
esac

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
    echo "    python3 ${PY_ARGS[*]}"
    echo "    aws lambda update-function-configuration  (mode=$INGEST_MODE)"
    [[ -z "$SKIP_INGEST" ]] && echo "    aws lambda invoke $INGESTER_FN"
    exit 0
fi

# ---------------------------------------------------------------------------
# 2/4: Roll out the reader role
# ---------------------------------------------------------------------------
echo
echo "[2/4] rolling out BedrockOpsLensReader (scope: $SCOPE)..."
python3 "${PY_ARGS[@]}"

# ---------------------------------------------------------------------------
# 3/4: Reconfigure the central ingester so it actually uses the new roles
# ---------------------------------------------------------------------------
echo
echo "[3/4] reconfiguring ingester to mode=$INGEST_MODE..."
CURRENT_ENV="$(aws lambda get-function-configuration \
    --function-name "$INGESTER_FN" --region "$PIPELINE_REGION" \
    --query 'Environment.Variables' --output json)"

NEW_ENV="$(echo "$CURRENT_ENV" | python3 -c "
import json, sys, os
v = json.load(sys.stdin)
v['MONITORED_ACCOUNTS_MODE'] = os.environ['INGEST_MODE']
ids = os.environ.get('INGEST_IDS', '')
if ids:
    v['MONITORED_ACCOUNTS_IDS'] = ids
elif 'MONITORED_ACCOUNTS_IDS' in v:
    # Drop a stale explicit list when switching to a discovery mode.
    del v['MONITORED_ACCOUNTS_IDS']
print(json.dumps({'Variables': v}))
" )"
INGEST_MODE="$INGEST_MODE" INGEST_IDS="$INGEST_IDS" \
    aws lambda update-function-configuration \
        --function-name "$INGESTER_FN" \
        --environment "$NEW_ENV" \
        --region "$PIPELINE_REGION" \
        --query 'Environment.Variables.MONITORED_ACCOUNTS_MODE' --output text >/dev/null
aws lambda wait function-updated --function-name "$INGESTER_FN" --region "$PIPELINE_REGION"
echo "    ingester reconfigured."

# ---------------------------------------------------------------------------
# 4/4: Trigger one ingest run + report results
# ---------------------------------------------------------------------------
if [[ -n "$SKIP_INGEST" ]]; then
    echo
    echo "[4/4] --skip-ingest set; data will populate on the daily 05:00 UTC schedule."
    exit 0
fi

echo
echo "[4/4] running first ingest..."
INGEST_OUT="$(mktemp -t bol-pipe.XXXXXX.json)"
trap 'rm -f "$INGEST_OUT"' EXIT

START="$(date +%s)"
set +e
aws lambda invoke \
    --function-name "$INGESTER_FN" \
    --invocation-type RequestResponse \
    --cli-read-timeout 900 \
    --region "$PIPELINE_REGION" \
    "$INGEST_OUT" >/dev/null 2>&1
RC=$?
set -e
END="$(date +%s)"
DURATION="$((END-START))s"

if [[ $RC -ne 0 ]]; then
    echo "    WARNING: ingest invoke failed (rc=$RC). Check CloudWatch logs:" >&2
    echo "      aws logs tail /aws/lambda/$INGESTER_FN --since 5m --region $PIPELINE_REGION" >&2
    exit 1
fi

if command -v jq >/dev/null 2>&1; then
    jq . "$INGEST_OUT"
else
    cat "$INGEST_OUT"; echo
fi

echo
echo "============================================================================"
echo "✅ PIPELINE READY"
echo "   Mode:     $INGEST_MODE"
echo "   Run took: $DURATION"
echo "   Daily:    EventBridge fires the ingester at 05:00 UTC every day."
echo "   Manual:   ./setup-pipeline.sh --scope $SCOPE  (re-run any time)"
echo "============================================================================"
