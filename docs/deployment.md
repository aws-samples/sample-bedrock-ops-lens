# Deploy and manage the central dashboard

[Home](../README.md) · [Account scope](account-scope.md) · [Operations](operations.md)

This guide covers the full dashboard deployment (Tier B). For local AWS lookups
without a hosted stack, use the [MCP-only installation](../mcp/README.md#install).
Run commands from the repository root unless a step says otherwise.

## Prerequisites

- AWS CLI v2 with credentials for the account that will host Lens. The operator
  needs deployment permissions for the resources in the CloudFormation templates,
  including IAM; the scripts do not grant permissions to their caller.
- A running Docker engine with Buildx, Node.js/npm for the frontend build, and
  Python 3.12 with `boto3` for the deployment and account-setup tooling.
- A local checkout and `config.yaml` copied from `config.example.yaml`.
- An allowed email domain for users of the dashboard.

Confirm your account before deployment, using the intended CLI profile:

```bash
aws sts get-caller-identity --profile lens-central --query Account --output text
```

Profiles such as `lens-central` are examples of credentials you configure;
the repository does not create them.

## Deploy

Use the commands in the [README quick start](../README.md#quick-start). Before
running them, edit `config.yaml` for `deploy_region`, collection Regions, and
other settings. `DEPLOY_REGION` overrides `deploy_region`; otherwise deployment
falls back to `us-east-1`. The script deliberately ignores a leftover
`AWS_REGION` or `AWS_DEFAULT_REGION` when choosing the deployment Region.

The quick start explicitly sets `ENABLE_ORGANIZATIONS_DISCOVERY=false`, starting
in single-account mode. Without that override, a new central stack defaults to
Organizations discovery. Lambda environment settings take precedence over
`config.yaml` for account selection. Follow the [account-scope guide](account-scope.md)
to configure an explicit list or an Organizations rollout after deployment.

The script builds the frontend and Lambda image, provisions the VPC, Aurora,
ElastiCache, Cognito, CloudFront, WAF, ingestion schedule, and other resources,
initializes the schema, and runs an initial ingest. Allow time for image builds
and AWS resource creation; use the reported CloudFormation status to judge
completion. It prints the dashboard URL and sign-in commands when it finishes.

### Deployment settings

| Setting | Purpose |
|---|---|
| `AWS_PROFILE` | Credentials for the central account |
| `DEPLOY_REGION` | Override `config.yaml`'s deployment Region |
| `ALLOWED_EMAIL_DOMAINS` | Domain allowlist for dashboard users |
| `COGNITO_SELF_SIGNUP=enabled` | Opt into a public sign-up form gated by that allowlist |
| `ENABLE_ORGANIZATIONS_DISCOVERY=false` | Omit Organizations permissions and start in single-account mode |
| `BEDROCK_OPS_LENS_ROLE_NAME` | Set the central `ReaderRoleName`; target roles must match |
| `SKIP_INITIAL_INGEST=1` | Defer the initial ingest while preparing target roles or data sources |
| `STACK_NAME_SUFFIX` | Select the central stack suffix instead of `.deploy-stack-name` |

`ENABLE_ORGANIZATIONS_DISCOVERY` and `ReaderRoleName` are preserved on redeploy
when their environment overrides are omitted. Central redeployment can reset
Lambda's explicit account list, so rerun the appropriate account-setup command
afterward. For custom roles, follow the
[reader-name instructions](multi-account-setup.md#custom-reader-role-name).

### Choose monitored Regions

`deploy_region` selects the Region hosting the central stack.
`monitored_regions` selects where the CloudWatch and Service Quotas collectors
read data for each monitored account. One central deployment can collect from
multiple Regions, including in single-account mode.

For example, set this in `config.yaml` to collect from three Regions:

```yaml
monitored_regions:
  preset: explicit
  regions:
    - us-east-1
    - us-east-2
    - us-west-2
```

The example configuration defaults to `us-eu-apac`, a fixed list of eight
Regions. `us-major` selects the three Regions above. See
[`config.example.yaml`](../config.example.yaml) for the available presets.
Collection still requires access to each selected account and Region.

`config.yaml` is copied into the Lambda image during deployment. After changing
it, rerun `deploy.sh` to rebuild and deploy the image, then restore your account
scope as described above. Lambda environment overrides
`MONITORED_REGIONS_PRESET` and `MONITORED_REGIONS_LIST`, if set, take precedence
over the file.

Source coverage differs: invocation logs use `BEDROCK_LOGS_BUCKET` and
`BEDROCK_LOGS_REGION`; client telemetry uses its configured bucket and
`PROXY_EVENTS_REGIONS`. Neither automatically inherits `monitored_regions`.
Cost Explorer reads the billing data accessible to the central account and
groups it by account and service, rather than looping over monitored Regions.
Check the [ingestion results](operations.md#daily-refresh) for actual coverage.

## Sign-in

Sign-in is **admin-create-only by default**, with no public sign-up form.
`deploy.sh` prints the exact commands for the deployed user pool:

1. `aws cognito-idp admin-create-user` creates a user.
2. `aws cognito-idp admin-add-user-to-group --group-name bedrock-lens-admins`
   grants that user admin access.

Both steps are needed: `admin-create-user` does not fire the PostConfirmation
trigger that performs the first-admin bootstrap for self-service sign-up.
Complete the temporary-password flow on first sign-in. Group membership is
included in the sign-in token, so sign out and back in after a group change.

To enable self-service sign-up, set `COGNITO_SELF_SIGNUP=enabled` for deployment.
Users whose email domains match `ALLOWED_EMAIL_DOMAINS` can then sign up; the
first verified user is auto-promoted to admin. The domain allowlist applies in
both modes.

## Verify

Use the stack name printed by deployment and its Region:

```bash
LENS_STACK_NAME=BedrockOpsLens-example  # replace with your actual stack name
LENS_REGION=us-east-1
DASH_URL="$(aws cloudformation describe-stacks \
  --profile lens-central --region "$LENS_REGION" \
  --stack-name "$LENS_STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' \
  --output text)"

curl -sf "$DASH_URL/api/health"
```

Sign in and inspect ingestion freshness and the expected accounts/Regions.
A health response verifies API availability; use the
[ingestion checks](operations.md#daily-refresh) to establish data coverage.

For the deployed UI smoke test, install the frontend dependencies and Playwright's
Chromium browser, then run from `frontend/` with credentials for your test user:

```bash
cd frontend
npm install
npx playwright install chromium
DASH_URL="$DASH_URL" \
TEST_EMAIL="you@yourcompany.com" \
TEST_PASS="$BEDROCK_LENS_PASSWORD" \
  npx playwright test tests/deployed-smoke.spec.js --project=chromium --reporter=list
```

Set `BEDROCK_LENS_PASSWORD` in your local environment; do not commit credentials.

## Cost

The full stack incurs AWS charges even without application traffic. The current
[central template](../infra/cloudformation.yaml) sets Aurora Serverless v2 to a
**minimum of 0.5 ACU**, keeping the writer running rather than auto-pausing.
NAT Gateway and ElastiCache also have ongoing costs. WAF, storage, requests,
data transfer, and model invocations for Ops Review contribute additional costs.

Use the [AWS Pricing Calculator](https://calculator.aws/) with your deployment
Region, resource sizes, and expected traffic. A single idle-price estimate does
not cover every configuration. MCP-only mode avoids the hosted stack but can
still incur charges for the AWS APIs it calls.

## Tear down

From the checkout associated with the deployment:

```bash
./deploy.sh destroy
```

Confirm the account, Region, and stack names printed by the script before
approving deletion. Check the final CloudFormation statuses: Lambda@Edge replicas
can delay deletion of the edge stack, and the script may finish before that
stack has finished deleting.

Retention is deliberate. The central template retains the Cognito user pool,
SPA and access-log buckets, and the Secrets Manager KMS key; Aurora has a snapshot
deletion policy. Other bootstrap stacks have their own retention settings.
Review the [templates](../infra/) and deletion output for retained resources,
snapshots, or failed deletions before concluding that all charges have stopped.
Account reader roles and StackSet prerequisites have their own owners and
lifecycles; removing the central stack does not remove them automatically.
