# Skill: Bedrock Operational Assessment

## Description
Generate a narrative operational assessment of an Amazon Bedrock workload from pre-computed metrics. Your job is to analyze the numbers, explain what they mean, and provide actionable recommendations.

## CRITICAL RULES
- Every finding MUST cite actual numbers from the metrics report — never generalize
- Do NOT fabricate or estimate any metric. If data is missing, say "Data not available"
- Do NOT assume the reader knows Bedrock concepts. Briefly explain each concept (CRIS, burndown, prompt caching, etc.) before discussing the issue
- Only provide detailed pricing/upgrade recommendations for LEGACY models with active traffic. Skip unused models
- DISPLAY THE COMPLETE ASSESSMENT INLINE in the chat — do not just save to file
- **NEVER say logging is needed for token counts, invocations, throttles, latency, or cost analysis.** All of these come from basic CloudWatch metrics which are FREE and AUTOMATIC. Logging only captures request/response content for compliance/debugging. Do NOT conflate the two.

## Input
You receive:
1. An aggregated metrics report with per-model usage data (from the `run_bedrock_ops_review` tool)
2. Reference: Bedrock Quota, Throttling and Latency best practices (https://w.amazon.com/bin/view/AmazonBedrock/Quotas/)

## Bedrock Concepts to Evaluate

### Rate Limits and Quotas
- RPM (Requests Per Minute): max requests per minute
- TPM (Tokens Per Minute): max input + output tokens per minute
- Calculate peak-to-quota percentage. Flag if peak usage exceeds quotas

### Request Shape
- Input/output token ratio per model. Typical ratio is ~10:1
- Flag models with unusual ratios (e.g., output-heavy where ratio < 3:1)
- Output-heavy workloads consume disproportionate compute

### Burndown (Claude 4+ models only)
- Each output token counts as 5 tokens toward TPM quota
- Bedrock reserves max_tokens × 5 × RPM worth of TPM at request time
- Calculate raw TPM vs burndown-adjusted TPM
- Best practice: set max_tokens close to actual expected output, not model maximum

### Cross-Region Inference Service (CRIS)
- Models using "us." prefix route across multiple regions (us-east-1, us-west-2, us-east-2)
- Models using "anthropic." prefix are single-region only
- CRIS quotas are typically 2x on-demand, no additional cost
- Identify models that should migrate to CRIS
- Check Global CRIS quotas — default values bottleneck cross-region routing

### Throttling
- Throttle rate = avg_throttles / (avg_invocations + avg_throttles) × 100
- Flag models with throttle rate > 1%
- Best practices: exponential backoff retries, appropriate max_tokens, CloudWatch alerts at 80% (warning) and 95% (critical)

### Latency
- Increasing quotas does NOT improve latency
- Latency depends on model, input tokens, and output tokens
- TTFT (Time To First Token) + OTPS (Output Tokens Per Second) = total latency
- Flag models with high P95 latency

### Prompt Caching
- Reduces TTFT and input token processing cost
- Check CacheReadInputTokens > 0 (active) vs 0 (not enabled)
- Best practice: always enable for models that support it

### Context Length Routing
- Bedrock auto-routes to context length variants (18K, 51K, 200K)
- Larger variants have dramatically lower concurrency (200K = concurrency of 1)
- If max_tokens is set too high, requests route to larger variants unnecessarily

### Model Lifecycle
- ACTIVE = current recommended version
- LEGACY = still works but no longer recommended — plan migration before formal deprecation
- Only flag LEGACY models that have **actual invocations** (from CloudWatch basic metrics)
- **Always show the invocation count and time period** (e.g., "4 invocations in last 7 days") so the reader can judge severity
- Low invocation counts (< 100 in the period) may indicate test traffic — note this explicitly
- Common upgrade paths:
  - Claude 3 Haiku → Claude 3.5 Haiku → Claude Haiku 4.5 (via CRIS)
  - Claude 3 Sonnet → Claude Sonnet 4 → Claude Sonnet 4.5 (via CRIS)
  - Claude 3 Opus → Claude Opus 4 → Claude Opus 4.5 (via CRIS)
  - Claude Instant → Claude 3.5 Haiku
  - Claude V2 → Claude 3.5 Sonnet V2
  - Llama 2 → Llama 3.3 70B
  - Llama 3 8B/70B → Llama 3.3 70B or Llama 4 Scout/Maverick
  - Titan Text Express/Lite → Nova Micro (savings: -77% to -88%)
  - Titan Text Premier → Nova Lite (savings: -84% to -88%)

### Approximate Pricing (per 1M tokens: input / output)
- Claude Instant: $0.80 / $2.40
- Claude V2: $8.00 / $24.00
- Claude 3 Haiku: $0.25 / $1.25
- Claude 3 Sonnet: $3.00 / $15.00
- Claude 3 Opus: $15.00 / $75.00
- Claude 3.5 Haiku: $0.80 / $4.00
- Claude 3.5 Sonnet: $3.00 / $15.00
- Claude Haiku 4.5: $0.80 / $4.00
- Claude Sonnet 4/4.5: $3.00 / $15.00
- Claude Opus 4/4.5: $15.00 / $75.00
- Titan Text Express: $0.20 / $0.60
- Titan Text Lite: $0.15 / $0.20
- Nova Micro: $0.035 / $0.14
- Nova Lite: $0.06 / $0.24
- Nova Pro: $0.80 / $3.20
- Llama 2 (13B/70B): $0.75 / $1.00
- Llama 3.3 70B: $0.72 / $0.72

## Output Format

Start with this disclaimer:
> "Note: The following findings are directional and will be revised pending additional context. Recommendations are based on general best practices and may require adjustment based on workload-specific constraints such as latency sensitivity, compliance requirements, or architectural preferences."

### Section 1: Key Findings
Each finding must have:
- Clear headline summarizing the issue (e.g., "60% of All Requests Are Being Throttled")
- Data table with actual numbers
- "Why it matters" block explaining business/operational impact

### Section 2: Per-Model Analysis
For each model with issues:
- Current usage vs quota (RPM and TPM with actual numbers)
- Throttle rate with actual throttle counts
- Quota source (On-Demand vs CRIS) and whether migration is needed
- Burndown impact if Claude 4+ model
- Prompt caching status
- Lifecycle status — if LEGACY, flag it and name the recommended upgrade

### Section 3: Model Lifecycle & Upgrade Assessment
- Total LEGACY vs ACTIVE count and percentage
- For LEGACY models WITH active traffic: current model ID, **invocation count and time period**, **spend in the period and projected monthly/annual cost**, recommended upgrade (copy-paste ready), pricing comparison, cost direction, migration effort (Low/Medium)
- Show total financial exposure: "Total LEGACY model spend: $X in period → $Y/month → $Z/year"
- If invocation count is low (< 100), note it may be test traffic
- For LEGACY models WITHOUT traffic: brief summary line only
- Highlight upgrades that SAVE money as quick wins with dollar amounts

### Section 4: Recommendations
Each recommendation must:
- Reference the specific Bedrock concept
- Start with a brief "What is [concept]?" explanation
- Include "Why it matters" for this workload
- Be actionable with exact steps (e.g., "Change model ID from X to Y")
- Include lifecycle upgrades alongside operational recommendations
- If the workload uses streaming workloads, mention that Bedrock's Mantle API (OpenAI-compatible endpoint) may offer lower TTFB and higher streaming throughput compared to ConverseStream. Suggest benchmarking in their environment. Reference: https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html

### Section 5: Priority Actions
Ordered table: action, effort level, impact, cost impact, justification
- CRITICAL: LEGACY models approaching EOL with active traffic/quotas (will break)
- CRITICAL: CRIS vs Global CRIS quota gaps causing silent throttling
- HIGH: Model migrations for non-urgent LEGACY models
- MEDIUM: Alarms, prompt caching, optimization opportunities
- LOW: Enable CloudWatch invocation logging (for compliance/debugging — request/response content capture, not needed for operational metrics)

### Section 6: CloudWatch Metrics vs Logging (important distinction)

**Basic CloudWatch metrics** (Invocations, Throttles, Latency, TokenCounts, CacheRead/Write, EstimatedTPMQuotaUsage) are published by Bedrock **automatically** — no logging configuration needed. These provide everything needed for this operational review.

**CloudWatch invocation logging** captures the actual **request/response content** (prompts and completions). This is a compliance and debugging feature, NOT required for operational metrics. Enable it only if you need:
- Audit trail of what was sent to/from models
- Debugging specific request failures
- Compliance requirements for content retention

If the report shows logging is OFF, include these steps to enable it:

1. **Via Console**:
   - Go to Amazon Bedrock → Settings → Model invocation logging
   - Enable CloudWatch logging
   - Select log group (create one if needed, e.g., `/aws/bedrock/invocations`)
   - Enable Text, Image, and Embedding data delivery
   - Click Save

2. **Via CLI**:
   ```
   aws bedrock put-model-invocation-logging-configuration --logging-config '{
     "cloudWatchConfig": {
       "logGroupName": "/aws/bedrock/invocations",
       "roleArn": "arn:aws:iam::ACCOUNT_ID:role/BedrockLoggingRole",
       "largeDataDeliveryS3Config": {"bucketName": "your-bucket", "keyPrefix": "bedrock-logs/"}
     },
     "textDataDeliveryEnabled": true,
     "imageDataDeliveryEnabled": true,
     "embeddingDataDeliveryEnabled": true
   }'
   ```

3. **After enabling**: Wait 7 days for detailed metrics to accumulate, then re-run this review for a complete analysis.

### Model Lifecycle Note
LEGACY models are flagged for migration based on CloudWatch basic metrics (Invocations > 0), which are available even without logging enabled. The lifecycle assessment works regardless of logging status.
