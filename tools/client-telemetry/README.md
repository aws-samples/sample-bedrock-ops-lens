# Client telemetry on-ramps

The dashboard's per-request event pipeline (the same one behind the Workloads
tab) accepts **client-reported** telemetry from three on-ramps. All three emit
the identical metadata-only NDJSON event into:

```
s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl[.gz]
```

Event shape (see the root README for the full field list):

```json
{"ts":"2026-07-25T18:03:22Z",
 "dimensions":{"user":"jsmith@corp.com","team":"ml-platform","workload":"search"},
 "model":"claude-sonnet-5","endpoint":"anthropic-api","region":"us-east-1",
 "input_tokens":812,"output_tokens":143,"cache_read_tokens":600,
 "status":200,"throttled":false,"latency_ms":940,
 "ttft_ms":310,"retry_attempts":1,"cost_usd_est":0.0125,"request_id":"req_..."}
```

`endpoint` values: `runtime` | `mantle` (Bedrock paths) | `anthropic-api` |
`openai-api` (direct-API paths — visible ONLY through client telemetry).
Aliases accepted: `anthropic`, `openai`, `bedrock`, `azure-openai`, …
`ttft_ms`, `retry_attempts`, `cost_usd_est` are optional. Cost is an
ESTIMATE — the dashboard labels it as such; reconcile with Cost Explorer.

**What never leaves your infrastructure: prompt or response content.** Events
are metadata-only by construction.

---

## On-ramp 1 — LiteLLM proxy callback (all providers, no OTEL needed)

Covers every backend the proxy fronts: Bedrock, direct Anthropic, direct
OpenAI. See `litellm_callback.py` (copy-paste, ~150 lines, fail-open).

```yaml
# litellm config.yaml
litellm_settings:
  callbacks: ["ops_lens_callback.ops_lens_handler"]
```
```bash
export OPS_LENS_EVENTS_BUCKET=your-genai-proxy-events
export OPS_LENS_REGION=us-east-1
```

Attribution comes from per-request metadata (`workload`, `team`, `user`, …)
or automatically from LiteLLM virtual-key user/team identity.

## On-ramp 2 — Claude Code native telemetry (developer fleets)

Claude Code emits OTLP metrics/events with `user.email` on every record —
no code changes, env vars only (admins can force-enable via managed settings):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<your-collector>:4318
# optional org attributes stamped on all telemetry:
export OTEL_RESOURCE_ATTRIBUTES="team=ml-platform,cost_center=cc-123"
```

Route the collector's output to S3 with the mapping in on-ramp 3. The
`claude_code.api_request` event carries model, tokens (incl. cache split),
duration_ms, cost estimate, and retry counts — mapping 1:1 onto the event
shape above. If your org runs **Claude Apps Gateway**, point one of its
telemetry destinations at the same collector: exports arrive already
identity-stamped.

## On-ramp 3 — OTEL collector → S3 (any gen_ai-instrumented SDK)

For apps using the Anthropic SDK / OpenAI SDK with OpenTelemetry GenAI
instrumentation (`gen_ai.client.*` metrics, Development stability — the
ingester is schema-tolerant by design). Collector sketch:

```yaml
receivers:
  otlp: { protocols: { http: { endpoint: 0.0.0.0:4318 } } }
processors:
  batch: {}
  transform/opslens:
    # map gen_ai.* / claude_code.api_request attrs onto the event fields:
    #   gen_ai.request.model        -> model
    #   gen_ai.provider.name        -> endpoint (anthropic->anthropic-api, ...)
    #   gen_ai.usage.input_tokens   -> input_tokens
    #   gen_ai.usage.output_tokens  -> output_tokens
    #   duration / time_to_first_chunk -> latency_ms / ttft_ms
    #   user.email, team, ...       -> dimensions
exporters:
  awss3:
    s3uploader:
      region: us-east-1
      s3_bucket: your-genai-proxy-events
      s3_prefix: proxy-events/us-east-1
      s3_partition_format: '%Y/%m/%d/%H'
    marshaler: body  # NDJSON lines
service:
  pipelines:
    logs: { receivers: [otlp], processors: [batch, transform/opslens], exporters: [awss3] }
```

---

## Coverage honesty

Client telemetry is **self-reported**: only instrumented/proxied traffic
appears, and the direct-API slices have no AWS-side cross-check (no CUR line
items, no CloudWatch, no invocation logs). The dashboard labels these
surfaces "client-reported" and keeps AWS-metered sources as billing/quota
truth. Uninstrumented traffic is invisible — treat coverage gaps as findings,
not as zero usage.
