"""Bedrock Ops Lens MCP — IDE-side companion to the dashboard.

Exposes the same insights as the dashboard via Model Context Protocol tools.
Runs in two modes, auto-detected at startup:

  Tier A (no deployment) — BEDROCK_LENS_API not set
      Lightweight mode. Each tool calls AWS directly via boto3
      (CloudWatch Metrics, Cost Explorer, Service Quotas, Bedrock APIs).
      No historical depth beyond what those APIs return live; no per-tag
      attribution. Costs zero. Best for solo platform engineers.

  Tier B/C (deployed dashboard) — BEDROCK_LENS_API + BEDROCK_LENS_USER set
      Each tool proxies through the deployed REST API. Pre-aggregated
      historical data, per-tag cost attribution, multi-account scope.
      Sub-500ms tool-call latency.

The same set of tools is registered in both modes — the customer doesn't
need to learn a different command surface based on whether they have the
backend deployed.
"""
__version__ = "0.1.0"
