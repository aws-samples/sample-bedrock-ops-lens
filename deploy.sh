#!/usr/bin/env bash
# ============================================================================
# Bedrock Ops Lens — one-command deploy.
#
# Pure CloudFormation. Two-pass:
#   Pass 1: ECR repo only (so we can push the backend image first)
#   Pass 2: Everything else (VPC, Aurora, Redis, Fargate, ALB, S3,
#           Cognito, CloudFront)
#
# Usage:
#     ./deploy.sh              # dry-run (validate templates + show plan)
#     ./deploy.sh --yes        # actually deploy
#     ./deploy.sh destroy      # tear down
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

ACTION="${1:-dry-run}"

# -----------------------------------------------------------------------------
# Pre-flight
# -----------------------------------------------------------------------------
echo "[1/8] pre-flight checks..."
command -v aws >/dev/null    || { echo "ERROR: aws CLI not found";    exit 1; }
command -v docker >/dev/null || { echo "ERROR: docker not found";     exit 1; }

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running."
    if [[ "$OSTYPE" == "darwin"* ]] && [[ -d "/Applications/Docker.app" ]]; then
        echo "       Start it with:  open -a Docker"
    elif command -v colima >/dev/null 2>&1; then
        echo "       Start it with:  colima start"
    else
        echo "       Start your Docker daemon and re-run."
    fi
    exit 1
fi

# config.yaml is consumed by the Lambda image build (Dockerfile.lambda) and
# by ingestion/config.py at runtime. It is gitignored on purpose — each
# customer edits their own. Fail fast with a clear message instead of dying
# inside the Docker build with a confusing "/config.yaml not found".
if [[ ! -r config.yaml ]]; then
    echo "ERROR: config.yaml is missing. Copy the template and edit it:"
    echo "       cp config.example.yaml config.yaml"
    echo "       Then set your monitored_accounts / regions / Bedrock model id."
    exit 1
fi

# Region resolution. Priority:
#   1. DEPLOY_REGION env var
#   2. config.yaml `deploy_region`
#   3. fallback us-east-1
# Deliberately ignore AWS_REGION / AWS_DEFAULT_REGION from the user's shell —
# leftover values there are exactly the kind of footgun a deploy script
# must not silently honour.
if [[ -n "${DEPLOY_REGION:-}" ]]; then
    REGION="$DEPLOY_REGION"
elif [[ -r "$ROOT/config.yaml" ]]; then
    REGION="$(grep -E '^deploy_region:' "$ROOT/config.yaml" | awk '{print $2}' | tr -d '"' | tr -d "'" | head -1)"
    REGION="${REGION:-us-east-1}"
else
    REGION="us-east-1"
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

echo "    account: $ACCOUNT_ID"
echo "    region:  $REGION  (pinned for this deploy)"

# -----------------------------------------------------------------------------
# Sign-up email-domain allowlist
#
# On re-deploy, the existing CFN stack is the source of truth — auto-discover
# the parameter so the operator doesn't have to remember it. Only prompt when
# this is a first-time deploy and the env var wasn't passed in.
# -----------------------------------------------------------------------------
if [[ -z "${ALLOWED_EMAIL_DOMAINS:-}" ]]; then
    EXISTING_STACK_NAME="${STACK_NAME_SUFFIX:+BedrockOpsLens-${STACK_NAME_SUFFIX}}"
    if [[ -z "$EXISTING_STACK_NAME" && -r "$ROOT/.deploy-stack-name" ]]; then
        EXISTING_STACK_NAME="BedrockOpsLens-$(cat "$ROOT/.deploy-stack-name")"
    fi
    if [[ -n "$EXISTING_STACK_NAME" ]]; then
        ALLOWED_EMAIL_DOMAINS="$(aws cloudformation describe-stacks \
            --stack-name "$EXISTING_STACK_NAME" --region "$REGION" \
            --query 'Stacks[0].Parameters[?ParameterKey==`AllowedEmailDomains`].ParameterValue' \
            --output text 2>/dev/null || true)"
        [[ "$ALLOWED_EMAIL_DOMAINS" == "None" ]] && ALLOWED_EMAIL_DOMAINS=""
    fi
