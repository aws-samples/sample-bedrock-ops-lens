# `bedrock-runtime` vs `bedrock-mantle` — Telemetry & API Comparison

Reference for what observability data exists on each Bedrock endpoint, and
which API path to call for each model. Combines public AWS docs with
**live field-test findings** (us-east-1) on
**2026-06-03**. Where field tests contradict or refine docs, the field
result wins; where field tests confirm docs, both are cited.

> **Key fact:** the `bedrock-mantle` endpoint has only **two** documented
> observability options — **CloudWatch metrics** and **CloudTrail**.
> **Model Invocation Logging is `bedrock-runtime`-only.** There are no
> per-request token/latency logs for `bedrock-mantle`.

---

## 1. Field-test results — what actually works on Mantle

End-to-end tested via `awscurl --service bedrock` against
`https://bedrock-mantle.us-east-1.api.aws/...` from this account. Every
row below was hit live and confirmed.

### Available APIs and which models each accepts

| HTTP path                              | API style                  | Models confirmed working                                                                  |
|---|---|---|
| `POST /v1/chat/completions`            | OpenAI Chat Completions    | `openai.gpt-oss-120b`                                                                     |
| `POST /v1/responses`                   | OpenAI Responses (o-series) | (none of Anthropic; not tested with OpenAI o-series in this account)                      |
| `POST /anthropic/v1/messages`          | Anthropic Messages (native) | `anthropic.claude-opus-4-7`, `anthropic.claude-opus-4-8`, `anthropic.claude-haiku-4-5`     |
| `GET /v1/models`                       | OpenAI-style list          | All Mantle models (returned 41 in field test)                                             |
| `GET /v1/models/{id}`                  | OpenAI-style get           | Per-model metadata                                                                         |

### Wrong-path-for-model error shape

When a request lands on the right host but the wrong API for that model,
Mantle returns a structured 400 (NOT 404):

```json
{"error":{"code":"validation_error",
          "message":"The model 'anthropic.claude-opus-4-7' does not support the '/v1/chat/completions' API",
          "param":null,"type":"invalid_request_error"}}
```

A 404 with empty body means **wrong path** (e.g. `/v1/messages` does not
exist; the Anthropic Messages API lives under `/anthropic/v1/messages`).

### Anthropic on Mantle — verified request body

```bash
awscurl --service bedrock --region us-east-1 -X POST \
  -H "Content-Type: application/json" \
  "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages" \
  -d '{
    "anthropic_version": "bedrock-2023-05-31",
    "model": "anthropic.claude-opus-4-7",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "..."}]
  }'
```

Response body shape (verified):

```json
{
  "model": "claude-opus-4-7",
  "id": "msg_bdrk_...",
  "type": "message",
  "role": "assistant",
  "content": [{"type": "text", "text": "..."}],
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 25,
    "output_tokens": 40,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation": {
      "ephemeral_5m_input_tokens": 0,
      "ephemeral_1h_input_tokens": 0
    },
    "service_tier": "standard"
  }
}
```

The response includes per-request **input/output token counts**, **cache
breakdown**, **service_tier**, and a **request id** for tracing — same
shape as Anthropic's native Messages API. Latency is not in the response
body, but you can measure it client-side as wall-clock around the call.

### OpenAI / OSS-style on Mantle — verified request body

```bash
awscurl --service bedrock --region us-east-1 -X POST \
  -H "Content-Type: application/json" \
  "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions" \
  -d '{
    "model": "openai.gpt-oss-120b",
    "messages": [{"role": "user", "content": "..."}],
    "max_completion_tokens": 50
  }'
```

Response shape (verified, abbreviated):

```json
{
  "choices": [{"message": {"role": "assistant", "content": "..."},
               "finish_reason": "length", "index": 0}],
  "model": "openai.gpt-oss-120b",
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "service_tier": "default",
  "usage": {"completion_tokens": 50, "prompt_tokens": 75, "total_tokens": 125}
}
```

Note `max_completion_tokens` (OpenAI-style) — **not** `max_tokens` (which
is the Anthropic Messages field).

### Authentication

**SigV4 over the `bedrock` service** (NOT `bedrock-runtime`, NOT
`bedrock-mantle`). Verified:

```
--service bedrock --region us-east-1
```

The `awscurl` SigV4 signer with `--service bedrock` produces a valid
signature for the `bedrock-mantle.<region>.api.aws` host.

### CloudWatch metric emission lag

