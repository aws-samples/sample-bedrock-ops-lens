# Workloads and client telemetry

[Home](../README.md) · [Attribution sources](dashboard-guide.md#which-attribution-source-when) · [Client emitters](../tools/client-telemetry/README.md)

The Workloads tab answers **"which of my use-cases is driving usage, throttling,
and latency"** - CloudWatch can't, because it's keyed by model, not by your
application. It needs a shared layer in front of your model calls (LiteLLM, a
gateway, an SDK wrapper) that emits **one metadata-only event per request** to
S3. No proxy layer → this tab stays empty; everything else works normally.

**What you get:** pivot usage by any attribute you emit (`workload`, `env`,
`team`, `cost_center`, …) with tokens, throttle rate, latency, and TPM quota
utilization per value. Events can also cover traffic AWS-side sources can't
see: `bedrock-mantle` latency/TTFT and direct `anthropic-api` / `openai-api`
calls, rolled up in a "Usage by provider" view. These numbers are
**client-reported** (the UI labels them); AWS-metered sources stay the
billing/quota truth.

## Setup (3 steps)

1. **Emit events.** Easiest: already on LiteLLM? Drop in the ready-made
   callback from [`tools/client-telemetry/`](../tools/client-telemetry/) (also
   has OTEL-collector and Claude Code on-ramps). Building your own? Copy
   `tools/reference-proxy/` - one NDJSON line per request to:

   ```
   s3://<your-bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl
   ```
   ```json
   {"ts":"2026-07-04T18:03:22Z",
    "dimensions":{"workload":"flights-search","env":"prod","business_unit":"travel"},
    "model":"anthropic.claude-opus-4-8","endpoint":"runtime","region":"us-east-1",
    "input_tokens":812,"output_tokens":143,"cache_read_tokens":0,
    "status":200,"throttled":false,"latency_ms":940,"request_id":"msg_..."}
   ```

   `dimensions` holds whatever attributes you slice by. `endpoint` is
   `runtime`, `mantle`, `anthropic-api`, or `openai-api`. Optional `ttft_ms`,
   `retry_attempts`, `cost_usd_est` enable the TTFT, retry, and estimated-cost
   columns. Metadata only - no prompt or response text ever leaves your proxy.

2. **Grant read access.** For a cross-account bucket, its policy must allow
   the ingester role to use `s3:GetObject` on the event-object prefix and
   `s3:ListBucket` on the bucket ARN. Listing can be restricted with an S3 prefix
   condition. The ingester's identity policy must also allow these reads; the
   central template grants them for `ProxyEventsBucket`. If the objects use a
   customer-managed KMS key, configure its key policy and decryption permission.
   Lens reads the delivered events; it does not sit in the request path.

3. **Deploy pointing at the bucket:**
   ```bash
   export PROXY_EVENTS_BUCKET=your-genai-proxy-events
   export PROXY_EVENTS_REGIONS=us-east-1,us-west-2
   ./deploy.sh --yes
   ```
   Leave `PROXY_EVENTS_BUCKET` unset to skip proxy-event ingestion.

## How this relates to AWS-native attribution

AWS's native mechanisms ([Bedrock cost
management](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-management.html))
answer **dollars** by principal / inference profile / Mantle Project - and this
toolkit uses them where they fit. What they don't emit is **throttle rate,
latency, or TPM quota utilization per workload**, and they don't cover
non-Bedrock traffic. The two paths are complements:

| | AWS-native (`requestMetadata` tags) | Proxy / client events |
|---|---|---|
| Setup | Tag calls + invocation logging on - no proxy | Emitter (LiteLLM callback, OTEL, or your gateway) |
| Coverage | `bedrock-runtime` only | runtime + mantle + direct Anthropic/OpenAI APIs |
| Metrics | Tokens + volume | + throttle %, latency, TTFT, retries, est. cost, and TPM quota utilization for Bedrock paths (AWS quotas do not apply to direct-provider calls, which are reported separately with no limit) |
| Freshness | At the next ingest (daily by default) | At the next ingest (daily by default) |
| Trust | AWS-witnessed | Client-reported |

Already running invocation logging with tagged requests? You get
usage-by-attribute with zero proxy work - pick "Option 1" in Settings. Want
throttle/latency/quota per workload or non-Bedrock coverage? Emit events -
"Option 2". Running both is normal.

> **Transport note:** the event shape is transport-agnostic; S3 NDJSON is the
> supported transport today (simplest cross-account read-only access, no
> per-metric cardinality cost). CloudWatch Logs / custom-metric readers could
> be added without changing the data model.
