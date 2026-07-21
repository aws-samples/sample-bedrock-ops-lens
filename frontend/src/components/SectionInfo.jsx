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
  /* Cross-tab: bedrock-runtime vs bedrock-mantle                          */
  /* --------------------------------------------------------------------- */
  'endpoint-switcher': {
    title: 'bedrock-runtime vs bedrock-mantle',
    body: 'Every tab on this dashboard has a sub-tab switcher between the two Bedrock endpoints. bedrock-runtime is the long-standing API path (Converse, ConverseStream, InvokeModel, InvokeModelWithResponseStream). bedrock-mantle is the newer endpoint that exposes OpenAI-compatible Responses + Chat Completions APIs and the Anthropic Messages API. Both endpoints stay supported — Mantle is not a deprecation of runtime, it is a new entry point for OpenAI/Anthropic-native clients. The two endpoints publish to separate CloudWatch namespaces (AWS/Bedrock and AWS/BedrockMantle) and have subtly different gaps in what they emit.',
    why: 'Most fleets will end up with traffic on both endpoints. Looking at "all of Bedrock" without splitting would average bedrock-mantle latency gaps and quota oddities into the runtime numbers, masking real signal.',
    action: 'Switch to bedrock-mantle on any tab to see the Mantle slice in isolation. The coverage badge tells you what data path backs the active view: Live metric (CW publishes it), Live metric (partial) (CW publishes it but with known gaps), Log-derived (CW does not publish; we parse Bedrock invocation logs), Defaults only (only static AWS-published defaults available), or Not available (no data path exists).',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle-metrics.html',
  },
  'mantle-coverage-latency': {
    title: 'Latency coverage on bedrock-mantle',
    body: 'AWS does not publish latency or time-to-first-token metrics for the bedrock-mantle endpoint. The dashboard derives Mantle latency from Bedrock Model Invocation Logs when the customer enables them: per-request output.outputBodyJson.metrics.latencyMs is parsed and aggregated into p50 / p90 / p99 per (model, region, day).',
    why: 'Without invocation logging enabled, Mantle latency is invisible. Most observability tools render zero in this case — that is wrong. We render an explicit "not available" banner so an operator can tell "no traffic" apart from "no telemetry source enabled".',
    action: 'Enable Bedrock Model Invocation Logging in the AWS console (Bedrock > Settings > Model invocation logging > Enable). Point it at S3 or CloudWatch Logs. Once the dashboard ingester picks up the new logs (next 05:00 UTC or via manual ingest), Mantle latency populates.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html',
  },
  'mantle-coverage-quotas': {
    title: 'Quotas coverage on bedrock-mantle',
    body: 'The bedrock-mantle endpoint does not publish quotas through AWS Service Quotas — Mantle quotas are managed internally. The default published values per the AWS docs are 10M input TPM, 2M output TPM, and 100M RPM, with structured ramp-up. The dashboard surfaces these as static defaults for the Mantle sub-tab; live applied values are not retrievable.',
    why: 'A customer cannot see "is my Mantle workload near its quota" from anywhere today, including the AWS console. The dashboard at least shows the published ceiling so operators have a number to compare peak usage against.',
    action: 'For a quota increase on Mantle, contact AWS Support directly — Service Quotas console requests will not work for the Mantle endpoint until AWS exposes those quota codes publicly.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle.html',
  },

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
    body: 'Distribution across Bedrock-runtime API operations: InvokeModel, Converse, InvokeModelWithResponseStream, ConverseStream. The AWS/Bedrock CloudWatch metrics do NOT carry an operation dimension, so this is only populated from Bedrock model invocation logs. Without logging enabled, all traffic shows as "Not attributed".',
    why: 'Streaming operations (ConverseStream, InvokeModelWithResponseStream) indicate real-time UX. Non-streaming indicates batch or backend workloads.',
    action: 'Enable Bedrock model invocation logging to break traffic out by operation. Streaming-heavy fleets are more latency-sensitive — prioritize prompt caching and CRIS for those workloads.',
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
    title: 'Claude burndown risk',
    body: 'Recent Anthropic Claude models burn down the TPM quota faster on output tokens: each output token counts 15× toward TPM for Claude Opus 4.8, 5× for other Claude 3.7+ models (Sonnet/Opus/Haiku 3.7, 4, 4.x), and 1× for everything else. The "Burndown" column shows the per-model rate; "Peak TPM (quota)" applies it (input − cache-read + output × rate) so the number matches how CloudWatch EstimatedTPMQuotaUsage burns down the quota; "Quota util %" is that peak against your applied TPM limit.',
    why: 'Bedrock also reserves max_tokens × rate × RPM up-front when each request lands. A workload with max_tokens set to the model maximum but actual avg output of, say, 300 tokens still has the full max_tokens × rate × RPM reserved — so throttling can hit with 80%+ unused real capacity. For Opus 4.8 (15×) the over-reservation is 3× larger than the old 5× assumption.',
    action: 'Set max_tokens close to actual expected output (not the model maximum). One-line code change; throttling drops without a quota increase. This peak is from hourly-averaged data, so for the true per-minute throttling ceiling confirm against CloudWatch EstimatedTPMQuotaUsage (Sum, 1-minute).',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html',
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
  'status-codes': {
    title: 'Status Codes',
    body: 'Stacked hourly breakdown of every request by true HTTP status — 200 OK plus per-code errors (400, 403, 404, 408, 424, 429, 500, 503). This chart is sourced ONLY from Bedrock model invocation logs, which carry a real per-request errorCode. If invocation logging is not enabled for the monitored account(s), the chart shows a notice instead of fabricating a breakdown — CloudWatch metrics expose only all-4xx / all-5xx aggregates and cannot distinguish individual codes.',
    why: 'Individual codes have different owners and fixes: 429 = quota exceeded (platform), 400 = bad request (your client), 403 = IAM/model-access denial, 404 = wrong model/profile ID, 408 = model timeout, 424 = model error, 500/503 = Bedrock-side. A true per-code view tells you who to page.',
    action: 'To populate this chart, enable Bedrock model invocation logging to S3 (see the deployment README) and re-run ingestion. 429 sustained → request a Service Quotas increase. 5xx sustained → check the AWS Health Dashboard. 400/404 spike after a deploy → roll back.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html',
  },
  'error-trend': {
    title: 'Error trend (429 / 4xx / 5xx)',
    body: 'Stacked daily breakdown of failed invocations from CloudWatch metrics. AWS/Bedrock publishes three trustworthy error counters: InvocationThrottles (real 429s), InvocationClientErrors (all 4xx) and InvocationServerErrors (all 5xx). This chart shows throttles (429), the remaining non-throttle 4xx aggregate, and the 5xx aggregate — it does NOT split the non-throttle 4xx into individual codes. For a true per-code view (403 vs 404 vs 408 vs 424 …), see the Status Codes chart above, sourced from invocation logs. Click any bar to drill into the hourly breakdown (last 7 days only).',
    why: '429 = quota exceeded (request a Service Quotas increase). Non-throttle 4xx = client-side request problems. 5xx = Bedrock-side. Separating real throttles from the rest tells you immediately whether you are quota-bound or have a client bug.',
    action: '429 sustained → request a quota increase. 4xx (non-throttle) spike after a deploy → roll back / check payloads. 5xx sustained → check the AWS Health Dashboard. For exact non-throttle codes, enable invocation logging and use the Status Codes chart.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-cw.html',
  },
  'errors-by-model': {
    title: 'Errors by model',
    body: 'Per-model failure breakdown from CloudWatch. Columns: 429 (real throttles), 4xx* (remaining non-throttle 4xx aggregate), 5xx (all server errors). CloudWatch does not expose individual non-throttle codes — see the Status Codes chart for those.',
    why: 'A single misbehaving model usually dominates the error count, so isolating it scopes the investigation. 429 is quota-side, 4xx* is client-side, 5xx is AWS-side.',
    action: 'Highest 429 → drill into Throttle hotspots for that model and check its quota. Highest 4xx* → engage the application owner (request payloads). Highest 5xx → check the AWS Health Dashboard for that model/region.',
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
    title: 'Claude burndown risk',
    body: 'Recent Anthropic Claude models count each output token as more than 1 against TPM: 15× for Claude Opus 4.8, 5× for other Claude 3.7+ (Sonnet/Opus/Haiku 3.7, 4, 4.x), 1× otherwise. Bedrock also reserves max_tokens × rate × RPM at request time, only adjusting after the request completes. "Peak TPM (quota)" applies the per-model rate to output per-hour ((input − cache-read) + output × rate) before taking the peak, and "Quota util %" is that peak against the applied TPM limit. Cache-read input tokens are excluded (they don\'t count toward the quota); hours predating the cache-read column are excluded from the peak rather than counted inflated. NOTE: this peak is from hourly-averaged data — TPM quotas are enforced per-minute, so treat CloudWatch EstimatedTPMQuotaUsage (Sum, 1-minute) as the authoritative throttling ceiling.',
    why: 'If max_tokens is set to the model maximum but actual avg output is 300 tokens, Bedrock still reserves the full budget. The workload hits ThrottlingException with 80%+ unused real capacity — and for Opus 4.8 (15×) the over-reservation is 3× worse than the old 5× assumption. Highest-impact, zero-cost fix for Claude throttling.',
    action: 'Set max_tokens close to actual expected output (check the Avg output column for the right ballpark — typically 200-1000), not the model maximum. One-line client change. Throttling drops without any quota increase.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html',
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
    title: 'Cost share by model',
    body: 'Top 8 models by real AWS Cost Explorer spend over the window. When Cost Explorer breaks Bedrock out per model (EDP/marketplace line items), these are exact. When Cost Explorer reports one consolidated "Amazon Bedrock" line (most accounts), the real CE total is allocated across models by their token usage — the header states which mode is in effect.',
    why: 'Grounded in your actual invoice, not a price guess: the dollar total always matches Cost Explorer. Only the per-model split is approximated, and only when AWS itself doesn\'t provide one.',
    action: 'Use it to see which models dominate spend. The full daily trend and by-account breakdown live on the Cost Insights tab.',
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
    body: 'Hourly peak tokens-per-minute for the selected (account · model · region) over the last 14 days, plotted against the applied AWS Service Quotas TPM limit (the red line). TPM is quota-accurate: (input − cache-read) + output × the model\'s burndown multiplier (15× for Claude Opus 4.8, 5× for other Claude 3.7+, 1× otherwise), divided by 60 — matching how CloudWatch EstimatedTPMQuotaUsage burns down the quota. Because the source is hourly-averaged, this reads lower than CloudWatch\'s true per-minute peak for bursty traffic; for the actual throttling ceiling use CloudWatch EstimatedTPMQuotaUsage (Sum, 1-minute). When the data range is much smaller than the limit, the y-axis switches to logarithmic so both fit on the same chart.',
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

  'insights-request-shape': {
    title: 'Request shape by model',
    body: 'Per-model average input tokens per request, average output tokens per request, the input:output ratio, and total requests over the window. Request shape drives capacity: TPM ≈ RPM × (avg input + avg output tokens per request). A typical chatbot runs around 10:1 input:output.',
    why: 'You cannot size a quota without knowing the shape of the traffic. Two workloads at the same RPM can have wildly different TPM if one is input-heavy (long context, RAG) and the other output-heavy (generation). Shape also points at the right optimization: input-heavy (ratio > 50:1) is a prompt-caching candidate; output-heavy (ratio < 2:1) on Claude 4+ amplifies TPM burndown, so max_tokens tuning matters most there.',
    action: 'Input-heavy → enable prompt caching for stable system prompts and shared context (~90% cheaper on the cached portion). Output-heavy on Claude 4+ → tune max_tokens close to actual output length. Use avg input + avg output × RPM to project TPM before filing a Service Quotas increase.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html',
  },
  'insights-multimodal': {
    title: 'Multimodal token breakdown',
    body: 'For models that process more than text, this splits token usage into input text, input speech, output text, output speech, and output images per model. Only shown when the fleet actually has multimodal usage — text-only fleets see nothing here.',
    why: 'Multimodal tokens (speech, image) are priced and rate-limited differently from text and are easy to under-account for when planning capacity. Seeing the split tells you whether a spike is coming from text volume or from a growing speech/image workload.',
    action: 'Growing speech or image token share → confirm the relevant modality quotas and pricing are sized for it. If image output dominates, verify the workload genuinely needs image generation vs. a cheaper text path.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html',
  },
  'insights-mantle-token-pct': {
    title: 'Token size percentiles (per inference)',
    body: 'p50 / p90 / p99 of input and output tokens per inference for each model on the bedrock-mantle endpoint. Mantle publishes no latency to CloudWatch, so this per-request token distribution is the primary shape signal available for Mantle traffic.',
    why: 'Averages hide the tail. A model with a modest average input but a heavy p99 input has a small set of very large requests that drive TPM burndown and can trip throttling even when the average looks safe. The percentile spread tells you how bursty the request sizes are.',
    action: 'Wide gap between p50 and p99 input → the workload has a few very large prompts; size TPM headroom against p99, not the average. Large p99 output on Claude 4+ → tune max_tokens for those calls to limit burndown.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle-metrics.html',
  },
  'workloads-setup': {
    title: 'Set up per-workload attribution',
    custom: (
      <SpaceBetween size="m">
        <div>
          <Box variant="awsui-key-label">What this is</Box>
          <p style={{ margin: '4px 0' }}>
            The Workloads tab answers “which of my use-cases is driving Bedrock
            usage, throttling, and latency” — attribution the AWS-native metrics
            can’t provide, because CloudWatch is keyed by <strong>model</strong>,
            not by your application’s use-case.
          </p>
          <p style={{ margin: '4px 0' }}>
            It works only if you front Bedrock with a <strong>shared GenAI proxy
            / gateway</strong> (LiteLLM, a Bedrock gateway, an internal SDK
            wrapper, etc.) that can tag each call with a <code>workload</code>.
            If your apps call Bedrock directly with no common layer, this tab
            stays empty — the rest of the dashboard is unaffected.
          </p>
        </div>

        <div>
          <Box variant="awsui-key-label">Privacy model</Box>
          <p style={{ margin: '4px 0' }}>
            Your proxy drops <strong>one metadata-only event per request</strong>
            into an S3 bucket; the dashboard reads that bucket <strong>read-only</strong>
            (no inbound endpoint, never sits in your request path). No prompt or
            response text ever leaves your proxy.
          </p>
        </div>

        <div>
          <Box variant="awsui-key-label">1. Emit an event per request</Box>
          <p style={{ margin: '4px 0' }}>
            After each Bedrock call, append one NDJSON line to S3 under this
            exact layout (<code>.jsonl</code> or <code>.jsonl.gz</code>):
          </p>
          <Box variant="code" display="block">
            s3://&lt;your-bucket&gt;/proxy-events/&lt;region&gt;/&lt;YYYY&gt;/&lt;MM&gt;/&lt;DD&gt;/&lt;HH&gt;/*.jsonl
          </Box>
          <Box variant="code" display="block">
            {'{'}"ts":"2026-07-04T18:03:22Z","workload":"flights-search",<br/>
            &nbsp;"model":"anthropic.claude-opus-4-8","endpoint":"runtime","region":"us-east-1",<br/>
            &nbsp;"input_tokens":812,"output_tokens":143,"cache_read_tokens":0,<br/>
            &nbsp;"status":200,"throttled":false,"latency_ms":940,"request_id":"msg_..."{'}'}
          </Box>
          <p style={{ margin: '4px 0' }}>
            <code>workload</code> is your attribution key; <code>endpoint</code>
            is <code>runtime</code> or <code>mantle</code>. A working,
            copy-paste starting point ships in the deployment package under
            <code> tools/reference-proxy/</code>, and the full field reference
            is in the project README under “Workloads: per-workload attribution”.
          </p>
        </div>

        <div>
          <Box variant="awsui-key-label">2. Grant the dashboard read access</Box>
          <p style={{ margin: '4px 0' }}>
            Add a bucket policy allowing the ingester role <code>s3:GetObject</code>
            + <code>s3:ListBucket</code> on <code>.../proxy-events/*</code>
            (read-only; cross-account is supported).
          </p>
        </div>

        <div>
          <Box variant="awsui-key-label">3. Point the deploy at the bucket</Box>
          <Box variant="code" display="block">
            export PROXY_EVENTS_BUCKET=your-genai-proxy-events<br/>
            export PROXY_EVENTS_REGIONS=us-east-1,us-west-2<br/>
            ./deploy.sh --yes
          </Box>
          <p style={{ margin: '4px 0' }}>
            The daily ingester reads new events into this view. Nothing is ever
            synthesized — the tab shows only what your proxy emits. Once the
            first events land, this tab populates automatically and stays on.
          </p>
        </div>
      </SpaceBetween>
    ),
  },
  'wl-quota': {
    title: 'Quota utilization by dimension',
    body: 'Peak TPM (tokens-per-minute) for each dimension value, as a share of the applicable Bedrock Service Quotas limit. Bedrock quota limits are set per (account, model, region) — never per workload — so this attributes a share of that ceiling to each value: for each (value, model) it takes the busiest hour of quota-tokens (input + output × the AWS per-model burndown multiplier; cache-read excluded per the AWS burndown doc), converts to per-minute TPM, and divides by the applied limit (or the published default when no increase is set).',
    why: 'Tokens and throttle rate tell you WHO is heavy and WHO is getting throttled; quota utilization tells you WHO is about to BE throttled. A workload sitting at 85% of its model TPM ceiling is one traffic spike from 429s — this is the leading indicator the account-level view can\'t give you when many workloads share one model.',
    action: 'Values in the red (>80%) need headroom now — request a quota increase for that model/region, or move the workload to a cross-region inference profile (CRIS) for ~2x capacity. This is a proxy-derived ESTIMATE: it uses proxy-reported tokens and excludes cache-write (which the proxy doesn\'t send), so it can slightly under-count vs CloudWatch\'s native EstimatedTPMQuotaUsage — treat it as a floor.',
  },
  'wl-tokens': {
    title: 'Tokens by workload',
    body: 'Input and output token totals for the top workloads in the window. A "workload" is your application-level use-case (search, chat, summarize…) as tagged by a GenAI proxy/gateway fronting Bedrock. This attribution is not available from AWS-native metrics: CloudWatch is keyed by model, not by use-case. It only populates when a proxy emits one metadata-only event per request to the S3 bucket this dashboard reads (no prompt/response text, never in your request path).',
    why: 'Model-level metrics tell you "Claude Sonnet is busy" but not "the recommendation engine is what is driving it". Token totals per workload are the truth metric for cost allocation and for finding which use-case to optimize first.',
    action: 'No data? Front Bedrock with a shared proxy (LiteLLM, a Bedrock gateway, an internal SDK wrapper) that tags each call and drops events to S3, set PROXY_EVENTS_BUCKET in config.yaml, and re-run ./deploy.sh. See the README "Workloads: per-workload attribution" section. High output-token workloads on Claude 4+ burn TPM fastest — tune max_tokens there first.',
  },
  'wl-throttle': {
    title: 'Throttle rate by workload',
    body: 'Percentage of each workload\'s requests that were throttled (HTTP 429) in the window, from proxy-emitted per-request status. Workloads are sorted by throttle rate so the most quota-starved use-cases are first.',
    why: 'Throttling is invisible at the model level when several workloads share a model — one bursty use-case can trip the shared quota and degrade every other caller. Splitting throttle rate by workload isolates the culprit.',
    action: 'A single workload with a high throttle rate → move it to a cross-region inference profile (CRIS) for ~2x quota, or request a quota increase scoped to its traffic. Broadly elevated throttling → the shared quota is undersized for the fleet.',
  },
  'wl-table': {
    title: 'Per-workload detail',
    body: 'One row per workload with requests, input/output tokens, throttle %, error %, p99 latency, and which Bedrock endpoints (runtime / mantle) it used. Endpoint-agnostic — the proxy reports the same shape for both. Downloadable as CSV for chargeback or review.',
    why: 'The charts show the top workloads; this table is the complete, exportable ledger. p99 latency and error % per workload surface reliability problems that request/token counts alone hide.',
    action: 'Export for chargeback or a review meeting. A workload with high p99 latency but low volume → likely large prompts or a slow model choice; check its token percentiles. High error % → engage that use-case\'s owner.',
  },
  'identity-usage': {
    title: 'Top callers (by IAM principal)',
    body: 'Per-IAM-principal usage — requests, input/output tokens, failed requests, and distinct models used — attributed from the identity.arn in Bedrock Model Invocation Logs. This complements the tag-based workload view with principal-level attribution. It is empty when model invocation logging is not enabled, since that is the only source of caller identity.',
    why: 'Tag-based workload attribution needs a proxy or tagging discipline; the invocation-log identity is emitted automatically. This is the most direct answer to "which role/user is driving this traffic" and surfaces principals with high failure counts that a workload rollup can hide.',
    action: 'Enable Bedrock Model Invocation Logging (Bedrock > Settings > Model invocation logging) to populate this. High failed-request counts on one principal → engage that caller. A single principal dominating requests → confirm its quotas and CRIS adoption.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html',
  },
  'mantle-projects': {
    title: 'Per-project usage (chargeback)',
    body: 'Per-project usage on the bedrock-mantle endpoint — requests, 4xx client errors, input/output tokens, and distinct models used — grouped by the Mantle Project dimension. Intended for chargeback and per-project accountability.',
    why: 'The Project dimension is Mantle-native attribution: it does not need a proxy or tag conventions. When populated it is the cleanest way to split a shared Mantle fleet across teams for cost and quota accountability.',
    action: 'Use the token columns for chargeback allocation. A project with a high 4xx share → engage that team about malformed requests. If empty, the Mantle Project dimension has no data yet for this window.',
    docLink: 'https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle-metrics.html',
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
      {/* An entry may either supply the standard shows/why/action strings, or
          a `custom` React node for rich content (numbered steps, code blocks)
          — e.g. the Workloads setup guide that used to live only in the README. */}
      {info.custom ? info.custom : (
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
      )}
    </HelpPanel>
  );
}