fi
if [[ -z "${ALLOWED_EMAIL_DOMAINS:-}" ]]; then
    echo
    echo "Sign-up policy"
    echo "  Cognito will only accept sign-ups whose email domain is on the allowlist."
    echo "  Examples:  amazon.com           amazon.com,subsidiary.com           *  (any)"
    while [[ -z "${ALLOWED_EMAIL_DOMAINS:-}" ]]; do
        read -r -p "  Email domain(s) to allow: " ALLOWED_EMAIL_DOMAINS
        if [[ -z "$ALLOWED_EMAIL_DOMAINS" ]]; then
            echo "    (cannot be empty — type a domain or '*' for any)"
        fi
    done
fi
export ALLOWED_EMAIL_DOMAINS
echo "    sign-up: ALLOWED_EMAIL_DOMAINS=$ALLOWED_EMAIL_DOMAINS"

# -----------------------------------------------------------------------------
# Bedrock model-invocation logs bucket — auto-discover.
# If the operator already enabled Bedrock invocation logging in this account,
# pre-fill the parameter so tag-attribution ingestion works out of the box.
# Set BEDROCK_LOGS_BUCKET=- to opt out.
# -----------------------------------------------------------------------------
if [[ -z "${BEDROCK_LOGS_BUCKET:-}" ]]; then
    BEDROCK_LOGS_BUCKET="$(aws bedrock get-model-invocation-logging-configuration \
        --region "$REGION" --query 'loggingConfig.s3Config.bucketName' --output text 2>/dev/null || true)"
    if [[ "$BEDROCK_LOGS_BUCKET" == "None" || "$BEDROCK_LOGS_BUCKET" == "null" ]]; then
        BEDROCK_LOGS_BUCKET=""
    fi
fi
[[ "$BEDROCK_LOGS_BUCKET" == "-" ]] && BEDROCK_LOGS_BUCKET=""
if [[ -n "$BEDROCK_LOGS_BUCKET" ]]; then
    echo "    bedrock invocation logs: $BEDROCK_LOGS_BUCKET (tag-attribution ingester enabled)"
else
    echo "    bedrock invocation logs: (none configured — tag-attribution ingester skipped)"
fi

# -----------------------------------------------------------------------------
# Stack name resolution. Pin the suffix on first deploy so re-runs UPDATE
# the same stack rather than forking dozens of orphan stacks.
# -----------------------------------------------------------------------------
PIN_FILE="$ROOT/.deploy-stack-name"
SUFFIX="${STACK_NAME_SUFFIX:-}"
if [[ -z "$SUFFIX" && -r "$PIN_FILE" ]]; then
    SUFFIX="$(cat "$PIN_FILE")"
fi
if [[ -z "$SUFFIX" ]]; then
    SUFFIX="$(date +%Y%m%d-%H%M)"
    echo "$SUFFIX" > "$PIN_FILE"
    echo "    stack:   pinned new suffix $SUFFIX in .deploy-stack-name"
fi
if [[ "$SUFFIX" == "none" ]]; then
    ECR_STACK="BedrockOpsLensEcr"
    MAIN_STACK="BedrockOpsLens"
    EDGE_STACK="BedrockOpsLensEdge"
else
    ECR_STACK="BedrockOpsLensEcr-$SUFFIX"
    MAIN_STACK="BedrockOpsLens-$SUFFIX"
    EDGE_STACK="BedrockOpsLensEdge-$SUFFIX"
fi
# Edge stack is ALWAYS in us-east-1 (CloudFront / Lambda@Edge / WAFv2-CLOUDFRONT
# constraint). Main + ECR live in $REGION.
EDGE_REGION="us-east-1"
echo "    stacks:  $ECR_STACK ($REGION) + $MAIN_STACK ($REGION) + $EDGE_STACK ($EDGE_REGION)"

