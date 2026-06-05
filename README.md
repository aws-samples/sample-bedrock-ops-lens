# Bedrock Ops Lens

A Bedrock observability dashboard you can deploy in your own AWS account, plus an MCP server that exposes the same data to Claude Code, Cursor, and Kiro (CLI or IDE).

![Architecture](images/architecture.png)


## What it does

Cost Explorer rolls all of Bedrock into one line. CloudWatch is per-account. Invocation logs are JSON in S3. This dashboard joins those three signals so you can see per-account, per-model, per-tag attribution in one place. The MCP exposes the same data to your IDE, so you can ask the question instead of clicking through tabs.


## Two ways to use it

```mermaid
flowchart TB
    subgraph A["Tier A. MCP only"]
        direction TB
        A1["You, in your IDE"]
        A2["bedrock-lens-mcp<br/>running locally"]
        A1 --> A2
    end

    subgraph B["Tier B. Full dashboard"]
        direction TB
        B1["You, in a browser<br/>or in your IDE"]
        B2["Web dashboard<br/>+ bedrock-lens-mcp"]
        B3["Hosted backend<br/>with daily refresh"]
        B1 --> B2 --> B3
    end

    bottom[("Your AWS account.<br/>Bedrock, CloudWatch, Cost Explorer, Service Quotas.")]

    A2 --> bottom
    B3 --> bottom

    classDef tierBox fill:#fff8d6,stroke:#b89b1a,stroke-width:1px,color:#000
    classDef innerBox fill:#dcd6f7,stroke:#6c5ce7,stroke-width:1px,color:#000
    classDef sharedBox fill:#fff8d6,stroke:#b89b1a,stroke-width:1px,color:#000

    class A,B tierBox
    class A1,A2,B1,B2,B3 innerBox
    class bottom sharedBox
```

| Tier | Use it when |
|---|---|
| A. MCP only | You want quick answers in your IDE, no infrastructure. Cannot do heavy historical or tag-attributed work because there is no database. |
| C. Full dashboard | Finance, leadership, or anyone without AWS access needs the same insights. Includes the web UI and the MCP. |

Tier A is light. The MCP runs on your laptop and calls AWS APIs live. Useful for quick lookups but cannot do heavy historical work or per-tag cost attribution because there is no database behind it.

Tier B is everything else. The Cloudscape web dashboard, sign-in, CloudFront, daily ingester, Aurora, Memcached, and the same MCP wired up to talk to the hosted backend. Most teams deploy this so non-engineers can get the same insights without a terminal.


## Quick start

```bash
git clone https://github.com/aws-samples/sample-bedrock-ops-lens.git
cd sample-bedrock-ops-lens
ALLOWED_EMAIL_DOMAINS=yourcompany.com ./deploy.sh --yes
```

The script handles everything: VPC, Aurora, Memcached, Cognito, CloudFront, WAF, schema, ingester, and a first ingest run. About 12 minutes. It prints the dashboard URL when done.

Open the dashboard URL and sign up. Anyone whose email domain matches `ALLOWED_EMAIL_DOMAINS` can create their own account; the first verified user is auto-promoted to admin.


## Wiring up the MCP

Install the MCP server first.

```bash
cd mcp
pipx install -e .
```

Pick the option that matches how you deployed.

<details>
<summary><b>Option 1. Tier A. No deployment, uses your local AWS credentials</b></summary>

Best for: a quick, solo setup. The MCP calls AWS directly.

```bash
claude mcp add bedrock-lens -- bedrock-lens-mcp
```

</details>

<details>
<summary><b>Option 2. Tier B, no password (recommended if you have AWS credentials)</b></summary>

Best for: anyone with AWS credentials who deployed the stack. Uses SigV4 signing so there is no Cognito password to manage.

```bash
FN_URL=$(aws cloudformation describe-stacks \
  --stack-name BedrockOpsLens-<suffix> \
  --query 'Stacks[0].Outputs[?OutputKey==`BackendLambdaUrl`].OutputValue' \
  --output text)

claude mcp add bedrock-lens \
  --env BEDROCK_LENS_FUNCTION_URL="$FN_URL" \
  -- bedrock-lens-mcp
```

