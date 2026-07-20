"""Backend dispatcher.

Each MCP tool calls into a Backend object that hides whether we're
proxying through the deployed REST API or going direct to AWS.
"""
from __future__ import annotations

import os
from typing import Any, Protocol

from . import api_client, direct_collector


class Backend(Protocol):
    mode: str

    def health(self) -> dict: ...
    def overview_summary(self, *, days: int) -> dict: ...
    def by_user(self, *, days: int, top_n: int, group_by: str) -> dict: ...
    def cost_summary(self, *, days: int) -> dict: ...
    def cost_by_account(self, *, days: int) -> dict: ...
    def cost_by_model(self, *, days: int, top_n: int) -> dict: ...
    def quotas(self, *, region: str | None) -> dict: ...
    def model_insights(self, *, days: int, top_n: int) -> dict: ...
    def model_lifecycle(self) -> dict: ...
    def errors_summary(self, *, days: int) -> dict: ...
    def latency_summary(self, *, days: int) -> dict: ...
    def ops_review_synthesize(self, *, days: int) -> dict: ...


# ---------------------------------------------------------------------------
# Tier B/C — proxy through the deployed REST API
# ---------------------------------------------------------------------------
class HttpBackend:
    mode = "deployed"

    def __init__(self, client: api_client.ApiClient) -> None:
        self.c = client

    def health(self) -> dict:
        return {"status": "ok", "backend": self.mode,
                "via": "deployed dashboard at " + self.c.base_url}

    # --- volumetric / overview ---
    def overview_summary(self, *, days: int) -> dict:
        return self.c.get("/summary", params={"days": days})

    def by_user(self, *, days: int, top_n: int, group_by: str = "group") -> dict:
        return {
            "summary": self.c.get("/by-user/summary", params={"days": days, "top_n": top_n, "group_by": group_by}),
            "by_model": self.c.get("/by-user/by-model", params={"days": days}),
        }

    # --- cost ---
    def cost_summary(self, *, days: int) -> dict:
        return self.c.get("/cost-summary", params={"days": days})

    def cost_by_account(self, *, days: int) -> dict:
        return self.c.get("/cost-by-account", params={"days": days})

    def cost_by_model(self, *, days: int, top_n: int) -> dict:
        return self.c.get("/cost-by-model", params={"days": days, "top_n": top_n})

    # --- quotas ---
    def quotas(self, *, region: str | None) -> dict:
        params = {}
        if region:
            params["region"] = region
        return self.c.get("/quotas", params=params)

    # --- per-model deep-dive ---
    def model_insights(self, *, days: int, top_n: int) -> dict:
        return self.c.get("/model-insights", params={"days": days, "top_n": top_n})

    # --- foundation models / lifecycle ---
    def model_lifecycle(self) -> dict:
        return self.c.get("/model-lifecycle")

    # --- errors ---
    def errors_summary(self, *, days: int) -> dict:
        return self.c.get("/errors-by-model", params={"days": days})

    # --- latency ---
    def latency_summary(self, *, days: int) -> dict:
        return self.c.get("/latency-by-model", params={"days": days})

    # --- ops review (LLM-synthesized exec brief) ---
    def ops_review_synthesize(self, *, days: int) -> dict:
        # Synthesis calls Bedrock with a long context — 1–2 minutes is normal.
        # Use a 180s timeout instead of the default 30s.
        return self.c.post("/ops-review/synthesize",
                            json_body={"days": days},
                            timeout_s=180.0)


# ---------------------------------------------------------------------------
# Tier A — direct AWS calls via boto3
# ---------------------------------------------------------------------------
class DirectBackend:
    mode = "direct"

    def health(self) -> dict:
        try:
            ident = direct_collector.whoami()
            return {
                "status": "ok",
                "backend": self.mode,
                "account": ident.get("Account"),
                "arn": ident.get("Arn"),
                "via": "direct boto3 (no deployed dashboard)",
            }
        except Exception as e:
            return {"status": "degraded", "backend": self.mode, "error": str(e)}

    def overview_summary(self, *, days: int) -> dict:
        return direct_collector.overview_summary(days=days)

    def by_user(self, *, days: int, top_n: int, group_by: str = "group") -> dict:
        return {
            "window_days": days,
            "_note": (
                "Tier A: per-caller attribution needs the deployed pipeline — "
                "it is built from Bedrock model invocation logs (identity.arn), "
                "which CloudWatch metrics don't carry. Deploy the dashboard "
                "(Tier B) or query the invocation-logs bucket directly."
            ),
        }

    def cost_summary(self, *, days: int) -> dict:
        return direct_collector.cost_summary(days=days)

    def cost_by_account(self, *, days: int) -> dict:
        return direct_collector.cost_by_account(days=days)

    def cost_by_model(self, *, days: int, top_n: int) -> dict:
        # CE doesn't natively group by model — model insights via CW is the
        # closest analog. Return that with a note.
        out = direct_collector.model_insights(days=days, top_n=top_n)
        out["_note"] = (
            "Tier A: CW Metrics doesn't carry $; use model_insights for volume "
            "and cost_summary for $ totals. Per-model $ requires the deployed "
            "dashboard's Cost Explorer ingestion."
        )
        return out

    def quotas(self, *, region: str | None) -> dict:
        return direct_collector.quotas(region=region)

    def model_insights(self, *, days: int, top_n: int) -> dict:
        return direct_collector.model_insights(days=days, top_n=top_n)

    def model_lifecycle(self) -> dict:
        return direct_collector.model_lifecycle()

    def errors_summary(self, *, days: int) -> dict:
        # CW Bedrock has aggregate counters but not per-model error breakdown
        # without a separate per-modelId loop. Return aggregate for Tier A.
        ovr = direct_collector.overview_summary(days=days)
        return {
            "window_days": days,
            "total_invocation_errors": ovr.get("total_invocation_errors", 0),
            "total_throttles":         ovr.get("total_throttles", 0),
            "_note": "Tier A: aggregate only; per-model breakdown needs deployed pipeline.",
        }

    def latency_summary(self, *, days: int) -> dict:
        return {
            "window_days": days,
            "_note": "Tier A: latency percentiles aren't in CW Bedrock metrics. "
                     "Deploy the dashboard pipeline to get p50/p90/p99 by model.",
        }

    def ops_review_synthesize(self, *, days: int) -> dict:
        return {
            "window_days": days,
            "_note": "Tier A: LLM-synthesized exec brief requires the deployed "
                     "backend (it calls Bedrock with the dashboard's curated context). "
                     "Use the raw cost / quotas / model_insights tools and ask "
                     "Claude/Cursor to synthesize.",
        }


# ---------------------------------------------------------------------------
# Auto-detect
# ---------------------------------------------------------------------------
def auto_select() -> Backend:
    """Return the best backend for the current environment.

    BEDROCK_LENS_API set → HttpBackend (Tier B/C)
    Else                 → DirectBackend (Tier A)
    """
    client = api_client.from_env()
    if client is not None:
        return HttpBackend(client)
    return DirectBackend()