# -----------------------------------------------------------------------------
# Destroy
# -----------------------------------------------------------------------------
if [[ "$ACTION" == "destroy" ]]; then
    echo "[!] DESTROY requested for:"
    echo "      $MAIN_STACK ($REGION)"
    echo "      $ECR_STACK ($REGION)"
    echo "      $EDGE_STACK ($EDGE_REGION)"
    read -p "    Type 'destroy' to confirm: " confirm
    [[ "$confirm" == "destroy" ]] || { echo "    aborted."; exit 0; }
    # Order matters: main depends on edge (WebACLId / Lambda@Edge ARN), so
    # delete main first. Lambda@Edge replicas keep the edge stack stuck for
    # 30-90 minutes after CloudFront detaches, so destroy here only initiates
    # the delete and doesn't wait on edge.
    aws cloudformation delete-stack --stack-name "$MAIN_STACK" --region "$REGION" || true
    echo "    waiting for $MAIN_STACK delete..."
    aws cloudformation wait stack-delete-complete --stack-name "$MAIN_STACK" --region "$REGION" || true
    aws cloudformation delete-stack --stack-name "$ECR_STACK" --region "$REGION" || true
    aws cloudformation wait stack-delete-complete --stack-name "$ECR_STACK" --region "$REGION" || true
    aws cloudformation delete-stack --stack-name "$EDGE_STACK" --region "$EDGE_REGION" || true
    echo "    edge stack delete initiated; Lambda@Edge replicas may keep it"
    echo "    in DELETE_IN_PROGRESS for 30-90 min after CloudFront detaches."
    echo "    deleted."
    exit 0
fi

# -----------------------------------------------------------------------------
# Validate templates
# `validate-template --template-body` rejects bodies >51200 bytes. Once the
# security-hardening pass added KMS keys / WAF rules / scoped IAM, the main
# template grew past that ceiling. For any template over the limit, upload
# to a per-region staging bucket and validate via --template-url. Same
# staging bucket is reused below for create-stack / update-stack.
# -----------------------------------------------------------------------------
echo "[2/8] validating templates..."

CFN_STAGING_BUCKET="bedrock-ops-lens-cfn-staging-${ACCOUNT_ID}-${REGION}"
if ! aws s3api head-bucket --bucket "$CFN_STAGING_BUCKET" --region "$REGION" 2>/dev/null; then
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$CFN_STAGING_BUCKET" --region "$REGION" >/dev/null
    else
        aws s3api create-bucket --bucket "$CFN_STAGING_BUCKET" --region "$REGION" \
            --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$CFN_STAGING_BUCKET" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
    aws s3api put-bucket-encryption --bucket "$CFN_STAGING_BUCKET" \
        --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
    aws s3api put-bucket-versioning --bucket "$CFN_STAGING_BUCKET" \
        --versioning-configuration Status=Enabled >/dev/null
    echo "    created CFN staging bucket: s3://$CFN_STAGING_BUCKET/"
fi

# Object key includes a content hash so concurrent deploys don't race.
MAIN_HASH="$(shasum -a 256 infra/cloudformation.yaml | awk '{print substr($1,1,12)}')"
MAIN_KEY="cloudformation-${MAIN_HASH}.yaml"
aws s3 cp infra/cloudformation.yaml "s3://$CFN_STAGING_BUCKET/$MAIN_KEY" --region "$REGION" --quiet
MAIN_TEMPLATE_URL="https://s3.${REGION}.amazonaws.com/${CFN_STAGING_BUCKET}/${MAIN_KEY}"

aws cloudformation validate-template --template-body file://infra/ecr-bootstrap.yaml --region "$REGION" >/dev/null
aws cloudformation validate-template --template-body file://infra/edge-bootstrap.yaml --region "$EDGE_REGION" >/dev/null
aws cloudformation validate-template --template-url "$MAIN_TEMPLATE_URL" --region "$REGION" >/dev/null
echo "    all three templates valid"