</details>

<details>
<summary><b>Option 3. Tier B with a Cognito password (for users without AWS credentials)</b></summary>

Best for sharing access with someone who does not have AWS credentials. They sign in with email and password instead.

Set your credentials as environment variables first, then add the server. Do not commit the password.

```bash
export BEDROCK_LENS_API=https://<distribution>.cloudfront.net
export BEDROCK_LENS_USER=you@yourcompany.com
export BEDROCK_LENS_PASSWORD=...   # paste the password here, do not check it in

claude mcp add bedrock-lens \
  --env BEDROCK_LENS_API="$BEDROCK_LENS_API" \
  --env BEDROCK_LENS_USER="$BEDROCK_LENS_USER" \
  --env BEDROCK_LENS_PASSWORD="$BEDROCK_LENS_PASSWORD" \
  -- bedrock-lens-mcp
```

</details>

Then ask Claude something like:

> Run the bedrock-lens health check.

> What was our Bedrock spend last 30 days, and which day had the biggest jump?

> Are we using any models that are Legacy or about to hit EOL?

> Run an ops review of the last 14 days and summarize the top 3 issues.


## Daily refresh

After deploy, EventBridge invokes the ingester every day at 05:00 UTC. The ingester reads CloudWatch metrics, Cost Explorer, Service Quotas, Bedrock APIs, and Bedrock invocation logs from S3, then writes everything into Aurora and bumps the cache generation. Open the dashboard the next morning, yesterday's data is there.

Manual backfill if you change the schedule or want a fresh run:

```bash
aws lambda invoke \
  --function-name BedrockOpsLens-<suffix>-ingester \
  --invocation-type RequestResponse --cli-read-timeout 900 \
  /tmp/out.json
```


## Multi-account data pipeline

The central Lambda pulls Bedrock data from every account you point it at. One script does the whole thing: it deploys a read-only `BedrockOpsLensReader` role into each account via a CloudFormation StackSet, reconfigures the central ingester to use those roles, and triggers the first ingest run synchronously so you see real data immediately.

```bash
./setup-pipeline.sh --scope <single|ou|org-root|accounts> [opts]
```

For Cost Explorer, no per-account role is needed at all — the management account's Cost Explorer is org-aware natively and the central Lambda calls it once.

<details>
<summary><b>Option 1. Just my own account (single)</b></summary>

The simplest case. No StackSet. Reader role deployed to the central account itself; ingester pulls from this one account.

```bash
./setup-pipeline.sh --scope single
```

</details>

<details>
<summary><b>Option 2. All accounts under one or more OUs (recommended for orgs)</b></summary>

Service-managed StackSet, deployed to the OUs you list. Auto-deploy is ON, so accounts joining the OU later are auto-onboarded. Run from the management account, or pass `--delegated-admin` from a delegated administrator account.

```bash
./setup-pipeline.sh --scope ou --ou-id ou-xxxx-yyyyyyyy
```

For multiple OUs, comma-separate them.

</details>

<details>
<summary><b>Option 3. Whole org root</b></summary>

Same as option 2 but targets every account in the organization. Useful for small orgs where OU-scoping isn't worth it.

```bash
./setup-pipeline.sh --scope org-root
```

</details>

<details>
<summary><b>Option 4. Explicit account list (no AWS Organizations)</b></summary>

Self-managed StackSet. Doesn't require AWS Organizations, but each member account needs the AWS-provided `AWSCloudFormationStackSetExecutionRole` pre-provisioned (one-time, per the [AWS docs](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-prereqs-self-managed.html)).

```bash
./setup-pipeline.sh --scope accounts \
  --accounts 111111111111,222222222222,333333333333
```

Or via file:

```bash
./setup-pipeline.sh --scope accounts \
  --accounts-file accounts.txt
```

</details>

