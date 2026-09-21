# Bedrock Ops Lens

**A quota limit can become an application outage.**

Within an AWS account and Region, workloads using the same Amazon Bedrock model
can compete for request and token quota. A chatbot and a document summarizer can
make the same number of calls yet consume very different amounts of token quota.
Prompt length, response length, and model-specific token accounting mean request
counts alone cannot tell you how much headroom remains.

The evidence needed to act is scattered: usage, throttles, and latency in
CloudWatch; limits in Service Quotas; billed spend in Cost Explorer; and request
and caller context in S3 invocation logs. Across accounts and Regions, operators
must connect those signals to identify the model under pressure, the workload
driving demand, and the appropriate response. That manual work slows incident
response and makes capacity and cost planning harder.

**Bedrock Ops Lens brings those signals into one operational view.** It is an
open-source solution you deploy in your own AWS account to investigate usage,
prioritize risks, and decide what to change across configured accounts and Regions.

- **Investigate:** compare quota utilization, throttles, errors, latency, billed
  spend, and model lifecycle in the dashboard. Add caller or workload attribution
  with invocation logging or client telemetry.
- **Act on findings:** after ingestion, Lens checks for quota pressure, throttles,
  cost changes, and models approaching end of life. Findings include suggested
  remediation, with CLI commands and console links where available for you to
  review and execute. Optional Amazon SNS delivery brings findings to your
  notification workflows.
- **Use telemetry in your own agents:** the MCP server exposes operational data
  to Claude Code, Cursor, Kiro, and other MCP clients, so you can build workflows
  around the same evidence.
- **Review priorities:** Ops Review generates an AI-written operational brief
  grounded in the collected telemetry.

![Dashboard walkthrough with synthetic demo data](images/demo.gif)

The walkthrough and [screenshot gallery](docs/screenshots.md) use synthetic demo
data. Optional [client telemetry](docs/workloads.md) adds workload attribution
and direct Anthropic/OpenAI API usage, labelled as client-reported data.

## Two ways to use it

