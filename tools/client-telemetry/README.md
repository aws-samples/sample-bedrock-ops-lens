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
OpenAI. See `litellm_callback.py` (copy-paste, fail-open).

Register BOTH callbacks. A success-only registration means failed and throttled
requests never emit an event, so the error-rate and throttle-rate panels read a
structural zero - exactly the signals you opened the tab for:

**Running the LiteLLM proxy** (`litellm --config config.yaml`) - register the
CustomLogger instance:

```yaml
# litellm config.yaml
litellm_settings:
  callbacks: ["litellm_callback.ops_lens_logger"]
```

**Using the LiteLLM Python SDK** in your own process - register the functions:

```python
import litellm, litellm_callback
litellm.success_callback = [litellm_callback.ops_lens_handler]
litellm.failure_callback = [litellm_callback.ops_lens_failure_handler]
```

Copy `litellm_callback.py` next to your `config.yaml`, or anywhere on the proxy's
`PYTHONPATH`. Three things verified against litellm 1.93.0, each of which silently
breaks the integration if you get it wrong:

- **The module name must match the filename.** The proxy refuses to start
  otherwise: `ImportError: Could not find module file .../ops_lens_callback.py`.
- **The proxy needs `callbacks:`, not `success_callback:`.** A plain function in
  `success_callback` is *registered* ("Initialized Success Callbacks - [<function
  ops_lens_handler>]") and then never invoked for a successful request. Requests
  return 200, nothing is emitted, and only failures reach S3 - so the dashboard
  shows a 100% error rate over zero traffic. `callbacks:` with the CustomLogger
  instance covers successes *and* failures, so there is no second entry to forget.
- **`ops_lens_logger` is an instance, not a class.** A class reference does not
  resolve.

Set `OPS_LENS_DEBUG=1` while you are wiring it up: the handler is fail-open by
design (telemetry must never break inference), and that flag is what turns a
silent handler exception into a printed traceback.
```bash
export OPS_LENS_EVENTS_BUCKET=your-genai-proxy-events
export OPS_LENS_REGION=us-east-1
# optional
export OPS_LENS_FLUSH_EVERY_N=200     # flush after N events
export OPS_LENS_FLUSH_EVERY_S=30      # ...or after N seconds, whichever first
export OPS_LENS_MAX_BUFFERED=20000    # cap while S3 is unreachable
```

Attribution comes from per-request metadata (`workload`, `team`, `user`, …)
or automatically from LiteLLM virtual-key user/team identity
(`user_api_key_user_email`, `user_api_key_team_alias`, and the end-user id).

What it records per request: tokens (input / output / cache-read / cache-write,
kept disjoint across both the OpenAI and Anthropic usage shapes), end-to-end
latency, TTFT from the first streamed chunk, the model's own region, retry count,
LiteLLM's cost estimate, and the real HTTP status. A provider the callback does
not recognise is reported as endpoint `unknown` rather than being counted as
Bedrock traffic.

Delivery: events are buffered and flushed on size, on age (from a background
thread, so a quiet period does not leave the tail unsent), and at shutdown
(atexit + SIGTERM). A failed upload is retried with backoff and re-queued rather
than dropped; the buffer is bounded and any forced drop is reported. Uploads
happen off the request path, so no request waits on S3.

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

Route the collector's output to S3 as in on-ramp 3 - the ingester normalizes
Claude Code's records itself, so no collector transform is needed. The
`claude_code.api_request` event carries model, tokens (incl. cache split),
duration_ms, cost estimate, and retry counts — mapping 1:1 onto the event
shape above. If your org runs **Claude Apps Gateway**, point one of its
telemetry destinations at the same collector: exports arrive already
identity-stamped.

## On-ramp 3 - OTEL collector -> S3 (any gen_ai-instrumented SDK)

For apps using the Anthropic SDK / OpenAI SDK with OpenTelemetry GenAI
instrumentation (`gen_ai.*` attributes), or Claude Code's OTLP output.

**No collector transform is needed.** The ingester normalizes OTLP GenAI records
itself (`ingestion/otel_normalize.py`), so the collector's only job is to write
the records to S3 as NDJSON under the `proxy-events/<region>/` prefix:

```yaml
receivers:
  otlp: { protocols: { http: { endpoint: 0.0.0.0:4318 } } }
processors:
  batch: {}
exporters:
  awss3:
    s3uploader:
      region: us-east-1
      s3_bucket: your-genai-proxy-events
      s3_prefix: proxy-events/us-east-1
      s3_partition_format: '%Y/%m/%d/%H'
    marshaler: otlp_json    # NOT `body` - see the note below
service:
  pipelines:
    logs:   { receivers: [otlp], processors: [batch], exporters: [awss3] }
    # Instrumentation libraries that emit inference SPANS rather than log records
    # need this second pipeline, or their telemetry never leaves the collector.
    traces: { receivers: [otlp], processors: [batch], exporters: [awss3] }
```