# Pre-deploy security gate. cfn-guard catches common AWS-side flagged
# patterns: Lambda Function URL with AuthType=NONE, Lambda::Permission
# with Principal=*, S3 buckets without BlockPublicAccess, default
# encryption, IAM policies with Action+Resource both "*". If any rule
# fails, deploy aborts.
echo "[2b/6] cfn-guard security policy..."
if [[ ! -r infra/policy-guard.rules ]]; then
    echo "    infra/policy-guard.rules not present; skipping cfn-guard pre-deploy gate."
elif command -v cfn-guard >/dev/null 2>&1; then
    if ! cfn-guard validate -d infra/cloudformation.yaml -r infra/policy-guard.rules >/dev/null 2>&1; then
        echo "    cfn-guard: FAIL"
        echo
        cfn-guard validate -d infra/cloudformation.yaml -r infra/policy-guard.rules || true
        echo
        echo "ERROR: deploy blocked. Fix the rule violations above before re-running."
        exit 1
    fi
    echo "    cfn-guard: pass"
else
    echo "    cfn-guard not installed; skipping."
    echo "    Recommended: brew install cloudformation-guard (macOS) or"
    echo "    https://docs.aws.amazon.com/cfn-guard/ for other platforms."
fi

# Dry-run stops here.
if [[ "$ACTION" != "--yes" ]]; then
    cat <<EOF

============================================================================
DRY RUN COMPLETE.

Estimated monthly cost (idle, Aurora paused):
    Aurora Serverless v2 (paused)        ~ \$0–\$45
    NAT Gateway (single AZ)              ~ \$32
    ElastiCache Memcached (t4g.micro)    ~ \$13
    Lambda (backend + ingester)          ~ \$0–\$2
    CloudFront / S3 / Cognito / WAF      ~ \$5
    Total idle                           ~ \$50–\$97 / month

Security defaults:
    ✓ All S3 buckets: block-public-access = ALL
    ✓ Aurora + Memcached + Lambda: PRIVATE subnets only
    ✓ Lambda Function URL: AuthType=AWS_IAM (CloudFront-OAC-only)
    ✓ CloudFront: only public surface, fronted by AWS WAF
    ✓ Cognito User Pool: MFA optional, sign-up gated to: $ALLOWED_EMAIL_DOMAINS
    ✓ IAM: least-privilege per Lambda role

To actually deploy, run:
    ./deploy.sh --yes

============================================================================
EOF
    exit 0
fi

# -----------------------------------------------------------------------------
# Pass 1a: Edge bootstrap (always us-east-1)
# Lambda@Edge + WAFv2-CLOUDFRONT must live in us-east-1 regardless of where
# the rest of the stack runs. Capture both ARN outputs to feed the main
# stack as input parameters.
# -----------------------------------------------------------------------------
echo "[3/8] deploying edge bootstrap stack ($EDGE_STACK in $EDGE_REGION)..."
aws cloudformation deploy \
    --stack-name "$EDGE_STACK" \
    --template-file infra/edge-bootstrap.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "StackNamePrefix=$EDGE_STACK" \
    --tags "app=bedrock-ops-lens" "stack=$EDGE_STACK" \
    --region "$EDGE_REGION" \
    --no-fail-on-empty-changeset

EDGE_SHA_VERSION_ARN="$(aws cloudformation describe-stacks --stack-name "$EDGE_STACK" --region "$EDGE_REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`EdgeShaVersionArn`].OutputValue' --output text)"
WEB_ACL_ARN="$(aws cloudformation describe-stacks --stack-name "$EDGE_STACK" --region "$EDGE_REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`WebAclArn`].OutputValue' --output text)"
echo "    edge sha-256 fn: $EDGE_SHA_VERSION_ARN"
echo "    web acl:        $WEB_ACL_ARN"