| Choose | What you get | Start here |
|---|---|---|
| **Tier A: MCP only** | Live AWS API lookups using your local credentials; no hosted dashboard or ingestion database | [Install and connect the MCP](mcp/README.md#install) |
| **Tier B: Full dashboard + MCP** | Browser dashboard, sign-in, stored history, scheduled ingestion, and MCP access | [Quick start](#quick-start) |

Tier A has [limits on history, attribution, and analysis](mcp/README.md#tier-a-constraints).
Tier B supplies the stored data needed for those views.

The full deployment uses Lambda, Aurora Serverless v2, ElastiCache, CloudFront,
S3, Cognito, WAF, and EventBridge, with SNS for optional notifications.

![Bedrock Ops Lens architecture](images/architecture.png)

## Quick start

For the full dashboard, prepare AWS CLI v2 credentials, Python with `boto3`,
Node.js/npm, and a running Docker engine with Buildx. See the
[deployment prerequisites](docs/deployment.md#prerequisites) for details.

```bash
git clone https://github.com/aws-samples/sample-bedrock-ops-lens.git
cd sample-bedrock-ops-lens
cp config.example.yaml config.yaml
```

Edit `config.yaml` for your deployment Region and
[monitored Regions](docs/deployment.md#choose-monitored-regions). One central
deployment collects CloudWatch metrics and quotas from each configured account
and Region. The following first deployment starts in **single-account mode without AWS
Organizations discovery**. Replace the email domain with your own:

```bash
ALLOWED_EMAIL_DOMAINS=yourcompany.com \
  ENABLE_ORGANIZATIONS_DISCOVERY=false \
  ./deploy.sh --yes
```

The script builds the application, provisions the central stack, initializes
the database, and runs an initial ingest. It prints the dashboard URL and the
`admin-create-user` and `admin-add-user-to-group` commands for your first admin.
Complete both commands to add that user to `bedrock-lens-admins`.
After changing group membership, sign out and back in.

**Sign-up is admin-create-only by default.** To enable self-service sign-up,
set `COGNITO_SELF_SIGNUP=enabled` for deployment. Follow the
[sign-in instructions](docs/deployment.md#sign-in) for the domain allowlist and
first-admin behavior.

[Verify the deployment](docs/deployment.md#verify), then choose the account
scope below. For an Organizations-based rollout, the
[account-scope guide](docs/account-scope.md) explains how to enable discovery.

## Multi-account data pipeline

Each target needs a reader role that trusts the central ingester's actual IAM
role. Choose the deployment method your organization permits:

| Option | Setup route | Organizations required? | StackSets required? | Instructions |
|---|---|---|---|---|
| **1** | Central account only | No | No | [Single account](docs/account-scope.md#option-1-single-account) |
| **2** | Reader rollout to one or more OUs | Yes | Yes, service-managed | [OU setup](docs/account-scope.md#option-2-organizational-units) |
| **3** | Whole organization root | Yes | Yes, service-managed | [Org-root setup](docs/account-scope.md#option-3-organization-root) |
| **4a** | Explicit account list | **No, including bootstrap** | Yes, self-managed | [Role prerequisites and setup](docs/multi-account-setup.md) |
| **4b** | Explicit account list; each owner deploys the reader | **No** | **No** | [Commands for central and target accounts](docs/manual-multi-account-setup.md) |

### Option 4a. Deploy reader roles through self-managed StackSets

The central account needs a StackSet administration role, and each target needs
an execution role. The central administration role must exist first. Follow the
[complete 4a guide](docs/multi-account-setup.md) before running setup.

### Option 4b. Each account owner deploys the reader role manually

For accounts A–E with C as central, the owners of A, B, D, and E create reader
roles trusting C's existing ingester role. C then configures ingestion with
`--scope accounts --skip-rollout`. No StackSet administration or execution roles
are needed. The [4b guide](docs/manual-multi-account-setup.md) contains the exact
commands to run in each account and in C.

Both routes use `ReaderRoleName` from the central stack, defaulting to
`BedrockOpsLensReader`. See [custom reader names](docs/multi-account-setup.md#custom-reader-role-name)
before selecting a different name.

<a id="what-setup-pipelinesh-does"></a>
<a id="scale"></a>
See the [setup behavior and scale guidance](docs/account-scope.md#what-setup-pipelinesh-does)
for validation, ingestion results, reruns, and the Lambda runtime limit.

## Before relying on the data

- **Billing access is separate.** Reader roles provide operational reads; they
  do not let an arbitrary central account retrieve unrelated accounts' bills.
- **Attribution needs its source.** Caller and request-tag views require
  invocation logging. Workload telemetry requires an emitter and is
  client-reported. See [which source to use](docs/dashboard-guide.md#which-attribution-source-when).
- **Check coverage.** Confirm each expected account and Region in ingestion
  results. Empty usage may mean no traffic; incomplete or failed runs need
  investigation. See [runtime verification](docs/manual-multi-account-setup.md#5-verify-the-runtime-path-and-maintain-access).
- **Deployment incurs AWS charges.** The default Aurora minimum is 0.5 ACU;
  the full stack does not become free when idle. See [cost and teardown](docs/deployment.md#cost).

## Documentation

The commands in these guides are run from the repository root unless stated otherwise.

| Task | Guide |
|---|---|
| <a id="verify"></a><a id="tear-down"></a>Deploy, sign in, verify, or remove the stack | [Deployment](docs/deployment.md) |
| <a id="wiring-up-the-mcp"></a>Connect an IDE using AWS credentials or Cognito | [MCP installation and authentication](mcp/README.md) |
| Choose accounts, understand account names, or plan fleet size | [Account scope](docs/account-scope.md) |
| <a id="daily-refresh"></a><a id="notifications-and-findings"></a>Refresh data and configure alerts | [Operations](docs/operations.md) |
| <a id="dashboard-tabs"></a><a id="how-quota-consumption-is-measured"></a><a id="which-attribution-source-when"></a>Understand dashboard views, quotas, and attribution | [Dashboard and data guide](docs/dashboard-guide.md) |
| <a id="workloads-per-workload-attribution-and-client-telemetry-optional"></a><a id="setup-3-steps"></a><a id="how-this-relates-to-aws-native-attribution"></a>Add workload or direct-provider telemetry | [Workloads setup](docs/workloads.md), [client emitters](tools/client-telemetry/README.md) |
| Compare Bedrock Runtime and Mantle telemetry | [Endpoint comparison](docs/mantle-vs-runtime-telemetry.md) |
| <a id="local-development"></a><a id="tests"></a>Develop locally, run tests, or contribute | [Contributing](CONTRIBUTING.md#local-development) |

## Cost

NAT Gateway, Aurora, ElastiCache, and other deployed resources can incur charges
without application traffic. Usage, storage, data transfer, and Ops Review model
invocations add costs. Use the [deployment cost notes](docs/deployment.md#cost)
and [AWS Pricing Calculator](https://calculator.aws/) for your Region and workload.

## Screenshots

[Open the full gallery](docs/screenshots.md). All images show synthetic demo
data generated by `db/seed.py`.

| View | View |
|---|---|
| <a id="overview"></a>[Overview](docs/screenshots.md#overview) | <a id="quotas"></a>[Quotas](docs/screenshots.md#quotas) |
| <a id="cost-insights"></a>[Cost Insights](docs/screenshots.md#cost-insights) | <a id="health--errors"></a>[Health & Errors](docs/screenshots.md#health--errors) |
| <a id="latency"></a>[Latency](docs/screenshots.md#latency) | <a id="capacity--adoption"></a>[Capacity & Adoption](docs/screenshots.md#capacity--adoption) |
| <a id="model-lifecycle"></a>[Model Lifecycle](docs/screenshots.md#model-lifecycle) | <a id="model-insights"></a>[Model Insights](docs/screenshots.md#model-insights) |
| <a id="usage--custom-attributes"></a>[Usage · Custom Attributes](docs/screenshots.md#usage--custom-attributes) | <a id="by-user--app--principal"></a>[By User / App / Principal](docs/screenshots.md#by-user--app--principal) |
| <a id="agents--mcp"></a>[Agents & MCP](docs/screenshots.md#agents--mcp) | <a id="compliance-guardrails"></a>[Compliance (Guardrails)](docs/screenshots.md#compliance-guardrails) |
| <a id="governance"></a>[Governance](docs/screenshots.md#governance) | <a id="ops-review-ai-agent"></a>[Ops Review (AI Agent)](docs/screenshots.md#ops-review-ai-agent) |

## License

MIT License. See [LICENSE](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md) for
contribution guidelines and [SECURITY.md](SECURITY.md) for reporting security issues.
