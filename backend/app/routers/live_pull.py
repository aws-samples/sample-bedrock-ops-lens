"""On-demand per-minute CloudWatch pull for the Quota drill-down.

The stored data answers "what was the busiest minute each day" and "what did
each hour average". Neither shows the SHAPE of a burst. This endpoint reads the
last few hours at one-minute resolution, live from CloudWatch, for one selected
account, Region and model, so the drill-down can plot it against the quota line.

The read itself runs in a dedicated function under the ingester's role (see
ingestion/live_pull.py). This endpoint only validates the request, confirms Lens
already reports usage for that exact account/Region/model, resolves which
identifiers to combine and the burndown rate, and invokes that function.

  GET  /api/live-pull  — whether live pull is configured in this deployment
  POST /api/live-pull  — {account_id, region, model_id, hours, endpoint}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, rate_catalog

router = APIRouter()

ALLOWED_HOURS = (1, 3, 6, 12, 24)
MAX_IDENTIFIERS = 512  # Same request bound as ingestion.live_pull; reads are batched.
COOLDOWN_SECONDS = 15
_ACCOUNT = re.compile(r"[0-9]{12}")   # ASCII only: \d also matches other scripts' digits
_REGION = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")

# Per-container memo of recent successful pulls. A repeated press within the
# cooldown gets the same answer instead of another round of CloudWatch calls.
_recent: dict[tuple, tuple[float, dict]] = {}


class LivePullRequest(BaseModel):
    account_id: str
    region: str
    model_id: str
    hours: int = 3
    endpoint: str = "runtime"


def _function_name() -> str:
    return os.environ.get("LIVE_PULL_FUNCTION_NAME", "").strip()


def _validate(body: LivePullRequest) -> None:
    if not _ACCOUNT.fullmatch(body.account_id):
        raise HTTPException(400, "account_id must be a 12-digit AWS account id.")
    if not _REGION.fullmatch(body.region):
        raise HTTPException(400, "region is not a valid AWS Region code.")
    if not _IDENTIFIER.fullmatch(body.model_id):
        raise HTTPException(400, "model_id is not a valid model identifier.")
    if body.hours not in ALLOWED_HOURS:
        raise HTTPException(400, f"hours must be one of {list(ALLOWED_HOURS)}.")
    if body.endpoint != "runtime":
        # The pull reads the AWS/Bedrock namespace. bedrock-mantle publishes
        # different metrics under its own namespace and has no burndown, so
        # answering for it here would chart the wrong traffic.
        raise HTTPException(400, "Live pull covers the bedrock-runtime endpoint only.")


async def _identifiers(account: str, region: str, model: str) -> list[str]:
    """The model plus every application inference profile that resolves to it.

    Same grouping as the minute-peak collector, so the live chart and the stored
    daily peak describe the same traffic. A cross-Region id such as
    "us.anthropic..." is its own row with its own quota family, so it is never
    folded in here.
    """
    rows = await db.fetch(
        """SELECT profile_arn, profile_id FROM public.dim_inference_profiles
            WHERE accountid = $1 AND region = $2 AND model_id = $3
            ORDER BY profile_id""",
        account, region, model)
    out = [model]
    for r in db.rows_to_dicts(rows):
        for ident in (r.get("profile_arn"), r.get("profile_id")):
            if ident and ident not in out:
                out.append(ident)
    if len(out) > MAX_IDENTIFIERS:
        raise HTTPException(
            422,
            f"This model has {len(out)} identifiers; live pull supports at most "
            f"{MAX_IDENTIFIERS}. No partial model total was fetched.",
        )
    return out


def _invoke(function_name: str, payload: dict) -> dict:
    import boto3
    from botocore.config import Config
    client = boto3.client("lambda", config=Config(
        connect_timeout=5, read_timeout=75, retries={"total_max_attempts": 1}))
    resp = client.invoke(FunctionName=function_name, Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError") or not isinstance(body, dict):
        return {"ok": False, "error": "failed", "detail": "The live-pull function failed."}
    return body


@router.get("/live-pull")
async def live_pull_config():
    return {"enabled": bool(_function_name()), "hours": list(ALLOWED_HOURS),
            "cooldown_seconds": COOLDOWN_SECONDS}


@router.post("/live-pull")
async def live_pull(body: LivePullRequest):
    function_name = _function_name()
    if not function_name:
        raise HTTPException(501, "Live pull is not configured in this deployment.")
    _validate(body)

    # Only an account/Region/model Lens already reports on. This keeps the
    # endpoint from becoming a way to probe arbitrary accounts, and gives a clear
    # message instead of a CloudWatch error for a typo. 422, not 404: the
    # CloudFront distribution rewrites every 404 to the SPA's index.html.
    known = await db.fetchval(
        """SELECT 1 FROM f_daily
            WHERE accountId = $1 AND region = $2 AND modelId = $3
              AND endpoint = 'runtime' LIMIT 1""",
        body.account_id, body.region, body.model_id)
    if not known:
        raise HTTPException(422, "Lens has no Bedrock usage for this account, Region and model.")

    key = (body.account_id, body.region, body.model_id, body.hours)
    now = time.monotonic()
    for k in [k for k, (at, _) in _recent.items() if now - at >= COOLDOWN_SECONDS]:
        del _recent[k]
    if key in _recent:
        return {**_recent[key][1], "cached": True}

    identifiers = await _identifiers(body.account_id, body.region, body.model_id)
    catalog = await rate_catalog.snapshot()
    rate = catalog.rate_for(body.model_id)
    payload = {"account_id": body.account_id, "region": body.region,
               "model_id": body.model_id, "identifiers": identifiers,
               "hours": body.hours, "rate": rate.rate, "rate_source": rate.source}
    try:
        result = await asyncio.to_thread(_invoke, function_name, payload)
    except Exception:  # noqa: BLE001 - never leak a trace to the browser
        raise HTTPException(502, "Could not reach the live-pull function.") from None

    if not result.get("ok"):
        status = {"invalid_request": 400, "no_access": 403, "not_monitored": 403,
                  "throttled": 429}.get(result.get("error"), 502)
        raise HTTPException(status, result.get("detail") or "Live pull failed.")
    result["has_application_profile"] = len(identifiers) > 1
    result["cached"] = False
    _recent[key] = (time.monotonic(), result)
    return result
