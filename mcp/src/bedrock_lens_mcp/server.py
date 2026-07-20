"""MCP server entrypoint.

Registers tools that map 1:1 to dashboard tabs. Every tool is dispatcher-
agnostic — it calls the auto-selected backend (HttpBackend if the dashboard
is deployed, DirectBackend otherwise).

The MCP framework (FastMCP) handles JSON-RPC stdio plumbing.

Usage:
  bedrock-lens-mcp                # stdio MCP server
  python -m bedrock_lens_mcp.server  # equivalent

Env vars:
  BEDROCK_LENS_API       https://<cloudfront>.cloudfront.net  → Tier B/C mode
  BEDROCK_LENS_USER      user@domain                          → required if API set
  BEDROCK_LENS_PASSWORD  permanent password                   → required if API set
  AWS_REGION             default region for direct boto3 calls (Tier A)
  AWS_PROFILE            for direct calls
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .backends import auto_select


# Build the backend exactly once at import time. Errors during construction
# (e.g. BEDROCK_LENS_API set but missing credentials) propagate to the host
# IDE's MCP launcher — visible to the user.
_backend = auto_select()
_mode = _backend.mode

mcp = FastMCP("Bedrock Ops Lens")


def _format(payload: dict) -> str:
    """Return the JSON payload as a fenced code block. MCP tools return text;
    JSON-in-text is the conventional way to give the LLM structured data."""
    return "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"


# ---------------------------------------------------------------------------
# Tools — one per high-value dashboard view.
# Tool docstrings are sent to the LLM as the tool description; keep them
# short, action-oriented, and explicit about the params.
# ---------------------------------------------------------------------------

@mcp.tool()
async def health() -> str:
    """Returns the MCP server's mode (deployed vs direct) and connectivity status.

    Use this first to confirm the MCP is reachable and which data path it's
    using. In 'deployed' mode it confirms the dashboard URL and that auth
    succeeded; in 'direct' mode it returns the AWS account ID it's hitting.
    """
    return _format(_backend.health())


@mcp.tool()
async def overview_summary(days: int = 14) -> str:
    """Total Bedrock activity in the window: invocations, tokens, errors, throttles.

    Args:
        days: Lookback window. Default 14, max 30.
    """
    days = max(1, min(int(days), 30))
    return _format(_backend.overview_summary(days=days))


@mcp.tool()
async def by_user(days: int = 14, top_n: int = 25, group_by: str = "group") -> str:
    """Who is calling Bedrock: per IAM caller identity (identity.arn from
    invocation logs). Captured automatically on every invocation — works
    even when teams don't tag their requests.

    Args:
        days: Lookback window. Default 14, max 30.
        top_n: Max callers returned. Default 25.
        group_by: 'group' (role = app/team/workload), 'user' (session =
            individual caller, SSO login), or 'principal' (full identity).
    """
    days = max(1, min(int(days), 30))
    top_n = max(1, min(int(top_n), 200))
    if group_by not in ("group", "user", "principal"):
        group_by = "group"
    return _format(_backend.by_user(days=days, top_n=top_n, group_by=group_by))


@mcp.tool()
async def cost_summary(days: int = 30) -> str:
    """Total Amazon Bedrock spend over the window, with daily breakdown.

    Args:
        days: Lookback window. Default 30 (Cost Explorer's natural granularity).
    """
    days = max(1, min(int(days), 365))
    return _format(_backend.cost_summary(days=days))


@mcp.tool()
async def cost_by_account(days: int = 30) -> str:
    """Bedrock spend grouped by AWS account, top 20.

    Args:
        days: Lookback window. Default 30.
    """
    days = max(1, min(int(days), 365))
    return _format(_backend.cost_by_account(days=days))


@mcp.tool()
async def cost_by_model(days: int = 30, top_n: int = 12) -> str:
    """Top models by spend in the window. (Tier A returns volume only;
    Tier B+ returns dollar amounts.)

    Args:
        days: Lookback window. Default 30.
        top_n: How many models to return. Default 12.
    """
    days = max(1, min(int(days), 365))
    top_n = max(1, min(int(top_n), 50))
    return _format(_backend.cost_by_model(days=days, top_n=top_n))


@mcp.tool()
async def quotas(region: str | None = None) -> str:
    """Service Quotas snapshot for AWS/Bedrock and AWS/Bedrock-Runtime in this account.

    Args:
        region: AWS region. Default = AWS_REGION env or us-east-1.
    """
    return _format(_backend.quotas(region=region))


@mcp.tool()
async def model_insights(days: int = 14, top_n: int = 12) -> str:
    """Per-model invocations/tokens/errors. The 'which models am I actually using' answer.

    Args:
        days: Lookback window. Default 14.
        top_n: How many models to return. Default 12.
    """
    days = max(1, min(int(days), 30))
    top_n = max(1, min(int(top_n), 50))
    return _format(_backend.model_insights(days=days, top_n=top_n))


@mcp.tool()
async def model_lifecycle() -> str:
    """All foundation models accessible in this account, with their lifecycle status
    (Active, Legacy, EOL, Extended-Access). Helps answer 'are we using anything
    that's about to be deprecated?'.
    """
    return _format(_backend.model_lifecycle())


@mcp.tool()
async def errors_summary(days: int = 14) -> str:
    """Bedrock invocation errors and throttles. Helps spot capacity issues.

    Args:
        days: Lookback window. Default 14.
    """
    days = max(1, min(int(days), 30))
    return _format(_backend.errors_summary(days=days))


@mcp.tool()
async def latency_summary(days: int = 14) -> str:
    """Latency percentiles (p50/p90/p99) by model. Tier B+ only — Tier A
    returns a stub explaining why.

    Args:
        days: Lookback window. Default 14.
    """
    days = max(1, min(int(days), 30))
    return _format(_backend.latency_summary(days=days))


@mcp.tool()
async def ops_review(days: int = 14) -> str:
    """LLM-synthesized executive summary of the Bedrock fleet's last `days` days.
    Tier B+ only — Tier A returns a stub.

    Args:
        days: Window the synthesis covers. Default 14.
    """
    days = max(1, min(int(days), 30))
    return _format(_backend.ops_review_synthesize(days=days))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[bedrock-lens-mcp] starting in {_mode} mode "
          f"({'BEDROCK_LENS_API=' + os.environ.get('BEDROCK_LENS_API', '(unset)')})",
          flush=True)
    mcp.run()


if __name__ == "__main__":
    main()