# -----------------------------------------------------------------------------
# Pass 1b: ECR bootstrap (in $REGION)
# -----------------------------------------------------------------------------
echo "[4/8] deploying ECR bootstrap stack ($ECR_STACK in $REGION)..."
aws cloudformation deploy \
    --stack-name "$ECR_STACK" \
    --template-file infra/ecr-bootstrap.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "StackNamePrefix=$ECR_STACK" \
    --tags "app=bedrock-ops-lens" "stack=$ECR_STACK" \
    --region "$REGION" \
    --no-fail-on-empty-changeset

ECR_URI="$(aws cloudformation describe-stacks --stack-name "$ECR_STACK" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`EcrRepoUri`].OutputValue' --output text)"
echo "    ECR repo: $ECR_URI"

# -----------------------------------------------------------------------------
# Frontend build
# -----------------------------------------------------------------------------
echo "[5/8] building frontend..."
( cd "$ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build )
echo "    dist/ size: $(du -sh "$ROOT/frontend/dist" | cut -f1)"

# -----------------------------------------------------------------------------
# Backend image build (Lambda container image, linux/amd64).
# Uses Dockerfile.lambda — based on AWS's public.ecr.aws/lambda/python:3.12
# image so the Lambda runtime client is built in. Same backend code as the
# Fargate Dockerfile; Mangum adapter wraps the FastAPI app for Lambda
# invocations.
# -----------------------------------------------------------------------------
echo "[6/8] building + pushing backend Lambda image..."
docker buildx build --platform linux/amd64 \
    -t bedrock-ops-lens-backend:lambda \
    -f "$ROOT/backend/Dockerfile.lambda" \
    --provenance=false \
    --load "$ROOT"
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker tag bedrock-ops-lens-backend:lambda "$ECR_URI:latest"
docker push "$ECR_URI:latest"
echo "    pushed: $ECR_URI:latest"

# -----------------------------------------------------------------------------
# Pass 2: Main stack
#
# Parameters travel via a JSON file rather than --parameter-overrides KEY=VAL
# because zsh and bash alike eat colons inside unquoted Key=Value pairs (a
# previous deploy turned `...repo:latest` into `...repolatest`). JSON file
# format is colon-safe.
# -----------------------------------------------------------------------------
echo "[7/8] deploying main stack ($MAIN_STACK)..."

PARAMS_JSON="$(mktemp -t bol-params.XXXXXX.json)"
trap 'rm -f "$PARAMS_JSON"' EXIT
cat > "$PARAMS_JSON" <<EOF
[
  {"ParameterKey":"AllowedEmailDomains","ParameterValue":"$ALLOWED_EMAIL_DOMAINS"},
  {"ParameterKey":"BackendImageUri","ParameterValue":"$ECR_URI:latest"},
  {"ParameterKey":"BedrockLogsBucket","ParameterValue":"$BEDROCK_LOGS_BUCKET"},
  {"ParameterKey":"StackNamePrefix","ParameterValue":"$MAIN_STACK"},
  {"ParameterKey":"EdgeShaVersionArn","ParameterValue":"$EDGE_SHA_VERSION_ARN"},
  {"ParameterKey":"WebAclArn","ParameterValue":"$WEB_ACL_ARN"}
]
EOF

# `aws cloudformation deploy` accepts file:// only for tags and template-body —
# not parameters. Use create-change-set + execute-change-set, which do.
# Simpler: detect create vs update and call the matching API directly.
EXISTING_STATUS="$(aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"

# DELETE_FAILED almost always means Lambda@Edge replicas haven't drained yet
# (CloudFront keeps Lambda@Edge functions warm for ~30-90 min after the
# Distribution is gone). Re-issue delete with --retain-resources for the
# stuck function so the rest of the stack clears, then proceed as fresh.
if [[ "$EXISTING_STATUS" == "DELETE_FAILED" ]]; then
    echo "    previous stack is DELETE_FAILED — likely Lambda@Edge replica drain."
    STUCK="$(aws cloudformation describe-stack-resources --stack-name "$MAIN_STACK" --region "$REGION" \
      --query 'StackResources[?ResourceStatus!=`DELETE_COMPLETE`].LogicalResourceId' --output text)"
    echo "    retaining stuck resources: $STUCK"
    aws cloudformation delete-stack --stack-name "$MAIN_STACK" --region "$REGION" \
      --retain-resources $STUCK
    aws cloudformation wait stack-delete-complete --stack-name "$MAIN_STACK" --region "$REGION" || true
    EXISTING_STATUS=NONE
fi

if [[ "$EXISTING_STATUS" == "NONE" || "$EXISTING_STATUS" == "ROLLBACK_COMPLETE" || "$EXISTING_STATUS" == "DELETE_COMPLETE" ]]; then
    if [[ "$EXISTING_STATUS" == "ROLLBACK_COMPLETE" ]]; then
        echo "    previous stack is ROLLBACK_COMPLETE — deleting before re-create"
        aws cloudformation delete-stack --stack-name "$MAIN_STACK" --region "$REGION"
        aws cloudformation wait stack-delete-complete --stack-name "$MAIN_STACK" --region "$REGION"
    fi
    echo "    creating fresh stack"
    aws cloudformation create-stack \
        --stack-name "$MAIN_STACK" \
        --template-url "$MAIN_TEMPLATE_URL" \
        --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
        --parameters "file://$PARAMS_JSON" \
        --tags "Key=app,Value=bedrock-ops-lens" \
               "Key=stack,Value=$MAIN_STACK" \
        --region "$REGION" \
        --query 'StackId' --output text
    echo "    waiting for create-complete (this is the long pole — Aurora ~5 min, CloudFront ~5 min)"
    aws cloudformation wait stack-create-complete --stack-name "$MAIN_STACK" --region "$REGION"
else
    echo "    updating existing stack (status was $EXISTING_STATUS)"
    UPDATE_OUT="$(aws cloudformation update-stack \
        --stack-name "$MAIN_STACK" \
        --template-url "$MAIN_TEMPLATE_URL" \
        --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
        --parameters "file://$PARAMS_JSON" \
        --tags "Key=app,Value=bedrock-ops-lens" \
               "Key=stack,Value=$MAIN_STACK" \
        --region "$REGION" 2>&1)"
    if echo "$UPDATE_OUT" | grep -q "No updates are to be performed"; then
        echo "    (no template diff — skipping wait)"
    else
        echo "    waiting for update-complete"
        aws cloudformation wait stack-update-complete --stack-name "$MAIN_STACK" --region "$REGION"
    fi
