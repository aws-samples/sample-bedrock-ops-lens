# Application inference profiles

Lens resolves application inference profile ARNs and IDs to their underlying
foundation model. The scheduled ingester paginates
`ListInferenceProfiles(typeEquals="APPLICATION")` in each monitored account and
Region. It uses `GetInferenceProfile` for observed profiles missing from the list.

The cache stores the profile ARN, ID, name, destination model ARNs and resolved
model ID. Multiple destination Regions count as **one model**, not multiple
invocations. Deleted profiles retain their last-known mapping. If a profile was
deleted before Lens ever saw it, its original identifier stays unresolved.

## Enable

Deploy this version normally; it applies migration `013` before updating the
API and ingester, and schedules the
`inference_profiles` ingester. Update each monitored account's reader role from
`infra/monitored-account-role.yaml` too: it needs both
`bedrock:ListInferenceProfiles` and `bedrock:GetInferenceProfile`.
The central ingester template already includes both actions.

For local development, run `make schema` on an existing database. New Compose
databases apply all migrations at initialization. Refresh the cache manually with:

```sh
python -m ingestion.inference_profiles --regions us-east-1,us-west-2 --days 14
```

This uses the same account configuration and role assumption as CloudWatch
ingestion. Failed lookups are reported without removing cached mappings or usage.

## What changes in Lens

Model and provider views, invocation-log attribution, proxy telemetry and
lifecycle checks use the resolved model. Original identifiers and counters stay
in `public` tables; the API uses `lens_read` views. Discovering a mapping therefore
resolves existing history without replaying logs or changing deduplication keys.
CloudWatch totals, invocation logs and proxy events remain separate sources.
Unresolved profiles retain their identifier and show an unknown provider in
Model Insights; Lens does not assign them a guessed model cost.
Lens queries the exact `ModelId` CloudWatch series; it does not add
higher-dimensional copies of those metrics.

Coincident hourly counters are summed before finding the busiest hour.
The reported rate is still an **hourly average per minute**, not a measured
minute peak. Latency source buckets remain separate; profile percentiles are
not summed or turned into a fabricated population percentile.

**Quota routing remains unknown for AIP traffic.** List/Get return regional
foundation model ARNs, but not the original `modelSource.copyFrom`. Lens does
not invent a `us.` or `global.` profile ID or score the traffic against an
on-demand quota. Its quota views show an unknown limit for affected traffic.
Existing explicit system-profile IDs retain their normal quota matching.

## Inspect a mapping or application

`GET /api/inference-profiles?accounts=123456789012&region=us-east-1`
returns the cached mappings, including retained profiles. Results paginate with
`limit`, `offset` and `next_offset`. `api_visible=false` means the last complete
refresh did not see the profile; it does not distinguish deletion from lost access.

The profile remains a separate dimension in the reporting views:

```sql
SELECT application_profile_arn, application_profile_name, modelid,
       SUM(total_requests) AS requests
FROM lens_read.f_daily_tagged
WHERE accountid = '123456789012'
  AND region = 'us-east-1'
  AND event_date >= current_date - 7
  AND tag_key = '__all__'
  AND application_profile_arn IS NOT NULL
GROUP BY application_profile_arn, application_profile_name, modelid;
```

This example uses invocation logs; it does not combine those requests with
CloudWatch or proxy totals. The mapping API and SQL dimension are available
without introducing another UI selector.

## Local verification

The profile tests use SDK stubs and a disposable PostgreSQL cluster. The browser
check uses the built UI and real local API, with synthetic data:

```sh
cd frontend && npm ci && npm run build && cd ..
LENS_AIP_BROWSER=1 .venv/bin/python -m pytest tests/test_inference_profiles*.py -q
```

Install PostgreSQL 15+ tools and Playwright Chromium first. This does not invoke
Bedrock or deploy AWS resources.

Sources: [ListInferenceProfiles](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_ListInferenceProfiles.html),
[GetInferenceProfile](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GetInferenceProfile.html),
[InferenceProfileModel](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_InferenceProfileModel.html),
[runtime metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html).
