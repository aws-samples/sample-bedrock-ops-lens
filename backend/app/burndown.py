"""Output-token burndown multiplier — single source of truth.

Bedrock burns down the TPM quota using *more* than one quota-token per output
token for recent Anthropic Claude models. CloudWatch's `EstimatedTPMQuotaUsage`
bakes this in; any dashboard number that claims to track that metric must apply
the same multiplier or it will understate quota usage (badly — up to ~15x on the
output portion for Claude Opus 4.8).

Per-model multiplier (output tokens -> quota tokens), VERBATIM from the AWS
public doc (quotas-token-burndown.html):
  - Anthropic Claude Opus 4.8 ............................ 15x
  - Anthropic Claude Sonnet 5 ............................ 10x
  - All other Anthropic Claude version 4.7 and below ..... 5x
  - All other models ..................................... 1x  (1:1)

  Doc quote: "The burndown rate for Anthropic Claude models version 4.8 is 15x
  for output tokens ... and the burndown rate for Anthropic Claude Sonnet 5 is
  10x for output tokens. For all other Anthropic models version 4.7 and below,
  the burndown is 5x for output tokens. For all other models, the burndown rate
  is 1:1."

  Burndown applies ONLY to the bedrock-runtime endpoint. Per the doc: "Models
  available exclusively on the bedrock-mantle endpoint have separate quotas for
  input and output tokens, so burndown does not apply." Callers MUST pass
  is_mantle=True (or rate 1) for the mantle endpoint.

Authority:
  - Burndown rates + formula:
    https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html
  - "EstimatedTPMQuotaUsage ... includes cache write tokens and output burndown
    multipliers":
    https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-observability-ttft-quota/

Quota-token formula, VERBATIM from the doc's end-of-request calculation:

    quota_tokens = InputTokenCount + CacheWriteInputTokens
                 + OutputTokenCount * output_burndown_rate(model)

  "CacheReadInputTokens don't contribute to this calculation and are not counted
  toward your quota."

This repo's CW metrics map 1:1 to columns: total_input_tokens = InputTokenCount
(cache EXCLUDED), total_cache_write_input_tokens = CacheWriteInputTokens,
total_cache_read_input_tokens = CacheReadInputTokens. So the quota tokens are:

    total_input_tokens + total_cache_write_input_tokens
      + total_output_tokens * rate

Do NOT subtract cache-read (never in total_input_tokens) and DO add cache-write.

CRITICAL: when computing a *peak*, apply the rate to output per-period BEFORE
taking the max — the busiest quota-minute/hour can differ from the busiest
raw-token one. Do not multiply an already-summed raw peak.
"""
from __future__ import annotations

import re

# Matches "Claude <Type?> <major>[.<minor>]" in either a public name
# ("Claude Opus 4.8", "Claude 3.7 Sonnet") or a model id / CRIS-prefixed id
# ("anthropic.claude-opus-4-8-...", "us.anthropic.claude-3-7-sonnet-...").
# The optional type token (opus/sonnet/haiku) can appear before OR after the
# version depending on the SKU, so it's matched non-greedily and the version is
# the first <digits>[sep<digits>] group that follows "claude".
_CLAUDE_VER = re.compile(
    r"claude[\s\-_]+(?:(?:opus|sonnet|haiku)[\s\-_]+)?(\d+)(?:[.\-_](\d+))?"
)


def output_burndown_rate(model_id: str | None, public_name: str | None = None,
                         is_mantle: bool = False) -> int:
    """Output burndown multiplier per the AWS doc: 15 (Opus 4.8), 10 (Sonnet 5),
    5 (any other Anthropic Claude version 4.7 and below), else 1.

    is_mantle=True forces 1: burndown applies only to bedrock-runtime; the
    bedrock-mantle endpoint has separate input/output quotas so no multiplier.

    Accepts CRIS-prefixed ids (``us.`` / ``eu.`` / ``apac.`` / ``global.`` ...) —
    the prefix is irrelevant to the substring/version checks below. Pass the
    resolved public name too when the caller has it; either source is enough.
    """
    if is_mantle:
        return 1

    mid = (model_id or "").lower()
    name = (public_name or "").lower()

    # Opus 4.8 -> 15x (id form opus-4-8 or name form "opus 4.8").
    if ("opus-4-8" in mid or "opus 4.8" in name or "opus-4.8" in name):
        return 15

    # Sonnet 5 -> 10x.
    if ("sonnet-5" in mid or "sonnet 5" in name or "sonnet-5" in name):
        return 10

    # Any other Anthropic Claude version 4.7 and below -> 5x. Parse the first
    # "Claude <maj>[.<min>]" in either string; missing minor treated as .0.
    # Only Claude models qualify; non-Claude (Nova, GPT-OSS, Llama, ...) -> 1x.
    for hay in (name, mid):
        m = _CLAUDE_VER.search(hay)
        if m:
            major = int(m.group(1))
            minor = int(m.group(2) or 0)
            # "version 4.7 and below" -> 5x; anything above that not already
            # matched (Opus 4.8 / Sonnet 5 handled above) falls through to 1x.
            return 5 if (major, minor) <= (4, 7) else 1

    return 1  # non-Anthropic or unrecognized