fi

# Sync SPA to bucket created by the stack.
SPA_BUCKET="$(aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`SpaBucketName`].OutputValue' --output text)"
echo "    syncing frontend to s3://$SPA_BUCKET/"
aws s3 sync "$ROOT/frontend/dist/" "s3://$SPA_BUCKET/" --delete --quiet

# -----------------------------------------------------------------------------
# Roll the Backend Lambda alias to the just-deployed image.
#
# CFN owns the Backend Lambda's $LATEST (the container image). It also owns
# `BackendLambdaInitialVersion` and `BackendLambdaAlias` (named "live"),
# but it doesn't keep them in sync with $LATEST after the first deploy --
# AWS::Lambda::Version is immutable, and CFN won't auto-publish a new one.
#
# After every code update, we publish a new immutable version pointing at
# the new image, then move the "live" alias to that version. Provisioned
# concurrency on the alias automatically warms the new code -- new
# requests skip the cold start.
#
# Skip this when nothing rolled (the first-deploy CFN already created v1).
# -----------------------------------------------------------------------------
BACKEND_FN="${MAIN_STACK}-backend"
echo "    publishing new Backend Lambda version + rolling 'live' alias..."
NEW_VERSION="$(aws lambda publish-version \
    --function-name "$BACKEND_FN" \
    --description "Auto-published by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --region "$REGION" \
    --query Version --output text 2>/dev/null || true)"
