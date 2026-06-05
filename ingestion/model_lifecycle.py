#!/usr/bin/env python3
"""
Model lifecycle ingester for Bedrock Ops Lens.

Calls bedrock:ListFoundationModels in each monitored region and refreshes the
`dim_model_lifecycle` table with status (ACTIVE/LEGACY) plus the lifecycle
date fields the API returns:

    startOfLifeTime               first published on Bedrock
    legacyTime                    moved to Legacy state
    publicExtendedAccessTime      start of post-2026-02-01 extended-access phase
    endOfLifeTime                 hard EOL — requests fail after this date

Same model can carry different dates in different regions, so the table is
keyed (modelId, region). One row per (modelId, region) — full refresh per
run; the table is small (~hundreds of rows) so we DELETE+INSERT for clarity.

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


def _bedrock_client(region: str):
    return boto3.client(
        "bedrock",
        region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _fetch_lifecycle_for_region(region: str) -> list[tuple]:
    """Returns rows: (modelId, region, status, model_name, provider,
    start_of_life_time, legacy_time, public_extended_access_time,
    end_of_life_time).

    `list_foundation_models` only returns `modelLifecycle.status` (no dates),
    so we follow up with `get_foundation_model` for every LEGACY model to
    pick up legacyTime / endOfLifeTime / publicExtendedAccessTime /
    startOfLifeTime. ACTIVE models don't need the per-model call — only the
    status matters for them and the table already records that.
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
        if status == "LEGACY":
            try:
                detail = client.get_foundation_model(modelIdentifier=model_id)
                lc = (detail.get("modelDetails") or {}).get("modelLifecycle") or lc_summary
            except Exception as e:
                # Don't fail the whole region for one model; record status
                # only and move on. The UI handles missing dates gracefully.
                print(f"    WARN [{region}/{model_id}] get_foundation_model failed: {e}")

        out.append((
            model_id,
            region,
            status,
            m.get("modelName"),
            m.get("providerName"),
            lc.get("startOfLifeTime"),
            lc.get("legacyTime"),
            lc.get("publicExtendedAccessTime"),
            lc.get("endOfLifeTime"),
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
        for region in regions:
            try:
                rows = _fetch_lifecycle_for_region(region)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"  [{region}] ERROR — {msg}", flush=True)
                failures.append((region, msg))
                continue
            print(f"  [{region}] {len(rows)} models "
                  f"({sum(1 for r in rows if r[2] == 'LEGACY')} legacy)")
            all_rows.extend(rows)

        if all_rows:
            # Full refresh — drop + insert. Table is small and the source of
            # truth is always the API, so reconciling deltas isn't worth it.
            async with conn.transaction():
                await conn.execute("DELETE FROM dim_model_lifecycle")
                await conn.executemany(
                    """
                    INSERT INTO dim_model_lifecycle (
                        modelId, region, status, model_name, provider,
                        start_of_life_time, legacy_time,
                        public_extended_access_time, end_of_life_time,
                        refreshed_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())
                    """,
                    all_rows,
                )
            total = len(all_rows)

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
