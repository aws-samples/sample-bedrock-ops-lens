"""Admin API for the output-token burndown rate catalog.

  GET  /api/burndown-rates          — catalog + provenance + unmapped models
  PUT  /api/burndown-rates          — replace the catalog (admin only)
  POST /api/burndown-rates/seed     — write the bundled doc values (admin only)

Editing a rate changes arithmetic that the response cache has already computed,
so a successful save invalidates the cache. Without that, the dashboard would
keep serving pre-edit numbers for the whole TTL and the admin would reasonably
conclude the save did nothing.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import cache, db, rate_catalog
from ..auth import is_admin

router = APIRouter()


@router.get("/burndown-rates")
async def get_rates(request: Request):
    """The catalog as stored, plus what an operator needs to maintain it:
    which models currently have traffic that no entry covers, and how stale the
    verification is."""
    cat = await rate_catalog.snapshot()

    # Models with recent traffic whose multiplier rests on an unverified bundled
    # assumption. This is the "AWS shipped a new SKU and nobody noticed" alarm.
    unmapped: list[str] = []
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT modelId
            FROM f_hourly_peak
            WHERE event_date >= current_date - 30
              AND endpoint = 'runtime' AND total_requests > 0
            """)
        unmapped = cat.unmapped(r["modelid"] for r in rows)
    except Exception:
        # A missing table (fresh stack, pre-ingest) must not 500 the settings page.
        unmapped = []

    return {
        "entries": list(cat.entries),
        "revision": cat.revision,
        "updated_at": cat.updated_at,
        "seeded_from_bundled": cat.seeded,
        "stale": cat.stale,
        "doc_url": rate_catalog.DOC_URL,
        "unmapped_models": unmapped,
        "is_admin": is_admin(request),
        # The native metric is preferred wherever it exists, so state plainly that
        # this catalog is a fallback and not the primary source of truth.
        "note": ("AWS EstimatedTPMQuotaUsage is used when a datapoint exists. "
                 "These rates cover hours with no datapoint, per-workload proxy "
                 "attribution, and the displayed burndown policy."),
    }


@router.put("/burndown-rates")
async def put_rates(request: Request, body: dict):
    if not is_admin(request):
        raise HTTPException(403, "admin access required")
    try:
        cat = await rate_catalog.save(body.get("entries"))
    except rate_catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    # Rates changed => every cached quota/burndown response is now wrong.
    cache.invalidate_all()
    return {"ok": True, "revision": cat.revision, "updated_at": cat.updated_at,
            "entries": list(cat.entries)}


@router.post("/burndown-rates/seed")
async def seed_rates(request: Request):
    """RESTORE the bundled, doc-verified values, overwriting whatever is stored.

    This is the undo button for a bad edit, so it must overwrite unconditionally.
    An earlier version delegated to `seed_if_absent()`, which is a no-op once any
    catalog exists — so "Restore AWS defaults" silently returned the very edited
    catalog the operator was trying to discard, and reported success.

    First-boot seeding is a different job and stays in `seed_if_absent()`, called
    from the app lifespan, precisely so a restart never clobbers real edits.
    """
    if not is_admin(request):
        raise HTTPException(403, "admin access required")
    cat = await rate_catalog.restore_bundled()
    cache.invalidate_all()
    return {"ok": True, "revision": cat.revision, "entries": list(cat.entries)}