The script is idempotent — re-run any time accounts are added or removed. Use `--dry-run` to preview without touching anything, `--skip-ingest` to skip the post-rollout ingest run.

### What `setup-pipeline.sh` does

1. Validates the central stack exists (auto-discovers from `.deploy-stack-name`).
2. Calls `scripts/setup-multi-account.py` to roll out the reader role via the right CloudFormation API for the chosen scope (service-managed StackSet for `ou` / `org-root`, self-managed for `accounts`, plain stack for `single`).
3. Reconfigures `MONITORED_ACCOUNTS_MODE` on the central ingester Lambda (`discover-org` for `ou`/`org-root`, `explicit` for `accounts`, `single` for `single`).
4. Triggers one ingest run synchronously and prints the per-module summary.

After this, EventBridge fires the ingester daily at 05:00 UTC. Re-run the script any time the OU shape changes.

### Scale

Validated end-to-end up to ~100 accounts in one ingester Lambda. Past that, the central Lambda starts hitting CloudWatch (50 TPS) and Service Quotas (5 TPS) per-account API limits.

For larger orgs (200+ accounts), shard by OU and run one StackSet per shard, each producing its own ingester:

```bash
./setup-pipeline.sh --scope ou --ou-id ou-engineering-...
./setup-pipeline.sh --scope ou --ou-id ou-data-...
./setup-pipeline.sh --scope ou --ou-id ou-experimental-...
```

For very large customers (500+ accounts) the pull architecture becomes the wrong fit. Reach out and we'll point you at the push-mode pattern (CW Metric Streams → Firehose → S3 → central ingester).


## Dashboard tabs

| Tab | Answer |
|---|---|
| Overview | Total requests, accounts, tokens, error rate, spend in the window |
| Quotas | Applied versus default quotas, peak usage, severity-coded utilisation |
| Cost Insights | Real Cost Explorer dollars, daily trend, by-account and by-model breakdowns |
| Health and Errors | Errors by model, by account, daily and hourly trends |
| Latency | p50, p90, p99 by model |
| Capacity and Adoption | CRIS adoption, throttle rates, prompt caching opportunities, Claude 4 burndown risk |
| Model Insights | Per-model deep dive: requests, tokens, cache hit rate, errors, accounts |
| Model Lifecycle | Live ListFoundationModels joined with usage, timeline of legacy and EOL bands |
| Ops Review | LLM-synthesized executive brief covering the top 3 issues |
| Settings | Auth identity, ingestion freshness, region and account scope, pinned tag keys |


## Cost

Idle, with Aurora paused, the stack runs around fifty dollars per month. NAT Gateway is the largest fixed cost at about thirty-two. Aurora is between zero and forty-five depending on activity. ElastiCache Memcached is about thirteen. Lambda, CloudFront, S3, Cognito, and WAF together are around five.


## Verify

```bash
DASH_URL=$(aws cloudformation describe-stacks \
  --stack-name BedrockOpsLens-<suffix> \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' \
  --output text)

curl -sf "$DASH_URL/api/health"
```

For end-to-end UI validation:

```bash
cd frontend
DASH_URL="$DASH_URL" \
TEST_EMAIL="you@yourcompany.com" \
TEST_PASS="$BEDROCK_LENS_PASSWORD" \
  npx playwright test tests/deployed-smoke.spec.js --project=chromium --reporter=list
```


## Local development

```bash
docker compose up -d
psql -d bedrock_lens -f db/schema.sql
psql -d bedrock_lens -f db/partitions.sql
cd backend && PYTHONPATH=.. uvicorn app.main:app --port 8001
cd frontend && npm install && npm run dev
```

Frontend at http://localhost:5173. Same FastAPI app and same ingester code that runs in Lambda runs locally under uvicorn.


## Tear down

```bash
./deploy.sh destroy
```

Cognito User Pool and the SPA bucket survive the delete on purpose, so re-deploys don't reset users. Delete them by hand if you want a fully clean account.


## License

MIT License. See `LICENSE` for details.
