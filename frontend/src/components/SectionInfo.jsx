// Section info — what each container shows + why it matters + what to do.
// Drives the right-side HelpPanel when a user clicks "Info" on a container.
//
// All references are public-AWS only:
//   AWS Service Quotas console — for quota increase requests
//   AWS Health Dashboard       — for service health
//   docs.aws.amazon.com / aws.amazon.com — for documentation
//   Public Bedrock model IDs   — no internal codenames
//   Platform-engineer audience — no internal-team or partner framing

import { Box, HelpPanel, Link, SpaceBetween } from '@cloudscape-design/components';

export const SECTION_INFO = {
  /* --------------------------------------------------------------------- */
  /* Overview tab                                                          */
  /* --------------------------------------------------------------------- */
  'daily-trend': {
    title: 'Request volume',
    body: 'Daily request count across every model, region, account, and operation in the current filter. Use the "Group by" dropdown to stack bars by Model, Provider, Traffic type, or Region (top 7 + Other).',
    why: 'Volume tells you platform busyness. Stacking shows what is driving it — one model on one region, or a fleet-wide ramp.',
    action: 'Sudden new category appearing → a new application onboarded.\nSteady growth in one category → anticipate capacity for that model/region.\nGroup by Provider to gauge Anthropic vs Amazon vs others share.',
  },
  'health-indicators': {
    title: 'Health indicators',
    body: 'Success rate % (computed `successful/total*100`) and Throttled % (`throttled/total*100`) over time. Both lines on the same y-axis (percentage).',
    why: 'Success dropping while volume stays flat means errors are climbing. Throttle rising while volume stays flat means you are hitting quota walls.',
    action: 'Success < 95% → open the Errors tab.\nThrottle > 1% → file a quota increase via Service Quotas + verify CRIS is enabled on Claude models.\nBoth steady while volume grows → healthy expansion.',
  },
  'top-models-requests': {
    title: 'Top models by requests',
    body: 'Models ranked by total API call volume in the window.',
    why: 'Embedding models often dominate by call count (short, fast calls). This shows operational load on the platform, not compute consumption.',
    action: 'Watch for sudden model adoption shifts. New models entering the top 7 may need capacity attention.',
  },
  'top-models-tokens': {
    title: 'Top models by tokens',
    body: 'Models ranked by total token consumption (input + output).',
    why: 'LLMs (Claude, Nova Pro) dominate by token volume despite fewer calls. This is the actual compute and cost driver.',
    action: 'Token-heavy models drive cost. Monitor growth here to anticipate budget impact and quota needs.',
  },
  'model-category': {
    title: 'Model category',
    body: 'Pie split between LLM (text/chat), Embedding/Rerank, and Image/Video models. Bucketed client-side from the model ID.',
    why: 'Embedding models look big by call count but consume minimal compute. LLMs dominate by token volume and cost. Knowing the split helps prioritize where to invest in capacity planning.',
    action: 'Capacity and quota planning should focus on LLM models. Embedding throttling is far less impactful per-request.',
  },
  'traffic-types': {
    title: 'Traffic types',
    body: 'Distribution across the four Bedrock traffic types: ON_DEMAND_INFERENCE_REQUEST, CROSS_REGION_OD_INFERENCE_REQUEST, SOURCE_REGION_OD_INFERENCE_REQUEST, and PROVISIONED_THROUGHPUT_V1. CRIS variants are folded together and split into "Global CRIS" vs "Regional CRIS" by inference_profile_prefix.',
    why: 'Cross-Region Inference (CRIS) gives ~2x quota and multi-region resilience at no additional cost. High single-region on-demand share is leaving free capacity on the table.',
    action: 'For Claude models on heavy on-demand, prefix the model ID with `us.` / `eu.` / `global.` to migrate to CRIS. One-line client change, no pricing change.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html',
  },
  'internal-external': {
    title: 'By account',
    body: 'Top accounts by request volume. The customer build does not have an Internal/External account-type concept the way the internal version did — accounts are simply listed with their usage.',
    why: 'Concentration risk: a single account driving most of the fleet load is a single point of failure if that account hits a quota or has an outage.',
    action: 'For the dominant account(s), confirm CRIS is enabled and quotas are sized for ~2x peak.',
  },
  'operations': {
    title: 'Operations',
    body: 'Distribution across Bedrock-runtime API operations: InvokeModel, Converse, InvokeModelWithResponseStream, ConverseStream.',
    why: 'Streaming operations (ConverseStream, InvokeModelWithResponseStream) indicate real-time UX. Non-streaming indicates batch or backend workloads.',
    action: 'Streaming-heavy fleets are more latency-sensitive. Prioritize prompt caching and CRIS for those workloads.',
  },
  'regions-health': {
    title: 'Regions — volume & capacity pressure',
    body: 'Per-region request volume, error rate, throttle rate, and 5xx counts. Always shows all regions for cross-region comparison; the top-bar Region filter does not apply to this panel by design.',
    why: 'Identifies regions where the platform is under stress. High throttle in one region but not others = regional capacity gap. High 5xx in one region = check the AWS Health Dashboard.',
    action: 'High throttle region → request a quota increase for the affected (model, region) via Service Quotas, and enable CRIS to spill across regions automatically.\nHigh 5xx region → check AWS Health Dashboard before responding to user reports.',
    docLink: 'https://health.aws.amazon.com/health/home',
  },

  /* --------------------------------------------------------------------- */
  /* Spend                                                                  */
  /* --------------------------------------------------------------------- */
  'spend-by-model': {
    title: 'Spend by model',
    body: 'Daily Bedrock spend in dollars. Sourced from AWS Cost Explorer (`ce:GetCostAndUsage`) at daily granularity, grouped by linked-account × service. Cost Explorer data lags 24-48h. When Cost Explorer returns a single consolidated "Amazon Bedrock" line item (most non-EDP customers), the per-model breakdown is derived by allocating the daily total in proportion to each model\'s token volume from CloudWatch — that case is disclosed in the chart description.',
    why: 'Spend is the truth metric — request and token counts are useful but cost is what gets reviewed. Pairing daily spend with token mix lets you spot the small handful of expensive workloads driving most of the bill.',
    action: 'High-cost models with low cache hit rate (see Capacity & Adoption tab) are the strongest cost-reduction candidates. For Claude families, prompt caching cuts ~90% of cached-portion cost; CRIS doesn\'t change cost but unblocks throttle headroom.',
    docLink: 'https://aws.amazon.com/bedrock/pricing/',
  },

  /* --------------------------------------------------------------------- */
  /* Engagement Signals (= Ops Insights) tab                              */
  /* --------------------------------------------------------------------- */
  'cris-adoption': {
    title: 'CRIS vs On-Demand',
    body: 'Per-model split between Cross-Region Inference traffic and single-region on-demand traffic. CRIS is identified by traffic_type IN (CROSS_REGION_OD_INFERENCE_REQUEST, SOURCE_REGION_OD_INFERENCE_REQUEST).',
    why: 'CRIS gives ~2x quota headroom and multi-region failover at no additional cost. On-demand-only workloads are first to throttle when traffic spikes.',
    action: 'Models with high on-demand share and an available CRIS variant → migrate by prefixing the model ID with `us.` / `eu.` / `global.`. Single-line client change, no pricing change.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html',
  },
  'cris-gaps': {
    title: 'CRIS adoption gaps',
    body: 'Accounts with > 10K on-demand requests on a Claude model with zero CRIS adoption. These are the high-volume workloads currently confined to single-region quotas.',
    why: 'These accounts are most likely to hit throttling first since they lack the ~2x quota headroom CRIS provides.',
    action: 'For each row, switch the client model ID to the corresponding CRIS profile (`us.` / `eu.` / `global.` prefix). Single-line code change, no pricing change, immediate quota headroom.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html',
  },
  'inference-profile': {
    title: 'Inference profile adoption',
    body: 'Distribution of requests across the inference-profile prefixes (`us`, `eu`, `global`, `apac`, `au`, `jp`, `ca`). The `global` prefix routes across all regions and has the highest aggregate capacity.',
    why: 'More actionable than the binary CRIS/non-CRIS split — shows which geographic profile users actually picked. Single-region profiles for cross-region workloads leave headroom on the table.',
    action: 'For workloads using a single regional profile (e.g., only `us`), consider switching to `global` for higher aggregate capacity. EU/APAC adoption is a leading indicator of international growth.',
  },
  'service-tier': {
    title: 'Service tier distribution',
    body: 'Requests, accounts, and throttle rate broken down by `service_tier` (default / flex / priority).',
    why: 'Higher tiers cost more but throttle less. The split tells you whether you are paying for tiers you do not need (priority workloads with consistently low throttle = over-paid headroom) or under-paying for workloads that need stability (default-tier high-throttle = should likely move).',
    action: 'priority/reserved tiers with non-trivial throttle → escalate or increase quota. flex tier with high throttle → expected (best-effort tier) — do not file a quota request for flex traffic.',
  },
  'cache-trend': {
    title: 'Cache hit rate trend',
    body: 'Daily fleet-wide ratio of cache_read_input_tokens / total_input_tokens. Tracks adoption growth over time.',
    why: 'Cache hits skip reprocessing, lowering both TTFT and cost. A flat line indicates caching is not yet being used — there is adoption upside.',
    action: 'Flat line → prioritize prompt-caching outreach. Spike → find the workload that drove it and use it as an internal case study.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'region-matrix': {
    title: 'Region × Model matrix',
    body: 'Top models broken out by region. Cells show request counts; an empty cell means the model has no traffic in that region.',
    why: 'Reveals where capacity pressure will hit next. A model with explosive growth in us-east-1 will throttle there long before less-used regions.',
    action: 'Before recommending a region migration, confirm the target region has the model available (cell is non-empty). Not every Bedrock model is in every region.',
    docLink: 'https://docs.aws.amazon.com/general/latest/gr/bedrock.html',
  },
  'throttle-rate-account': {
    title: 'Throttle rate by account',
    body: 'Accounts with non-zero throttling, ranked by throttle percentage = status_429_count / total_requests. Bedrock guidance: > 5% = ship-stopper (act today), 1-5% = high priority (act this week), < 1% = monitor.',
    why: 'These rows are precisely where your fleet is hitting quota walls right now. Each row is a candidate for either CRIS migration, max_tokens tuning (Claude 4+), or a Service Quotas increase.',
    action: 'Per row in priority order:\n1. Verify CRIS is enabled (model ID has `us.` / `eu.` / `global.` prefix).\n2. For Claude 4+, check max_tokens vs actual avg output (Request shape table).\n3. File a Service Quotas increase request to ~2x observed peak.',
    docLink: 'https://console.aws.amazon.com/servicequotas/',
  },
  'request-shape': {
    title: 'Request shape by model',
    body: 'Per-model average input tokens, average output tokens, and the in:out ratio. Typical workloads are around 10:1 (input-heavier than output).',
    why: 'Outliers signal specific optimization plays. Input-heavy (ratio > 50:1) means the workload is paying to re-process the same context every call — prompt caching cuts that ~90% on the cached portion. Output-heavy (ratio < 2:1) on Claude 4+ amplifies burndown — max_tokens tuning becomes critical.',
    action: 'Input-heavy → enable prompt caching for stable system prompts and shared context. Output-heavy on Claude 4+ → tune max_tokens close to actual output length, consider a smaller model (Haiku) if quality tolerates it.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'avg-rpm-tpm': {
    title: 'Avg RPM & TPM',
    body: 'Estimated average requests-per-minute and tokens-per-minute over the window: total / (days × 24 × 60). For fast-moving workloads also use the Peak Hours tab to find the actual peak.',
    why: 'Helps estimate quota utilization without needing to call the Service Quotas API for every (account, model) pair. Compare avg RPM/TPM against your applied RPM/TPM quotas to identify accounts approaching limits.',
    action: 'Accounts with avg RPM/TPM > 50% of applied quota → file a Service Quotas increase before peak hits. Use the "Download CSV" button for offline analysis.',
    docLink: 'https://console.aws.amazon.com/servicequotas/',
  },
  'burndown': {
    title: 'Claude 4+ burndown risk',
    body: 'For Claude 4+ family models: each output token counts 5× toward TPM (instead of 1×), and Bedrock reserves max_tokens × 5 × RPM up-front when each request lands. The "Effective TPM (5×)" column shows the reserved budget; the Overhead % shows how much beyond observed peak it represents.',
    why: 'A workload with max_tokens set to the model maximum but actual avg output of, say, 300 tokens, still has the full max_tokens × 5 × RPM reserved. Throttling can hit with 80%+ unused real capacity. This is the highest-impact, zero-cost fix for Claude 4+ throttling.',
    action: 'Set max_tokens close to actual expected output (not the model maximum). One-line code change. Throttling drops without any quota increase. Re-run this report a week later to confirm.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html',
  },
  'caching': {
    title: 'Prompt caching adoption',
    body: 'Per model: total input tokens, cache-read tokens (served from cache), cache-write tokens (newly cached), and hit rate = cache_read / total_input × 100.',
    why: 'For supported models, cached input tokens are roughly 90% cheaper than uncached and reduce TTFT by up to 85%. For workloads with stable system prompts (chatbots, agents, RAG), enabling caching is the single highest-impact, lowest-effort cost lever.',
    action: 'Models with > 1M daily input tokens and < 10% hit rate → enable caching on the long stable parts of your prompts (system instructions, tool definitions, retrieved context above the cache breakpoint).',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'context-routing': {
    title: 'Context length routing',
    body: 'Which context-length variants (18k / 51k / 200k / 256k / 1024k) requests are being routed to. Larger context variants have lower concurrency and higher latency.',
    why: 'If requests are routing to 200k+ variants when input + output is well below that threshold, performance degrades unnecessarily. Tightening max_tokens keeps requests on the smaller, faster variants.',
    action: 'Set max_tokens appropriately so requests route to the smallest variant that fits your output. Confirm input + output is genuinely > 18k before allowing 200k+ routing.',
  },

  /* --------------------------------------------------------------------- */
  /* Latency tab                                                           */
  /* --------------------------------------------------------------------- */
  'latency-chart': {
    title: 'Latency by model',
    body: 'p50/p90/p99 latency percentiles per model. Toggle between End-to-End (full request duration) and Time-to-First-Token (streaming UX).',
    why: 'TTFT is critical for streaming UX (chatbots, agents). p99 shows the worst-case user experience. Compare against published model baselines for sanity check.',
    action: 'p99 ≫ baseline → enable prompt caching (cuts TTFT ~85% on cached portions) and verify CRIS is on (multi-region spill reduces queueing). p99/p50 ratio > 5× → check Context length routing; the workload may be routing to larger variants than necessary.',
  },
  'latency-table': {
    title: 'Latency table',
    body: 'Detailed latency percentiles for all models with sample counts. Sortable and searchable.',
    why: 'Provides the baseline reference for any latency investigation. Compare reported latency from a specific application against the fleet p50/p90/p99 for the same model.',
    action: 'If a workload reports high latency and matches fleet p99, the latency is expected for that model — the workload should look at request shape, caching, or model selection. If higher than fleet p99, investigate context-length routing.',
  },
  'op-latency': {
    title: 'Latency by operation',
    body: 'p50/p90/p99 latency for InvokeModel, Converse, and their streaming variants.',
    why: 'Streaming operations have meaningful TTFT (vs near-zero for non-streaming, where TTFT == E2E). Lets you compare apples-to-apples within streaming or within non-streaming workloads.',
    action: 'Streaming-latency complaint → check if the workload\'s TTFT p90 matches the fleet TTFT p90. Big delta = investigate the integration. Small delta = it\'s the model.',
  },
  'cris-latency': {
    title: 'CRIS vs On-Demand latency',
    body: 'p50/p90/p99 latency for each model split by traffic_type (CRIS, On-Demand, Provisioned).',
    why: 'Quantifies the actual latency impact of CRIS for each model. CRIS routes across regions to find capacity, which can reduce queueing but adds inter-region hops.',
    action: 'Use this to make CRIS-migration decisions data-driven: if On-Demand p99 ≫ CRIS p99 for the same model, the migration is a clear win. If close, CRIS is still worthwhile for the quota headroom.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html',
  },

  /* --------------------------------------------------------------------- */
  /* Peak hours tab                                                        */
  /* --------------------------------------------------------------------- */
  'peak-hours': {
    title: 'Requests by hour of day',
    body: 'Each bar is the SUM of requests for that hour-of-day across every day in the selected window. Hours are in your browser local timezone (auto-detected).',
    why: 'Bedrock quota burndown happens at PEAK, not on average. Sizing your applied quota against the daily-average rate gets you throttled at the daily peak. Identifying the peak window also lets you time pre-emptive quota requests — they take days to be approved, so file before the next peak.',
    action: 'Find the peak-hour total, divide by 60 → that is your peak RPM. Compare to the model\'s applied RPM quota. Within 80% of quota → file a Service Quotas increase. Narrow peak window → consider request queuing or off-peak batch scheduling to flatten the curve.',
  },

  /* --------------------------------------------------------------------- */
  /* Errors tab                                                            */
  /* --------------------------------------------------------------------- */
  'error-trend': {
    title: 'Error trend (by status code)',
    body: 'Stacked daily breakdown of failed invocations split by HTTP status: 400 (ValidationException), 403 (AccessDeniedException), 429 (ThrottlingException), 500 (InternalServerException), 503 (ServiceUnavailableException). Click any bar to drill into hourly breakdown for that day (last 7 days only).',
    why: '429 = quota exceeded. 400 = bad request from your client. 403 = IAM or model-access denial. 500/503 = Bedrock-side issue. Different codes have different owners and different fix paths.',
    action: '429 sustained → request a Service Quotas increase TODAY. 5xx sustained → check the AWS Health Dashboard. 400 spike correlated with a deploy → roll back. 403 → audit recent IAM policy or Bedrock model-access changes.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/APIReference/CommonErrors.html',
  },
  'errors-by-model': {
    title: 'Errors by model',
    body: 'Per-model failure breakdown by status code. Each row is one model with its absolute counts in the 400/403/429/500/503 columns.',
    why: 'A single misbehaving model usually dominates the error count, so isolating it scopes the investigation. Different status codes need different owners — 429 is platform/quota, 400 is application/client, 5xx is AWS.',
    action: 'Pick the model with the highest 429 → drill into Throttle hotspots filtered to that model. Pick the model with the highest 400 → engage the application owner; the issue is in their request payload.',
  },

  /* --------------------------------------------------------------------- */
  /* Ops Review tab                                                        */
  /* --------------------------------------------------------------------- */
  'ops-exec-summary': {
    title: 'Executive summary',
    body: 'A narrative analysis of your fleet\'s last-N-days Bedrock posture, generated by Claude Opus from the structured findings on this page. The model is grounded only in the metrics shown — it cannot see raw data and never invents numbers. Includes a traffic-flow Mermaid diagram when there is enough data.',
    why: 'Two audiences, same need. (1) The platform engineer running the dashboard needs the "so what" — which one or two issues are biggest right now and what to act on. Reading five tables and synthesizing it under time pressure is error-prone. (2) Internal stakeholders reviewing the report (a budget review, a quarterly platform-health snapshot, an SRE handoff) need the same conclusion-first read without having to ask the engineer to summarize. This section does the synthesis up front so the conversation starts with the conclusion, not the raw data.',
    action: 'Use as the opening of your team email, internal review slide, or operations handoff note. Always cross-check the cited numbers against the structured tables below before sending — Claude is grounded but you sign your name to the report. Click Regenerate if you change the date range or filter.',
  },
  'ops-kpi-ribbon': {
    title: 'KPI ribbon',
    body: 'Five at-a-glance counters: lifecycle alerts, throttled hotspots, growth signals, burndown risks, request shape outliers. Numbers reflect the structured findings derived from your fleet usage in the selected window.',
    why: 'Lets you scan the report in 2 seconds before reading the narrative. Any non-zero red counter (critical) means something is actively breaking; any non-zero amber (warning) means action is required this week.',
    action: 'Two reds → act today. One red plus several ambers → file Service Quotas request and notify the application owners. All zeros → no urgent intervention; this is optimization territory.',
  },
  'ops-lifecycle': {
    title: 'Model lifecycle alerts',
    body: 'Models in your fleet that have entered LEGACY status, are inside the post-EOL extended-access window, or are already past EOL. Lifecycle dates come from the public AWS Bedrock model-lifecycle page and are refreshed periodically.',
    why: 'Once a model passes its EOL date, AWS reserves the right to stop accepting requests at any time. A workload relying on an EOL model in production is one announcement away from a P0. Legacy models also typically lag in throughput, latency, and pricing relative to current versions, so migration usually improves the bill at the same time.',
    action: 'CRITICAL (past EOL) → migrate this week. WARNING (legacy, EOL approaching) → plan a migration for the next sprint. INFO (legacy date in 90 days) → flag proactively so the team is not surprised. The horizontal timeline above the table makes the urgency visual.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html',
  },
  'ops-capacity-health': {
    title: 'Capacity health',
    body: 'Per-(account, model, region) throttle rate and observed peak TPM/RPM in the window. Peak is computed as max-over-hourly-buckets, so it reflects actual sustained-1-hour usage rather than a noisy single-second spike.',
    why: 'Throttle rate directly translates to failed user-facing requests. Bedrock guidance: > 5% = ship-stopper (act today), 1-5% = action this week, < 1% = monitor. Peak TPM/RPM tell you what to ask for in the Service Quotas request — request 2× peak as headroom.',
    action: 'CRITICAL rows → file a Service Quotas increase today with peak_tpm × 2 and peak_rpm × 2 as the requested values. Verify CRIS is enabled (model ID prefix `us.` / `eu.` / `global.`) — that doubles the quota for free. For Claude 4+ models, check max_tokens (see Burndown info) before assuming quota is the bottleneck.',
    docLink: 'https://console.aws.amazon.com/servicequotas/',
  },
  'ops-growth': {
    title: 'Growth signal',
    body: 'Compares recent token-per-day average against the prior window in the same period. HIGH GROWTH = +50% or more, GROWING = +20% to +50%, DECLINING = -30% or worse. Lower-volume accounts (under 1M tokens/day either side) are filtered out so the list stays signal.',
    why: 'Quota requests have lead time. Catching a +50% growth NOW means the quota increase is in place before the workload hits the wall, instead of after the user-visible incident is over. Declining workloads are an early signal of a competitive loss or production issue worth investigating.',
    action: 'HIGH GROWTH rows → collect a quarterly forecast from the application owner and pre-file Service Quotas requests for 3-6 months out. DECLINING rows → ask the owner why (migration to another model? cost optimization? production incident?).',
  },
  'ops-burndown': {
    title: 'Claude 4+ burndown risk',
    body: 'Starting with Claude 4, Bedrock counts each output token as 5 against TPM (instead of 1). Bedrock also reserves max_tokens × 5 × RPM worth of TPM at request time, only adjusting after the request completes. This table shows the effective peak TPM (peak_tpm + 4 × peak_output_tpm) and the burndown overhead %.',
    why: 'If max_tokens is set to the model maximum (4k or 8k) but actual avg output is 300 tokens, Bedrock still reserves the full budget. The workload hits ThrottlingException with 80%+ unused real capacity. Highest-impact, zero-cost fix for Claude 4+ throttling.',
    action: 'Set max_tokens close to actual expected output (check the Avg output column for the right ballpark — typically 200-1000), not the model maximum. One-line client change. Throttling drops without any quota increase.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html',
  },
  'ops-request-shape': {
    title: 'Request shape outliers',
    body: 'Typical Bedrock workloads have an input:output token ratio around 10:1. This table flags accounts/models outside that band. Input-heavy (ratio > 50:1) means long context or system prompts; output-heavy (ratio < 2:1) means generation workloads.',
    why: 'Outliers signal specific optimization plays. Input-heavy = paying to re-process the same context every call → prompt caching cuts that ~90% on the cached portion and reduces TTFT ~85%. Output-heavy on Claude 4+ amplifies burndown (see Burndown) so max_tokens tuning becomes critical.',
    action: 'Input-heavy → enable prompt caching for stable system prompts and shared context. Output-heavy on Claude 4+ → tune max_tokens, consider a smaller model (Haiku) if quality tolerates it.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'ops-engagement': {
    title: 'Engagement opportunities',
    body: 'Two cheap wins: (1) CRIS gap = accounts using on-demand for a Claude model that has a CRIS variant available, with > 10K OD requests and zero CRIS adoption. (2) Caching gap = high-volume Claude models (> 100M input tokens) with under 5% cache hit rate.',
    why: 'CRIS migration: ~2x quota at zero additional cost, single-line code change. Multi-region resilience as a bonus. Prompt caching: ~90% cost reduction on cached portions and ~85% TTFT reduction. Both are reversible, both are quick to ship, both materially improve user experience.',
    action: 'CRIS gap → switch model ID prefix to `us.` / `eu.` / `global.` for the affected models. Caching gap → enable caching breakpoints on stable system prompts. Track adoption in the next ops review.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  /* --------------------------------------------------------------------- */
  /* Model Lifecycle tab                                                   */
  /* --------------------------------------------------------------------- */
  /* --------------------------------------------------------------------- */
  /* Model Insights tab                                                    */
  /* --------------------------------------------------------------------- */
  'insights-provider-pie': {
    title: 'Requests by provider',
    body: 'Aggregate request counts grouped by model provider (Anthropic, Amazon, Meta, …) over the selected window.',
    why: 'Provider concentration is a real operational risk. If 90% of fleet traffic goes to one provider, a regional outage or quota change in that provider lands directly on your error rate. A diversified mix is a hedge.',
    action: 'Heavy single-provider dependency → evaluate whether a second provider can serve the same use case as a fallback. Bedrock makes provider switching a one-line modelId change.',
  },
  'insights-cost-pie': {
    title: 'Cost share by model (estimate)',
    body: 'Top 8 models by approximate spend, computed from token volumes × an in-code provider price table. Rough — for relative proportions only.',
    why: 'Numbers in the Cost tab are real (Cost Explorer); numbers here are an instantaneous "where is my budget going right now" snapshot driven by usage telemetry. Useful between Cost Explorer refreshes (which lag 24-48 hours).',
    action: 'Compare against the Cost tab\'s Cost Explorer numbers to validate. Big gaps usually mean a model has heavy prompt caching that the price-table approximation doesn\'t account for.',
  },
  'quota-drilldown': {
    title: 'Quota drill-down',
    body: 'Pick one (account · model · region) combination and see its TPM and RPM utilization vs the applied Service Quotas limit, hour-by-hour over the last 14 days. The red line is the limit. Headline KPIs at the top of each chart: Limit, Peak (with timestamp), Average, and Util % (peak as a fraction of limit).',
    why: 'When an oncall gets paged about throttling, the first question is "is the workload near its quota?". Fleet-wide views average that signal away. Drilling to a single (account, model, region) makes it a yes/no answer. If util is ≥100%, the customer needs a quota increase or to spread traffic across CRIS. If util is low and they\'re still throttled, the cause is somewhere else (regional outage, key-level throttling, etc).',
    action: 'Util ≥100% → request a quota increase, or migrate the workload to CRIS for higher headroom. Util 70-99% → growth runway is gone, plan for an increase. Util <70% but errors are high → look at the Errors tab and Latency tab; throttling is not the cause.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html',
  },
  'quota-drilldown-tpm': {
    title: 'Tokens per minute (TPM)',
    body: 'Hourly peak tokens-per-minute for the selected (account · model · region) over the last 14 days, plotted against the applied AWS Service Quotas TPM limit (the red line). TPM is the input + output token sum, divided by 60. When the data range is much smaller than the limit, the y-axis switches to logarithmic so both fit on the same chart.',
    why: 'TPM is the metric AWS actually rate-limits Bedrock on for most providers. Throttle errors almost always trace back to bursts pushing TPM above its quota, even when the request rate looks reasonable. Plotting peak TPM directly against the published quota line makes it obvious whether a workload is near its ceiling.',
    action: 'Util ≥100% → throttling is happening, request a quota increase or shift load to CRIS. Util 70-99% → no growth runway; plan an increase. Util <30% with throttle errors → the cap is somewhere else (per-key throttling, regional outage); check the Errors tab.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html',
  },
  'quota-drilldown-rpm': {
    title: 'Requests per minute (RPM)',
    body: 'Hourly peak requests-per-minute for the selected (account · model · region). When AWS publishes an RPM quota for the model, the red line is the applied limit. When AWS does NOT publish an RPM quota (true for some Anthropic SKUs including Claude Opus 4.7), the red line is a derived effective ceiling: the TPM limit ÷ the average tokens-per-request observed in this window. That number is the rate at which TPM would cap you, which is the real ceiling even when no nominal RPM exists.',
    why: 'The colleague-reference dashboard shipped with both TPM and RPM views because oncalls reach for whichever is more familiar. RPM is also the right metric for low-token, high-request workloads (chatbots, classifiers) where TPM is rarely the binding constraint. Showing the derived ceiling — instead of an empty card — keeps the drill-down useful even on models where AWS\'s quota catalogue has gaps.',
    action: 'Published RPM limit + util ≥100% → quota increase. Derived ceiling + util ≥80% → look at TPM in parallel, you are about to hit the underlying TPM cap. Util <30% but throttling shows in Errors → cap is elsewhere; investigate per-key or per-account throttling, or regional Bedrock issues.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html',
  },
  'insights-cards': {
    title: 'Top models',
    body: 'The 12 highest-volume models in your fleet for the selected window. Each card shows requests, tokens, average request shape, cache hit %, error rate, and unique accounts using the model.',
    why: 'Per-model fingerprint at a glance: high I/O ratio + low cache hit % is a caching opportunity; high error rate is a stability problem; low cache hit % on a high-input model is leaving money on the table.',
    action: 'High I/O ratio + low cache hit % → enable prompt caching. High error rate → open the Errors tab for that model. Long avg input + short avg output → consider a smaller / cheaper model.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'insights-table': {
    title: 'All models',
    body: 'Sortable, searchable table of every model used in the selected window. Same metrics as the cards, plus throttled count and accounts. The Cost column is the in-code estimate; the Cost tab has Cost Explorer truth.',
    why: 'Cards show the top 12; this is the long tail. A model with a tiny request count but a 30% error rate would be invisible in the cards but immediately obvious here.',
    action: 'Sort by Error rate descending to find unstable models. Sort by I/O ratio to spot caching candidates. Filter by provider in the top FilterBar to narrow scope.',
  },

  'lifecycle-timeline': {
    title: 'Lifecycle timeline',
    body: 'Horizontal timeline of the top 8 legacy models that are actively in use in the selected window. Each row is one model; the colored band runs from its Legacy date to its EOL date. The vertical line on every row is today. The Legacy / Extended access / EOL milestones are dotted on each band.',
    why: 'A glance answers two questions at once: which models are getting close to EOL, and how much runway you have. Timeline beats a date column because it makes "we have 3 weeks" visceral instead of a number to mentally subtract from today.',
    action: 'CRITICAL bands (red) crossing through today → migrate now, you are on borrowed time. WARNING bands (orange) → plan migration before EOL. INFO (blue) → schedule a review with the application owner so the date is on their radar.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html',
  },
  'lifecycle-table': {
    title: 'Legacy models',
    body: 'Every Bedrock model in the LEGACY state from the live AWS API (bedrock:ListFoundationModels). Lifecycle dates and status come straight from AWS — no scraping, no bundled JSON. Click a row to expand the per-account drill-down: which accounts are using each model, in which regions, and when they last accessed it.',
    why: 'Once a model passes its EOL date, AWS reserves the right to stop accepting requests at any time. A workload relying on an EOL model is one announcement away from a P0. Legacy models also lag in throughput, latency, and pricing — migration usually improves the bill at the same time. Knowing which accounts are affected is the difference between "send a generic notice" and "ping the three teams that matter".',
    action: 'CRITICAL (past EOL or extended access) → migrate this week. Use Recommended upgrade as the starting point. WARNING (currently Legacy) → plan for next sprint. INFO (Legacy within 90 days) → flag proactively. Use Download CSV to share with stakeholders or ingest into Jira.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html',
  },
  'ops-account-breakdown': {
    title: 'Detailed breakdown',
    body: 'Flat per-(account, model, region, operation, traffic_type) row counts for the accounts covered by this report. Includes Converse / InvokeModel / streaming variants and the full traffic-type label (CRIS Global, CRIS US, On-Demand, Provisioned).',
    why: 'The structured findings above aggregate to (account, model, region) so they read cleanly. This breakdown is the raw evidence: which exact API operation and traffic profile each row represents — useful for exports and for double-checking the AI summary.',
    action: 'Export rows for review meetings. Spot accounts that mix CRIS and on-demand on the same model (incomplete migration). Verify operation mix — heavy ConverseStream means latency-sensitive workload, so prompt caching pays double.',
  },
};

export default function SectionPanel({ sectionId }) {
  const info = SECTION_INFO[sectionId];
  if (!info) {
    return (
      <HelpPanel header={<h2>About this section</h2>}>
        <p>Click <strong>Info</strong> on any container to see what it shows, why it matters, and what to do.</p>
      </HelpPanel>
    );
  }
  return (
    <HelpPanel
      header={<h2>{info.title}</h2>}
      footer={info.docLink ? (
        <SpaceBetween size="xs">
          <Box variant="awsui-key-label">Reference</Box>
          <Link external href={info.docLink}>{info.docLink}</Link>
        </SpaceBetween>
      ) : undefined}
    >
      <SpaceBetween size="m">
        <div>
          <Box variant="awsui-key-label">What it shows</Box>
          {info.body.split('\n').map((line, i) => <p key={i} style={{ margin: '4px 0' }}>{line}</p>)}
        </div>
        <div>
          <Box variant="awsui-key-label">Why it matters</Box>
          {info.why.split('\n').map((line, i) => <p key={i} style={{ margin: '4px 0' }}>{line}</p>)}
        </div>
        <div>
          <Box variant="awsui-key-label">Action to take</Box>
          {info.action.split('\n').map((line, i) => <p key={i} style={{ margin: '4px 0' }}>{line}</p>)}
        </div>
      </SpaceBetween>
    </HelpPanel>
  );
}