if [[ -n "$NEW_VERSION" && "$NEW_VERSION" != "None" ]]; then
    aws lambda update-alias \
        --function-name "$BACKEND_FN" \
        --name live \
        --function-version "$NEW_VERSION" \
        --region "$REGION" \
        --query 'AliasArn' --output text >/dev/null
    echo "      → alias 'live' now points at version $NEW_VERSION"
    echo "      (provisioned concurrency takes ~30s to re-warm on the new version)"
else
    echo "    (no version change; alias 'live' unchanged)"
fi

# -----------------------------------------------------------------------------
# Roll the Ingester + Schema-init Lambdas to the just-pushed image.
#
# Both are PackageType=Image Lambdas pinned to ":latest". CFN resolves that
# tag to a digest at create time and does NOT re-pull when the tag string is
# unchanged, so on a code-only redeploy (same image tag, new digest) these
# Lambdas keep running the STALE image. update-function-code with the same
# :latest URI forces Lambda to resolve the tag to the new digest. The
# backend Lambda is handled above via publish-version; these two have no
# alias so a direct code update is the right tool.
# -----------------------------------------------------------------------------
for FN in "${MAIN_STACK}-ingester" "${MAIN_STACK}-schema-init"; do
    if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
        echo "    refreshing $FN to latest image digest..."
        aws lambda update-function-code \
            --function-name "$FN" \
            --image-uri "$ECR_URI:latest" \
            --region "$REGION" \
            --query 'LastUpdateStatus' --output text >/dev/null 2>&1 || true
        aws lambda wait function-updated --function-name "$FN" --region "$REGION" 2>/dev/null || true
    fi
done

# -----------------------------------------------------------------------------
# Initial ingester run.
#
# The schedule fires once a day, but on a fresh deploy the dashboard would
# show empty tabs until tomorrow. Invoke the Ingester Lambda once now so the
# user has data from minute zero. Synchronous, so any failure is visible.
#
# Skippable via SKIP_INITIAL_INGEST=1 if the customer wants a dry-run deploy.
# -----------------------------------------------------------------------------
if [[ -z "${SKIP_INITIAL_INGEST:-}" ]]; then
    echo "[8/8] initial ingest run..."
    INGESTER_NAME="${MAIN_STACK}-ingester"
    INGEST_OUT="$(mktemp -t bol-ingest.XXXXXX.json)"
    set +e
    aws lambda invoke \
        --function-name "$INGESTER_NAME" \
        --invocation-type RequestResponse \
        --cli-read-timeout 900 \
        --region "$REGION" \
        "$INGEST_OUT" >/tmp/bol-invoke-meta.json 2>&1
    INGEST_RC=$?
    set -e
    cat /tmp/bol-invoke-meta.json
    if [[ $INGEST_RC -ne 0 ]]; then
        echo "    WARNING: initial ingest invoke failed (rc=$INGEST_RC). Stack is up; data will populate on tomorrow's schedule."
    else
        echo "    ingester result:"
        if command -v jq >/dev/null 2>&1; then
            jq . "$INGEST_OUT" || cat "$INGEST_OUT"
        else
            cat "$INGEST_OUT"
        fi
    fi
    rm -f "$INGEST_OUT" /tmp/bol-invoke-meta.json
else
    echo "[8/8] SKIP_INITIAL_INGEST=1 set — skipping initial ingest"
fi

DASHBOARD_URL="$(aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' --output text)"
COGNITO_DOMAIN="$(aws cloudformation describe-stacks --stack-name "$MAIN_STACK" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`CognitoDomain`].OutputValue' --output text)"

echo
echo "============================================================================"
echo "✅ DEPLOYED"
echo "   Dashboard:   $DASHBOARD_URL"
echo "   Cognito:     $COGNITO_DOMAIN"
echo "============================================================================"
