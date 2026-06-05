"""Tag-attribution endpoints — populate the dynamic tag-key/value top-bar dropdowns.

These replace the reference's customer-portfolio dropdown (`/my-customers`,
`/customer-search`). Tag data comes from Bedrock per-request metadata
(`InvokeModel`/`Converse` `requestMetadata` field).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from .. import db
from ..auth import current_user_id

router = APIRouter()


@router.get("/tags")
async def list_tag_keys(min_requests: int = Query(0, ge=0)):
    """All tag_keys seen in dim_tags, ordered by 30-day request volume DESC.

    `min_requests` filters out low-volume keys (e.g., set to 1000 to hide
    one-off experiment tags)."""
    rows = await db.fetch(
        """
        SELECT tag_key, COUNT(*)::BIGINT AS distinct_values,
               SUM(total_requests_30d)::BIGINT AS total_requests_30d
        FROM dim_tags
        WHERE tag_key <> '__none__'
        GROUP BY tag_key
        HAVING SUM(total_requests_30d) >= $1
        ORDER BY total_requests_30d DESC
        """,
        min_requests,
    )
    return db.rows_to_dicts(rows)


@router.get("/tags/{key}/values")
async def list_tag_values(
    key: str = Path(..., min_length=1, max_length=256),
    q: str | None = Query(None, description="prefix typeahead"),
    limit: int = Query(50, ge=1, le=500),
):
    if q:
        rows = await db.fetch(
            """
            SELECT tag_value, total_requests_30d::BIGINT,
                   first_seen, last_seen
            FROM dim_tags
            WHERE tag_key = $1 AND tag_value ILIKE $2 || '%'
            ORDER BY total_requests_30d DESC
            LIMIT $3
            """,
            key, q, limit,
        )
    else:
        rows = await db.fetch(
            """
            SELECT tag_value, total_requests_30d::BIGINT,
                   first_seen, last_seen
            FROM dim_tags
            WHERE tag_key = $1
            ORDER BY total_requests_30d DESC
            LIMIT $2
            """,
            key, limit,
        )
    return db.rows_to_dicts(rows)


@router.get("/preferences")
async def get_preferences(request: Request):
    """Returns the calling user's pinned tag keys + dashboard defaults."""
    user_id = current_user_id(request)
    row = await db.fetchrow(
        "SELECT pinned_tag_keys, default_time_range, default_provider, updated_at "
        "FROM user_preferences WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return {
            "user_id": user_id,
            "pinned_tag_keys": [],
            "default_time_range": None,
            "default_provider": None,
        }
    d = dict(row)
    d["user_id"] = user_id
    d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
    return d


@router.put("/preferences")
async def put_preferences(request: Request, body: dict):
    """Upsert pinned tag keys + defaults for the calling user."""
    user_id = current_user_id(request)
    pinned = body.get("pinned_tag_keys", [])
    if not isinstance(pinned, list) or any(not isinstance(k, str) for k in pinned):
        raise HTTPException(400, detail="pinned_tag_keys must be list[str]")
    if len(pinned) > 10:
        raise HTTPException(400, detail="max 10 pinned tag keys")
    default_time_range = body.get("default_time_range")
    default_provider = body.get("default_provider")

    await db.fetchval(
        """
        INSERT INTO user_preferences (user_id, pinned_tag_keys, default_time_range, default_provider, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (user_id) DO UPDATE
          SET pinned_tag_keys = EXCLUDED.pinned_tag_keys,
              default_time_range = EXCLUDED.default_time_range,
              default_provider = EXCLUDED.default_provider,
              updated_at = now()
        RETURNING user_id
        """,
        user_id, pinned, default_time_range, default_provider,
    )
    return {"ok": True, "user_id": user_id}
