"""Application profile identities and their cached model resolution."""
from fastapi import APIRouter, Query

from .. import db

router = APIRouter()


@router.get("/inference-profiles")
async def inference_profiles(
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Includes retained profiles; api_visible=false is not proof of deletion."""
    rows = await db.fetch(
        """
        SELECT accountid AS "accountId", region, profile_id, profile_arn,
               profile_name, model_id AS "modelId", model_arns,
               destination_regions, api_visible, last_seen_at,
               model_id IS NOT NULL AS resolved,
               FALSE AS quota_routing_known
        FROM public.dim_inference_profiles
        WHERE ($1::text[] IS NULL OR accountid = ANY($1))
          AND ($2 = 'all' OR region = $2)
        ORDER BY accountid, region, profile_id
        LIMIT $3 OFFSET $4
        """, accounts, region, limit + 1, offset,
    )
    return {
        "profiles": db.rows_to_dicts(rows[:limit]),
        "next_offset": offset + limit if len(rows) > limit else None,
    }