Field-tested: a successful Mantle request shows up in
`AWS/BedrockMantle` metrics within **~60-90 seconds** (consistent with
CW's standard ~1-min metric publish cadence). Don't expect immediate
visibility; one ingest run after the requests will populate dashboards.

### Bonus metric not in the AWS doc

Field test surfaced a metric the public AWS doc does NOT list:
`EquivalentReservationUnits` (Account dimension only). Likely related to
Mantle's capacity-billing model. Not yet wired into the dashboard's
Mantle ingester — TODO.

---

## 2. By telemetry source

| Telemetry source | `bedrock-runtime` | `bedrock-mantle` endpoint |
|---|---|---|
| **CloudWatch metrics** | `AWS/Bedrock` namespace; dims `ModelId`, `ContextWindow` | `AWS/BedrockMantle` namespace; dims `Model`, `Project` |
| **Model Invocation Logging** | ✅ per-request to CloudWatch Logs / S3 (tokens, latency, errorCode, cache, inferenceRegion) | ❌ **not available** for the endpoint itself (per AWS docs). The per-request response body DOES carry token + service_tier + request id, so a thin client wrapper can produce equivalent log lines. |
| **CloudTrail** | `eventSource: bedrock.amazonaws.com` / `bedrock-runtime…` | `eventSource: bedrock-mantle.amazonaws.com`; inference = `CreateInference` **data events** (opt-in, paid) |
| **Cost / CUR** | ✅ by ModelId / inference profile | ✅ by model + **Project** (per-project chargeback) |
| **Service Quotas** | ✅ listed (RPM/TPM per model) | ❌ not in Service Quotas (managed internally; defaults 10M iTPM / 2M oTPM / 100M RPM) |

## 3. By signal (what you can actually measure)

| Signal | `bedrock-runtime` | `bedrock-mantle` endpoint |
|---|---|---|
| Invocation/inference count | ✅ `Invocations` | ✅ `Inferences` |
| Input/output tokens (aggregate) | ✅ `InputTokenCount` / `OutputTokenCount` | ✅ `TotalInputTokens` / `TotalOutputTokens` |
| Per-request token percentiles | ✅ via invocation logs | ✅ `InputTokens` / `OutputTokens` (Project+Model level only) |
| 4xx client errors | ✅ `InvocationClientErrors` | ✅ `InferenceClientErrors` |
| 5xx / server errors | ✅ `InvocationServerErrors` | ❌ **no metric** (CloudTrail records the call, not the outcome split) |
| 429 throttles | ✅ `InvocationThrottles` (⚠️ see note) | ❌ folded into 4xx, no breakout |
| Latency / TTFT | ✅ `InvocationLatency`, `TimeToFirstToken` (+ invocation logs) | ❌ **not published** by CloudWatch. Wall-clock client-side IS available (Anthropic and OpenAI response bodies do not carry server-side timing fields, only token counts). |
| Cache hit/miss tokens | ✅ `CacheReadInputTokenCount` / `CacheWriteInputTokenCount` | ⚠️ Anthropic Messages response body carries `cache_creation_input_tokens` + `cache_read_input_tokens` per-request (verified live), but no aggregate CW metric is published for the Mantle endpoint. |
| Per-request tracing (requestId) | ✅ invocation logs + CloudTrail | ✅ Response body carries `id` (e.g. `msg_bdrk_...` for Anthropic, `chatcmpl-...` for OpenAI). CloudTrail `CreateInference` data events also surface it (audit-grade only — model/stream/service_tier/metadata; **no** tokens, no latency). |
| Per-project attribution | ❌ (no project concept) | ✅ `Project` dimension + CloudTrail Project mgmt events |
| Cross-region (CRIS) | ✅ aggregated | ❌ in-region only (metric emitted in the handling Region) |
| Audit of mgmt actions | ✅ | ✅ CloudTrail mgmt events (ListModels, Projects, fine-tuning) — free, on by default |
| **Service tier observation** | implicit (provisioned-throughput vs on-demand inferred from quotas) | ✅ response body carries `service_tier` field per-request — visible client-side without invocation logging |
| **Bonus: capacity-equivalent units** | n/a | ⚠️ Field test surfaced `EquivalentReservationUnits` metric (Account-level only) — undocumented in the public CW doc. Likely tracks reserved-capacity consumption. Not yet ingested by this dashboard. |

⚠️ **Runtime throttle caveat:** field observations indicate that for
Mantle-*hosted* models invoked via `bedrock-runtime`,
`InvocationThrottles` can read 0 during real throttling. Confirm
empirically before trusting throttle metrics for Mantle-backed models on
the runtime path.

---

## 4. `AWS/BedrockMantle` granularity levels

Pick the dimension set that matches the query:

| Level | Use for | Notes |
|---|---|---|
| **Account** | overall usage, error rate, aggregate token volume; high-level dashboards + account-wide alarms | **Not** for cost analysis (pricing varies by model). `EquivalentReservationUnits` is published at this level. |
| **Project** | per-project rollups for chargeback / team dashboards | uses `Project` dim |
| **Model** | per-model usage and error rates | **best for migrating dashboards built on the runtime `ModelId` dimension** |
| **Project+Model** | cost analysis + **percentile** latency/token analysis | primary level; both project and model attribution on one datum; `InputTokens`/`OutputTokens` per-request data emit here only |

Each level emits independently — one inference contributes to all four
when both project and model resolve.

## 5. Differences from `bedrock-runtime` metrics (verbatim summary)

- **Separate namespace** — `AWS/BedrockMantle`. Dashboards/alarms on
  `AWS/Bedrock` will **not** pick up `bedrock-mantle` traffic.
- **Naming** — `Inferences` (not `Invocations`), `TotalInputTokens` /
  `TotalOutputTokens` (not `InputTokenCount` / `OutputTokenCount`),
  `InferenceClientErrors` (not `InvocationClientErrors`).
- **Project dimension** — present on Mantle, absent on runtime.
- **Cross-region** — Mantle is in-region only; no CRIS aggregation.
- **Latency** — `InvocationLatency` / `TimeToFirstToken` equivalents
  **not yet published** by Mantle.

## 6. Viewing in the console

CloudWatch console → **Metrics** → **All metrics** → **`AWS/BedrockMantle`**
→ choose the dimension set (e.g. `Project, Model`). Requires CloudWatch
read permissions.

Or via the dashboard: every tab now has a `bedrock-runtime` /
`bedrock-mantle` sub-tab switcher with a coverage badge that's honest
about the gaps in §3.

---

## 7. Practical takeaways

- **Per-request latency or 5xx breakdown via CloudWatch alone → only
  `bedrock-runtime` + Model Invocation Logging.** Mantle cannot provide
  these via CW today.
- **Mantle wall-clock latency is observable client-side.** Wrap each
  Mantle SDK call with a stopwatch and log it; you get the same number
  invocation logging would have given you, just in your code instead of
  in S3. This is how the dashboard's Mantle Latency tab populates when
  invocation logs aren't available.
- **Mantle endpoint per-request record = CloudTrail data events**
  (audit-grade: who called what model; **no** tokens/latency). Enable:
  ```bash
  aws cloudtrail put-event-selectors --trail-name <trail> --advanced-event-selectors \
    '[{"Name":"mantle-inference","FieldSelectors":[
        {"Field":"eventCategory","Equals":["Data"]},
        {"Field":"resources.type","Equals":["AWS::BedrockMantle::Project"]}]}]'
  ```
  ⚠️ customer `metadata` is logged verbatim — never put secrets in it.
- **Per-project cost/usage is the one thing Mantle does better** than
  runtime (the `Project` dimension).
- **Migrating an existing agent**: keep `bedrock-runtime` for Converse /
  InvokeModel calls you already have. Add Mantle as a parallel path for
  any new code that wants OpenAI / Anthropic-native APIs. They coexist;
  there is no deprecation of `bedrock-runtime`.

---

## 8. Migration cheat-sheet

If your code currently does this on `bedrock-runtime`:

```python
import boto3
client = boto3.client("bedrock-runtime", region_name="us-east-1")
resp = client.converse(
    modelId="anthropic.claude-opus-4-7",
    messages=[{"role": "user", "content": [{"text": "..."}]}],
)
```

The drop-in equivalent on `bedrock-mantle` for an Anthropic model is:

```python
from anthropic import AnthropicBedrock

client = AnthropicBedrock(
    aws_region="us-east-1",
    base_url="https://bedrock-mantle.us-east-1.api.aws/anthropic",
)
resp = client.messages.create(
    model="anthropic.claude-opus-4-7",
    max_tokens=1024,
    messages=[{"role": "user", "content": "..."}],
)
```

For an OpenAI / OSS / Mistral / Qwen / etc. model, use the OpenAI SDK
pointing at Mantle:

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
    api_key="bedrock-sigv4",  # placeholder; SigV4 signer goes on the HTTP layer
)
```

Both clients need a SigV4 signer wired into their HTTP transport. The
Anthropic `AnthropicBedrock` client handles this natively. For the
OpenAI client, plug in `aws-sigv4-auth` or an httpx auth handler with
`service="bedrock"`, `region="us-east-1"`.

---

## 9. Sources

### Public AWS docs

- Monitor the `bedrock-mantle` endpoint (overview):
  `https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle.html`
- CloudWatch metrics for `bedrock-mantle`:
  `https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle-metrics.html`
- CloudTrail for `bedrock-mantle`:
  `https://docs.aws.amazon.com/bedrock/latest/userguide/logging-cloudtrail-mantle.html`
- `bedrock-runtime` CloudWatch metrics:
  `https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html`

### Field tests (us-east-1)

- `awscurl --service bedrock` against `bedrock-mantle.us-east-1.api.aws`
  on **2026-06-03**. ~10 successful requests across `openai.gpt-oss-120b`,
  `anthropic.claude-opus-4-7`, `anthropic.claude-opus-4-8`, and
  `anthropic.claude-haiku-4-5`. CW metrics confirmed populated within
  ~90s.
