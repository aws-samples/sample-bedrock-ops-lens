# Reference GenAI proxy → Bedrock Ops Lens Workloads

`bedrock_proxy.py` is a minimal, **real** example of the "GenAI proxy" pattern
that feeds the dashboard's **Workloads** tab. It shows exactly what a customer's
proxy needs to emit for per-workload attribution — no dashboard-side changes
required.

## What it does (all real, nothing synthetic)

1. Makes **real** Amazon Bedrock calls (`bedrock-runtime` InvokeModel via
   inference profiles), reading the **actual** `usage` token counts, latency,
   and HTTP status from each response.
2. Tags each call with a `workload` (the per-use-case attribution key).
3. Writes **one metadata-only NDJSON event per request** to S3 — never prompt
   or response text — under the layout the ingester reads:

   ```
   s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl[.gz]
   ```

## Event schema (one JSON object per line)

```json
{
  "ts": "2026-07-04T05:05:29Z",
  "dimensions": {                   // arbitrary custom-attribute map
    "workload": "chat-assistant",
    "env": "prod",
    "business_unit": "consumer"
  },
  "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "endpoint": "runtime",            // or "mantle"
  "region": "us-west-2",
  "input_tokens": 20,
  "output_tokens": 27,
  "cache_read_tokens": 0,           // optional
  "status": 200,
  "throttled": false,
  "latency_ms": 1324.5,             // optional
  "request_id": "..."               // real Bedrock request id (for idempotency)
}
```

`dimensions` is any set of key/value attributes you want to slice by — the
dashboard pivots the Workloads tab by any key you emit (workload, env,
business_unit, cost_center, team, …) and computes tokens, throttle rate,
latency, and per-value TPM quota utilization for each. A bare top-level
`"workload": "x"` is still accepted (folded into `dimensions`) for back-compat.

## Run it

```bash
python bedrock_proxy.py \
  --bucket <your-proxy-events-bucket> \
  --region us-west-2 \
  --calls 20
```

Then trigger the ingester (or wait for its schedule) — the events flow through
`proxy_events.py` → `f_proxy_dim_hourly` / `dim_proxy_dimensions` →
`/api/workload-usage` → the Workloads tab.

## Wiring the dashboard to your bucket

Set `PROXY_EVENTS_BUCKET` (and optionally `PROXY_EVENTS_REGIONS`) when deploying
— see `deploy.sh` / `infra/cloudformation.yaml`. The dashboard reads the bucket
cross-account with a read-only grant; if unset, the Workloads tab simply shows
an empty state (no synthetic data is ever injected).

## Production notes

- A real proxy sits in the request path and emits **one event per user
  request**, not a batch driver like this example.
- Emit `endpoint: "mantle"` for calls made through the `bedrock-mantle`
  OpenAI-compatible endpoint so the Workloads view attributes them correctly.
- Keep events **metadata-only** — the dashboard never needs prompt/response
  content, and the bucket policy should forbid it.
