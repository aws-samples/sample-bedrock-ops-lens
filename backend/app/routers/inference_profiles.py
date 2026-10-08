"""Application profile identities, their cached model resolution, and usage."""
from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

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


@router.get("/inference-profile-usage")
async def inference_profile_usage(f: FilterSet = Depends(parse_filters)):
    """One row per application inference profile, with the model it resolved to
    and the usage that ran through it.

    This is the single table that answers "which profile is this, what model does
    it actually run on, and how much is it using?" — the question a customer
    invoking through profiles asks first, and which otherwise required reading
    opaque 12-character ids across several tabs.

    Grain is (account, Region, profile ARN), combining the ARN and short ID.
    Profile names are not unique. An identifier with no cached mapping keeps its
    own id, reports resolved=false and a null model: Lens does not guess a model
    it cannot verify, and an unpriced model stays unpriced.
    """
    # Alias the filter columns: dim_inference_profiles also has accountid and
    # region, so unqualified predicates would be ambiguous across the join.
    w = build_where(f, table_alias="d")
    rows = await db.fetch(
        f"""
        SELECT
          -- ONE row per profile. A caller may pass the full ARN in some code
          -- paths and the bare 12-character id in others; both resolve to the
          -- same profile, so grouping on the ARN collapses them. Grouping on the
          -- invoked identifier would have shown the same profile twice and split
          -- its usage. Unresolved identifiers have no ARN, so they group on
          -- themselves and stay separate.
          COALESCE(d.application_profile_arn, d.invoked_model_id) AS profile_key,
          MAX(d.application_profile_name)                 AS application_profile_name,
          d.application_profile_arn,
          ARRAY_AGG(DISTINCT d.invoked_model_id ORDER BY d.invoked_model_id)
                                                         AS invoked_identifiers,
          MAX(p.model_id)                                AS modelId,
          MAX(p.model_id) IS NOT NULL                    AS resolved,
          d.accountId,
          d.region,
          SUM(d.total_requests)::BIGINT                   AS total_requests,
          SUM(d.failed_requests)::BIGINT                  AS failed_requests,
          SUM(d.status_429_count)::BIGINT                 AS throttled_requests,
          SUM(d.total_input_tokens)::BIGINT               AS total_input_tokens,
          SUM(d.total_output_tokens)::BIGINT              AS total_output_tokens,
          SUM(d.total_cache_read_input_tokens)::BIGINT    AS cache_read_tokens,
          MIN(d.event_date)                               AS first_seen,
          MAX(d.event_date)                               AS last_seen,
          MAX(p.api_visible::int)::boolean                AS api_visible,
          MAX(cardinality(p.destination_regions))         AS destination_region_count
        FROM f_daily d
        LEFT JOIN public.dim_inference_profiles p
               ON p.accountid = d.accountId AND p.region = d.region
              AND p.profile_arn = d.application_profile_arn
        WHERE {w.sql} AND d.has_application_profile
        GROUP BY COALESCE(d.application_profile_arn, d.invoked_model_id),
                 d.application_profile_arn, d.accountId, d.region
        HAVING SUM(d.total_requests) > 0
        ORDER BY total_requests DESC
        LIMIT 500
        """,
        *w.params,
    )
    out = db.rows_to_dicts(rows)
    for r in out:
        for key in ("first_seen", "last_seen"):
            if r.get(key) is not None:
                r[key] = str(r[key])
        r["multi_region"] = bool((r.get("destination_region_count") or 0) > 1)
        # How many identifier forms the callers actually used for this profile.
        r["invoked_form_count"] = len(r.get("invoked_identifiers") or [])
    return out
