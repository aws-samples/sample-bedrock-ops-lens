# Understand the dashboard and its data

[Home](../README.md) · [Screenshots](screenshots.md) · [Workloads setup](workloads.md)

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
| Workloads | Per-workload / per-user usage, throttle, and latency - **requires a GenAI proxy or client telemetry** (see the [setup guide](workloads.md)). Includes direct **anthropic-api / openai-api** traffic and a "Usage by provider" rollup across all paths. Also per-IAM-principal callers (from invocation logs) and per-project Mantle chargeback |
| By User | Per-caller attribution from invocation-log identity: by app/group (role), user (session), or full principal |
| Agents & MCP | AgentCore runtimes and MCP gateway tools: invocations, sessions, errors, latency, real billed cost |
| Compliance | Guardrails interventions by policy type, guardrail, and daily trend |
| Governance | Declarative registry (`db/registry.yaml`) reconciled against observed usage: compliant, drift, undeclared (shadow AI) |
| Ops Review | An AI agent reviews the fleet's findings and writes an executive brief covering the top 3 issues |
| Settings | Auth identity, ingestion freshness, region and account scope, pinned tag keys |

Two notes on the By User tab. The "user" axis is the `sts:AssumeRole` session name, which the caller chooses - it is audit-grade only if you enforce it (IAM condition on `sts:RoleSessionName`, or IAM Identity Center federation, which pins it to the login); the "group" axis (the role itself) cannot be faked. And since it shows person-level usage to every signed-in user, check your organization's privacy requirements before enabling broad access.

Views populate from the sources you configure and can access. Caller and
request-tag attribution require invocation logs; proxy/client views require the
[Workloads setup](workloads.md). Governance also uses `db/registry.yaml`, and
Ops Review requires access to the selected Bedrock model.

## How quota consumption is measured

Output tokens do not always burn one quota token each. On some models one output
token consumes 10 or 15, so a dashboard that counts raw tokens can report a
workload at 8% of its limit when it is actually at 160%. Bedrock Ops Lens picks
its number per hour, in this order:

1. **AWS's own `EstimatedTPMQuotaUsage`**, when CloudWatch published a datapoint
   for that hour. AWS computes it with the current policy — cache-write tokens
   and the output multiplier already included — so nothing is applied on top.
   Reported as `aws_estimate`.
2. **Reconstruction**, when there is no datapoint:
   `uncached input + cache-write + output × burndown rate`. Cache *reads* never
   count. Reported as `reconstructed`.
3. **Nothing.** If neither is available the hour is excluded and the coverage gap
   is stated rather than folded into a total that looks complete.

Every response carries the source it used, and a series that mixes both says
`mixed` — one AWS-measured hour does not let a mostly-reconstructed chart claim
to be measured. Per-workload attribution from proxy telemetry always
reconstructs, because a model-level CloudWatch aggregate has no workload
dimension.

Hourly data means these are hourly-average per-minute rates, not measured
minute peaks. The API says so in `rate_basis`; the UI repeats it.

**Editing the rates.** The multipliers are data, not code: **Settings → Quota
burndown rates**. An admin can add a SKU or change a rate and the backend and the
scheduled findings job pick it up within 60 seconds — no rebuild, no redeploy.
Entries carry an *effective from* date (so a change today does not silently
re-rate last month's charts) and a separate *doc verified* date. Models with
traffic but no verified entry are listed on that screen, which is how a newly
launched SKU gets noticed. Anything falling back to the bundled table is labelled
unverified rather than presented as confirmed AWS policy.

Rates are seeded from the
[AWS token-burndown documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html)
on first boot; "Restore AWS defaults" puts them back.

Note AWS's own caveat: `EstimatedTPMQuotaUsage` is an approximation and does not
reflect the reservation-based accounting that actually drives throttling
decisions. Use it alongside observed throttles, not instead of them.

## Which attribution source when?

Bedrock Ops Lens has several ways to answer "who / what is driving usage" -
deliberately, because they differ in coverage and trust. Quick guide:

| You want to know… | Look at | Data source | Trust level | Needs |
|---|---|---|---|---|
| Which **team/person** called Bedrock (audit-friendly) | **By User** tab | Invocation-log `identity.arn` | AWS-witnessed | Invocation logging on |
| Usage by **workload / env / cost-center** (no proxy) | **Custom Attributes** tab (tags source) | Invocation-log `requestMetadata` | AWS-witnessed | Logging on + callers tag requests |
| Usage by any attribute **incl. throttle / latency / quota** | **Custom Attributes** tab (proxy source) | Proxy / client events | Client-reported | Gateway (e.g. LiteLLM callback) or OTEL emitter |
| Per-person **throttle, TTFT, retries, est. cost** | **Custom Attributes** tab, pivot by `user` | Proxy / client events | Client-reported | Emitter sends `user` dim |
| **Mantle** or **direct Anthropic/OpenAI API** traffic | **Custom Attributes** tab (+ By-Provider panel) | Client events only | Client-reported | Emitter (AWS-side sources can't see this) |
| Real **dollars** by account/service | **Cost Insights** tab | Cost Explorer | AWS-billed | Billing permissions and the appropriate account scope |

AWS-side metrics, logs, and bills provide evidence about the activity each
source observes. Request metadata and role-session names are caller-supplied;
interpret those labels according to your identity and tagging controls.
Client-reported telemetry covers only instrumented requests, including optional
direct-provider traffic, and is labelled separately in the UI.

Coverage varies by endpoint and source. See the
[Runtime/Mantle comparison](mantle-vs-runtime-telemetry.md) and
[Workloads setup](workloads.md) before comparing or combining views.

For implementation details of the quota drill-down, see the
[developer reference](quota-drilldown-implementation.md).
