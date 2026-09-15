#!/usr/bin/env python3
"""
Model lifecycle ingester for Bedrock Ops Lens.

Calls bedrock:ListFoundationModels in each monitored region and refreshes the
`dim_model_lifecycle` table with status (ACTIVE/LEGACY) plus the lifecycle
date fields the API returns:

    startOfLifeTime               first published on Bedrock
    legacyTime                    moved to Legacy state
    publicExtendedAccessTime      start of post-2026-02-01 extended-access phase
                                  (legacy-policy models only — see below)
    endOfLifeTime                 hard EOL — requests fail after this date

Same model can carry different dates in different regions, so the table is
keyed (modelId, region).

EOL RETENTION — why this is an UPSERT and not a full refresh
------------------------------------------------------------
After a model passes EOL, AWS removes it from all Regions: it disappears from
ListFoundationModels and GetFoundationModel raises ResourceNotFoundException.
This ingester used to DELETE the whole table and re-INSERT, which meant an
EOL'd model's row was destroyed on the very next run — so "past EOL" was a
state the dashboard could never report, and a customer still calling a dead
model saw only an unexplained 4xx spike with no model attribution.

So: UPSERT per row, then mark (not delete) any row the API stopped returning
in a region we successfully scraped. Reconciliation is scoped to regions that
actually answered — a throttled or unauthorised region must not be read as
"every model here is gone".

TWO LIFECYCLE POLICIES
----------------------
Models launched on Bedrock before 2026-09-07 follow the legacy policy; models
launched on or after it follow the current one. The difference matters for
migration planning:

    legacy policy   >=12 months on Bedrock before EOL, >=6 months in Legacy,
                    plus a public-extended-access phase (provider-set price
                    rises) for EOL dates after 2026-02-01.
    current policy  no 12-month floor, NO extended-access phase at all, and a
                    per-model Legacy period of either 6 months OR 45 days.

    https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html

The API has no policy field, but startOfLifeTime is returned for every model,
so the regime is derivable. Once a model is in Legacy the notice period it
actually gave is derivable too: endOfLifeTime - legacyTime.

Not derivable, and therefore not stored: the current policy's "EOL no sooner
than" date, and the declared Legacy period of a model that is still ACTIVE.
Both are model-card-only. This ingester does not hardcode lifecycle facts the
API doesn't serve.

The data is 100 % live from the AWS API. There is NO bundled JSON, NO scrape
of the docs page, NO hardcoded lifecycle dates. The only product opinion in
this codebase about lifecycle is the recommended-upgrade map in the
/api/model-lifecycle router, which is plain Python (reviewable, versioned).

Schedule alongside the quotas ingester (daily is plenty — lifecycle dates
move on the order of months, not minutes). Cross-account: lifecycle data is
identical across accounts in the same region, so we only need to call from
ONE session per region. We use the central account's session for simplicity.

Usage:
    python -m ingestion.model_lifecycle
    python -m ingestion.model_lifecycle --regions us-east-1,us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg
import boto3
from botocore.config import Config

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# Models launched on Bedrock on or after this instant follow the current model
# lifecycle policy; earlier ones follow the legacy policy. AWS states the cutoff
# as a date, so midnight UTC is the boundary.
POLICY_CUTOFF = datetime(2026, 9, 7, tzinfo=timezone.utc)

POLICY_CURRENT = "current"
POLICY_LEGACY = "legacy"


def classify_policy(start_of_life) -> str | None:
    """Which lifecycle policy governs a model, from its Bedrock launch date.

    Returns None when the API gave us no startOfLifeTime — the regime is then
    genuinely unknown and guessing it would be worse than showing nothing.
    """
    if start_of_life is None:
        return None
    return POLICY_CURRENT if start_of_life >= POLICY_CUTOFF else POLICY_LEGACY


def notice_period_days(legacy_time, end_of_life_time) -> int | None:
    """How many days of warning this model actually gave: EOL minus Legacy.

    NULL until the model enters Legacy (an ACTIVE model has no EOL date, and
    the current policy's declared Legacy period is model-card-only, so there is
    nothing to compute). ~184 means the legacy policy's "6 months"; ~45 means
    the current policy's short option.
    """
    if legacy_time is None or end_of_life_time is None:
        return None
    return (end_of_life_time - legacy_time).days


def _bedrock_client(region: str):
    return boto3.client(
        "bedrock",
        region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _fetch_lifecycle_for_region(region: str) -> list[tuple]:
    """Returns rows: (modelId, region, status, model_name, provider,
    start_of_life_time, legacy_time, public_extended_access_time,
    end_of_life_time, lifecycle_policy, notice_period_days).

    `list_foundation_models` returns the full `modelLifecycle` block — status
    AND the date fields — for every model, so one call per region covers the
    common case. We still follow up with `get_foundation_model` for a LEGACY
    model whose summary is missing the dates we need, since a LEGACY row with
    no EOL date can't be scored or timelined.
    """
    client = _bedrock_client(region)
    resp = client.list_foundation_models()
    out: list[tuple] = []
    for m in resp.get("modelSummaries", []) or []:
        lc_summary = m.get("modelLifecycle") or {}
        status = lc_summary.get("status")
        if not status:
            continue
        model_id = m.get("modelId")

        lc = lc_summary
        if status == "LEGACY" and not lc_summary.get("endOfLifeTime"):
            try:
                detail = client.get_foundation_model(modelIdentifier=model_id)
                lc = (detail.get("modelDetails") or {}).get("modelLifecycle") or lc_summary
            except Exception as e:
                # Don't fail the whole region for one model; record status
                # only and move on. The UI handles missing dates gracefully.
                print(f"    WARN [{region}/{model_id}] get_foundation_model failed: {e}")

        start_of_life = lc.get("startOfLifeTime")
        legacy_time = lc.get("legacyTime")
        eol_time = lc.get("endOfLifeTime")

        out.append((
            model_id,
            region,
            status,
            m.get("modelName"),
            m.get("providerName"),
            start_of_life,
            legacy_time,
            lc.get("publicExtendedAccessTime"),
            eol_time,
            classify_policy(start_of_life),
            notice_period_days(legacy_time, eol_time),
        ))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Refresh dim_model_lifecycle from bedrock:ListFoundationModels.",
    )
    ap.add_argument("--regions", default="",
                    help="comma-separated AWS regions; defaults to config.yaml monitored_regions")
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        try:
            from .config import load_config
            regions = load_config().resolved_regions()
        except Exception:
            regions = ["us-east-1"]

    conn = await asyncpg.connect(args.db_url)
    total = 0
    failures: list[tuple[str, str]] = []
    try:
        all_rows: list[tuple] = []
        # Only regions that actually answered may be reconciled. A region that
        # threw (throttle, no creds, opt-in not enabled) tells us nothing about
        # whether its models still exist, and treating silence as "all gone"
        # would retire the whole region's catalogue.
        scraped_regions: list[str] = []
        for region in regions:
            try:
                rows = _fetch_lifecycle_for_region(region)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"  [{region}] ERROR — {msg}", flush=True)
                failures.append((region, msg))
                continue
            scraped_regions.append(region)
            print(f"  [{region}] {len(rows)} models "
                  f"({sum(1 for r in rows if r[2] == 'LEGACY')} legacy)")
            all_rows.extend(rows)

        if all_rows:
            # UPSERT, never DELETE — a model that has passed EOL is GONE from
            # the API, and dropping its row would erase the one record that
            # tells the customer they are still calling a dead model.
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO dim_model_lifecycle (
                        modelId, region, status, model_name, provider,
                        start_of_life_time, legacy_time,
                        public_extended_access_time, end_of_life_time,
                        lifecycle_policy, notice_period_days,
                        api_visible, last_seen_at, refreshed_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                              TRUE, now(), now())
                    ON CONFLICT (modelId, region) DO UPDATE SET
                        status                      = EXCLUDED.status,
                        model_name                  = EXCLUDED.model_name,
                        provider                    = EXCLUDED.provider,
                        start_of_life_time          = EXCLUDED.start_of_life_time,
                        legacy_time                 = EXCLUDED.legacy_time,
                        public_extended_access_time = EXCLUDED.public_extended_access_time,
                        end_of_life_time            = EXCLUDED.end_of_life_time,
                        lifecycle_policy            = EXCLUDED.lifecycle_policy,
                        notice_period_days          = EXCLUDED.notice_period_days,
                        api_visible                 = TRUE,
                        last_seen_at                = now(),
                        refreshed_at                = now()
                    """,
                    all_rows,
                )

                # Retire rows the API stopped returning, scoped to regions that
                # answered this run. Retained, not deleted: end_of_life_time is
                # already on the row, so the dashboard can keep reporting
                # "past EOL" long after AWS has removed the model.
                seen_keys = [(r[0], r[1]) for r in all_rows]
                retired = await conn.fetchval(
                    """
                    WITH seen AS (
                        SELECT * FROM unnest($2::text[], $3::text[])
                             AS t(modelId, region)
                    ), retired AS (
                        UPDATE dim_model_lifecycle d
                           SET api_visible = FALSE
                         WHERE d.region = ANY($1::text[])
                           AND d.api_visible
                           AND NOT EXISTS (
                               SELECT 1 FROM seen s
                                WHERE s.modelId = d.modelId
                                  AND s.region  = d.region
                           )
                        RETURNING 1
                    )
                    SELECT count(*) FROM retired
                    """,
                    scraped_regions,
                    [k[0] for k in seen_keys],
                    [k[1] for k in seen_keys],
                )
            total = len(all_rows)
            if retired:
                print(f"  retired {retired} (model, region) row(s) no longer "
                      f"returned by the API — kept for past-EOL reporting")

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_model_lifecycle_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )
        if failures:
            print(f"\nDONE with {len(failures)} failure(s); {total} rows ingested.")
            for region, msg in failures:
                print(f"  [{region}] {msg}")
            return 1
        print(f"DONE. {total} (model, region) lifecycle rows.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
