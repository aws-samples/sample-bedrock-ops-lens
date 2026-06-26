"""Output-token burndown multiplier — single source of truth.

Bedrock burns down the TPM quota using *more* than one quota-token per output
token for recent Anthropic Claude models. CloudWatch's `EstimatedTPMQuotaUsage`
bakes this in; any dashboard number that claims to track that metric must apply
the same multiplier or it will understate quota usage (badly — up to ~15x on the
output portion for Claude Opus 4.8).

Per-model multiplier (output tokens -> quota tokens), per the AWS public docs:
  - Anthropic Claude Opus 4.8 ............................ 15x
  - All other Anthropic Claude 3.7 and later (Sonnet/Opus/Haiku 3.7, 4, 4.x)  5x
  - Everything else (Claude <= 3.6, non-Claude) .......... 1x

Authority:
  - Burndown rates + formula:
    https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html
  - "EstimatedTPMQuotaUsage ... includes cache write tokens and output burndown
    multipliers":
    https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-observability-ttft-quota/

Quota-token formula (per period, e.g. per hour or per minute):

    quota_tokens = (InputTokenCount + CacheWriteInputTokens)
                 + OutputTokenCount * output_burndown_rate(model)

CacheReadInputTokens do NOT count toward the quota and must be excluded. In this
repo `total_input_tokens` already includes cache-write, so
`total_input_tokens - total_cache_read_input_tokens` equals
`InputTokenCount + CacheWriteInputTokens`.

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


def output_burndown_rate(model_id: str | None, public_name: str | None = None) -> int:
    """Resolve a Bedrock modelId (or public model name) to its output burndown
    multiplier: 15 for Claude Opus 4.8, 5 for other Claude 3.7+, else 1.

    Accepts CRIS-prefixed ids (``us.`` / ``eu.`` / ``apac.`` / ``global.`` ...) —
    the prefix is irrelevant to the substring/version checks below. Pass the
    resolved public name too when the caller has it (e.g. from a codename map);
    either source is enough.
    """
    mid = (model_id or "").lower()
    name = (public_name or "").lower()

    # Opus 4.8 -> 15x. Check both the id form (opus-4-8) and the name form
    # (opus 4.8), tolerant of separators.
    if ("claude-opus-4-8" in mid or "opus-4-8" in mid
            or "opus 4.8" in name or "opus-4.8" in name):
        return 15

    # Other Claude 3.7+ -> 5x. Parse the first "Claude <maj>[.<min>]" we find in
    # either string; a missing minor is treated as .0 (so bare "Claude Sonnet 4"
    # == 4.0 >= 3.7 -> 5x). Claude 3.0 / 3.5 / 3.6 -> 1x.
    for hay in (name, mid):
        m = _CLAUDE_VER.search(hay)
        if m:
            major = int(m.group(1))
            minor = int(m.group(2) or 0)
            return 5 if (major, minor) >= (3, 7) else 1

    return 1  # non-Claude or unrecognized
