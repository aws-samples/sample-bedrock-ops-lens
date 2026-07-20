"""Governance: reconciliation of OBSERVED usage against the DECLARED
application registry (db/registry.yaml — the AI Act-style referential).

Statuses:
  compliant        observed (app, model) matches a declaration
  model_drift      declared app, model not in its allowed_models
  undeclared       observed app with no declaration (shadow AI)
  declared_unused  declaration with zero observed usage in window
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import asyncpg
import yaml
from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, parse_filters

router = APIRouter()

_REGISTRY_PATHS = [
    Path(os.environ.get("REGISTRY_PATH", "")),
    Path("/app/db/registry.yaml"),
    Path(__file__).resolve().parents[3] / "db" / "registry.yaml",
]


def _load_registry() -> list[dict]:
    for p in _REGISTRY_PATHS:
        try:
            if p and p.is_file():
                data = yaml.safe_load(p.read_text()) or {}
                return data.get("applications", []) or []
        except Exception:
            continue
    return []


async def _observed(f: FilterSet) -> list[dict]:
    """Observed usage grouped by (app identity, model). Tolerant to the
    table not existing yet (pre-first-ingest stacks)."""
    try:
        rows = await db.fetch(
            """
            SELECT COALESCE(principal_group, principal_arn) AS app,
                   modelId,
                   SUM(invocations)::BIGINT   AS invocations,
                   SUM(input_tokens + output_tokens)::BIGINT AS tokens
            FROM f_daily_by_identity
            WHERE event_date BETWEEN $1::date AND $2::date
            GROUP BY 1, 2
            """,
            f.start, f.end,
        )
        return db.rows_to_dicts(rows)
    except asyncpg.exceptions.UndefinedTableError:
        return []
    except Exception:
        return []


def _match_app(app: str, registry: list[dict]) -> dict | None:
    for entry in registry:
        rid = str(entry.get("id", ""))
        if rid and (rid == app or rid in app or fnmatch.fnmatch(app, f"*{rid}*")):
            return entry
    return None


def _model_allowed(model_id: str, entry: dict) -> bool:
    pats = entry.get("allowed_models") or []
    return any(fnmatch.fnmatch(model_id or "", p) for p in pats)


@router.get("/governance/registry")
async def governance_registry():
    """The declared referential, as loaded (for the UI registry panel)."""
    return _load_registry()


@router.get("/governance/policy/{app_id}")
async def governance_policy(app_id: str):
    """OPT-IN enforcement rendering. The governance model is detective by
    default (observe + flag, no a-priori blocking, minimal human control)
    to avoid making the registry an access bottleneck. For entries the
    org classifies as high-risk (AI Act), this endpoint renders the IAM
    identity policy (+ SCP statement example) enforcing allowed_models —
    proportionality: freedom by default, enforcement where risk justifies
    it."""
    entry = next((e for e in _load_registry() if str(e.get("id")) == app_id), None)
    if entry is None:
        return {"error": f"app '{app_id}' not in registry"}
    resources = []
    for pat in entry.get("allowed_models") or []:
        core = pat.strip("*")
        resources += [
            f"arn:aws:bedrock:*::foundation-model/*{core}*",
            f"arn:aws:bedrock:*:*:inference-profile/*{core}*",
        ]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowDeclaredModelsOnly",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": resources or ["*"],
            },
        ],
    }
    scp_hint = {
        "Sid": "DenyUndeclaredBedrockModels",
        "Effect": "Deny",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "NotResource": resources or ["*"],
        "Condition": {"ArnLike": {"aws:PrincipalArn": f"arn:aws:iam::*:role/{app_id}*"}},
    }
    return {"app": app_id, "identity_policy": policy, "scp_statement_example": scp_hint}


@router.get("/governance/reconciliation")
async def governance_reconciliation(f: FilterSet = Depends(parse_filters)):
    registry = _load_registry()
    observed = await _observed(f)

    rows: list[dict] = []
    used_ids: set[str] = set()

    for o in observed:
        app, model = o["app"], o.get("modelid") or o.get("modelId") or ""
        entry = _match_app(app or "", registry)
        if entry is None:
            status = "undeclared"
        else:
            used_ids.add(str(entry.get("id")))
            status = "compliant" if _model_allowed(model, entry) else "model_drift"
        rows.append({
            "app": app,
            "modelId": model,
            "invocations": o.get("invocations", 0),
            "tokens": o.get("tokens", 0),
            "status": status,
            "declared_name": (entry or {}).get("name"),
            "ai_act_risk": (entry or {}).get("ai_act_risk"),
            "use_case": (entry or {}).get("use_case"),
        })

    for entry in registry:
        if str(entry.get("id")) not in used_ids:
            rows.append({
                "app": entry.get("id"),
                "modelId": None,
                "invocations": 0,
                "tokens": 0,
                "status": "declared_unused",
                "declared_name": entry.get("name"),
                "ai_act_risk": entry.get("ai_act_risk"),
                "use_case": entry.get("use_case"),
            })

    order = {"undeclared": 0, "model_drift": 1, "declared_unused": 2, "compliant": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["invocations"] or 0)))

    summary = {
        "declared_apps": len(registry),
        "observed_apps": len({r["app"] for r in rows if r["invocations"]}),
        "undeclared": sum(1 for r in rows if r["status"] == "undeclared"),
        "model_drift": sum(1 for r in rows if r["status"] == "model_drift"),
        "declared_unused": sum(1 for r in rows if r["status"] == "declared_unused"),
    }
    return {"summary": summary, "rows": rows}