> **Use `marshaler: otlp_json`.** `marshaler: body` writes only the log *body* and
> discards every attribute: verified against
> otel/opentelemetry-collector-contrib v0.160.0, a 4,477-byte record arrived in S3
> as the 23-byte string `gen_ai.client.inference`, so nothing could be ingested.
> `otlp_json` writes the full OTLP envelope
> (`resourceLogs -> scopeLogs -> logRecords`), several records per document and no
> trailing newline; the ingester expands that shape and attaches each record's
> resource attributes, so `OTEL_RESOURCE_ATTRIBUTES` (team, cost_center, ...)
> arrives as attribution.

What the normalizer maps (both current and earlier gen_ai spellings are
accepted, since the GenAI conventions are still Development stability):

| OTLP attribute | Event field |
|---|---|
| `gen_ai.request.model` / `gen_ai.response.model` | `model` |
| `gen_ai.provider.name` / `gen_ai.system` | `endpoint` (`aws.bedrock`->runtime, `anthropic`->anthropic-api, `openai`/`azure.ai.openai`->openai-api) |
| `gen_ai.usage.input_tokens` / `output_tokens` | `input_tokens` / `output_tokens` |
| `gen_ai.usage.cache_read_input_tokens` | `cache_read_tokens` |
| `gen_ai.usage.cache_creation_input_tokens` | `cache_write_tokens` |
| `gen_ai.client.operation.duration` (seconds) | `latency_ms` |
| `gen_ai.server.time_to_first_token` (seconds) | `ttft_ms` |
| `http.response.status_code`, `error.type` | `status`, `throttled` |
| `gen_ai.request.retry_count` | `retry_attempts` |
| `cloud.region`, `cloud.account.id` | `region`, `accountId` |
| `gen_ai.response.id`, else `spanId`, else `traceId` | `request_id` (idempotency) |
| span `startTimeUnixNano` / `endTimeUnixNano` | event timestamp and elapsed `latency_ms` |
| span `status.code = STATUS_CODE_ERROR` | `status` 500 (or 429 when the message names throttling) |
| `traceId` / `spanId` | kept as `trace_id` / `span_id` |
| `user.email` / `enduser.id`, `team`, `service.name`, `deployment.environment`, `cost_center` | `dimensions.user` / `.team` / `.workload` / `.env` / `.cost_center` |
| any other non-namespaced attribute (e.g. from `OTEL_RESOURCE_ATTRIBUTES`) | an extra `dimensions` entry |

Notes:

- Resource attributes are read too, which is where `OTEL_RESOURCE_ATTRIBUTES`
  (`team=ml-platform,cost_center=cc-123`) actually lands.
- Records with no model attribute are not inference records (tool spans, start
  events) and are skipped.
- Mantle has no distinguishing provider attribute; stamp it explicitly with an
  `opslens.endpoint=mantle` attribute. Use that namespaced key with the LiteLLM
  proxy too: a bare `metadata.endpoint` is overwritten by the proxy with its own
  URL during request preparation, which used to put a Mantle deployment in the
  direct-OpenAI slice.
- A provider the normalizer does not recognise is recorded as endpoint
  `unknown` rather than being counted as Bedrock runtime traffic. Unknown traffic
  receives no share of the AWS bill and is not scored against AWS quotas.
- Verify a mapping without a collector:

  ```bash
  python -c "import json,sys; from ingestion.otel_normalize import normalize; \
      print(json.dumps(normalize(json.load(sys.stdin)), indent=2))" < one-record.json
  ```

### What an OTEL instrumentation library can and cannot tell you

Verified end to end with the real OpenAI SDK (2.48.0) +
`opentelemetry-instrumentation-openai-v2` (2.4b0) against a local
OpenAI-compatible fixture server, through a real collector with a `traces`
pipeline. Tokens, latency, attribution and error classification all arrive
correctly - **220 input / 35 output tokens and 5 successes + 1 HTTP 400 + 1 HTTP
500 reached the dashboard exactly**. Two things that instrumentation does NOT
report, so plan around them:

- **No cached-token breakdown.** The library emits only
  `gen_ai.usage.input_tokens` / `output_tokens`. A response whose
  `prompt_tokens_details.cached_tokens` was 40 arrives as 120 uncached input, so
  the cached-prompt-token panel stays empty for this on-ramp. The LiteLLM
  callback does report the cache split.
- **SDK-internal retries are invisible.** The span covers the LOGICAL call, so
  two HTTP 429s followed by a success produce ONE event with no throttle. Throttle
  rate from this on-ramp is therefore a floor. The LiteLLM proxy on-ramp reports
  one event per provider attempt and does capture them.
- Error status comes from the span's status MESSAGE (`Error code: 400 - ...`);
  the library's `error.type` attribute is dropped by the OTel SDK because it
  passes a type object rather than a string.

Label results from this path "OpenAI-compatible protocol and OTEL integration
verified" - it does not verify OpenAI's own service authentication, live rate
limits, or every provider response variation.

---

## Coverage honesty

Client telemetry is **self-reported**: only instrumented/proxied traffic
appears, and the direct-API slices have no AWS-side cross-check (no CUR line
items, no CloudWatch, no invocation logs). The dashboard labels these
surfaces "client-reported" and keeps AWS-metered sources as billing/quota
truth. Uninstrumented traffic is invisible — treat coverage gaps as findings,
not as zero usage.
