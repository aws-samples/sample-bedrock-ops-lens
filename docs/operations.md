# Refresh data and configure notifications

[Home](../README.md) · [Deployment](deployment.md) · [Account scope](account-scope.md)

## Daily refresh

By default, EventBridge invokes the ingester daily at 05:00 UTC. It collects
CloudWatch metrics, Cost Explorer, Service Quotas, Bedrock APIs, and configured
S3 invocation logs, writes the results to Aurora, and bumps the cache generation.
Data availability depends on source permissions, activity, and ingest completion.

For a manual refresh, use your deployed stack name and Region:

```bash
LENS_STACK_NAME=BedrockOpsLens-example  # replace with your actual stack name
LENS_REGION=us-east-1
aws lambda invoke \
  --profile lens-central --region "$LENS_REGION" \
  --function-name "${LENS_STACK_NAME}-ingester" \
  --invocation-type RequestResponse --cli-read-timeout 900 \
  /tmp/bedrock-ops-lens-ingest.json
```

Inspect both the invocation metadata and the JSON response in the output file.
A successful API invocation alone does not establish that every ingestion
module succeeded. Check `FunctionError`, the overall status, each module's
return code, and logs for each expected account and Region.

An explicit `incomplete_reason: time_budget` from invocation logs means completed
objects were recorded and remaining work can resume. Other nonzero return codes
remain failures. Use the [runtime verification guidance](manual-multi-account-setup.md#5-verify-the-runtime-path-and-maintain-access)
for cross-account collection.

## Notifications and findings

After ingestion, Lens evaluates data against your configured thresholds for
quota utilization, throttles, cost changes, and models approaching end of life.
Findings appear in the bell menu and include suggested remediation, a CLI command,
and a console link where available. You review and execute those actions.

Findings resolve when the condition clears. Notifications fire on open/resolve
transitions rather than repeating on every run while a condition persists.
Freshness follows ingestion; this is not a continuous stream of every request.

For delivery, an admin sets an SNS topic ARN in **Settings → Notifications** and
uses **Send test notification**. Use a topic allowed by the central deployment's
IAM policy. Email subscribers receive a digest; consumers such as Lambda,
webhooks, or EventBridge receive structured JSON. Those consumers can connect
to Slack, PagerDuty, or ticketing workflows. Signed-in users can subscribe their
email from the same page.

The quota finding joins CloudWatch usage with applied Service Quotas limits;
cost and lifecycle findings use additional data sources. CloudWatch alarms are
still appropriate for direct, minute-level thresholds on individual metrics.
See [quota measurement](dashboard-guide.md#how-quota-consumption-is-measured)
for the meaning and limits of the utilization numbers.
