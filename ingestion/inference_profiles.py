"""Cache application inference profiles per monitored account and source Region.

Run: python -m ingestion.inference_profiles --regions us-east-1 --days 14
Facts retain the invoked ARN/ID. lens_read views apply this cache at read time,
including to history and profiles that have subsequently been deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone

import asyncpg
from botocore.config import Config
from botocore.exceptions import ClientError

from .accounts import _add_common_args, discover_accounts, session_for
from .config import load_config

_PROFILE_ARN = re.compile(
    r"^arn:(aws[a-z-]*):bedrock:([a-z0-9-]+):([0-9]{12}):"
    r"(?:application-inference-profile|inference-profile)/([A-Za-z0-9][A-Za-z0-9_.:-]*)$"
)
_MODEL_ARN = re.compile(
    r"^arn:aws[a-z-]*:bedrock:([a-z0-9-]+)::foundation-model/([^/]+)$"
)
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def profile_record(profile: dict, account: str, region: str) -> tuple:
    """Validate ownership and resolve one model, without guessing a CRIS ID."""
    arn = profile.get("inferenceProfileArn", "")
    match = _PROFILE_ARN.fullmatch(arn)
    pid = profile.get("inferenceProfileId")
    if (profile.get("type") != "APPLICATION" or not match
            or match[2] != region or match[3] != account or match[4] != pid):
        raise ValueError("application inference profile has an invalid owner or identifier")
    arns = sorted({m.get("modelArn", "") for m in profile.get("models", [])})
    matches = [_MODEL_ARN.fullmatch(a) for a in arns]
    ids = {m[2] for m in matches if m}
    regions = sorted({m[1] for m in matches if m})
    model_id = next(iter(ids)) if arns and all(matches) and len(ids) == 1 else None
    return (account, region, pid, arn, profile.get("inferenceProfileName") or pid,
            model_id, arns, regions)


def is_profile_reference(identifier: str, account: str, region: str) -> bool:
    match = _PROFILE_ARN.fullmatch(identifier)
    if match:
        return match[2] == region and match[3] == account
    return bool(_OPAQUE_ID.fullmatch(identifier))


def read_profiles(client, account: str, region: str,
                  observed: set[str]) -> tuple[list[tuple], list[str]]:
    """Read ALL list pages; Get only observed profiles absent from that list.

    List failure raises before any cache write. Get failures leave the original
    identifier and any last-known mapping intact; they never imply zero usage.
    """
    records: dict[str, tuple] = {}
    aliases: set[str] = set()
    request = {"typeEquals": "APPLICATION", "maxResults": 1000}
    tokens: set[str] = set()
    while True:
        page = client.list_inference_profiles(**request)
        for profile in page["inferenceProfileSummaries"]:
            row = profile_record(profile, account, region)
            records[row[2]] = row
            aliases.update((row[2], row[3]))
        token = page.get("nextToken")
        if not token:
            break
        if token in tokens:
            raise ValueError("ListInferenceProfiles repeated a pagination token")
        tokens.add(token)
        request["nextToken"] = token

    warnings: list[str] = []
    attempted: set[str] = set()
    for identifier in sorted(observed - aliases):
        if identifier in aliases or not is_profile_reference(identifier, account, region):
            continue
        # A log can contain both the ARN and ID; don't retry the same missing
        # profile twice in this refresh, including when Get returns 404.
        pid = identifier.rsplit("/", 1)[-1]
        if pid in attempted:
            continue
        attempted.add(pid)
        try:
            profile = client.get_inference_profile(inferenceProfileIdentifier=identifier)
            row = profile_record(profile, account, region)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            if code != "ResourceNotFoundException":
                warnings.append(f"GetInferenceProfile: {code}")
            continue
        records[row[2]] = row
        aliases.update((row[2], row[3]))
    return list(records.values()), warnings


async def refresh_region(conn, client, account: str, region: str, days: int = 14) -> dict:
    observed = await conn.fetch(
        """
        SELECT DISTINCT modelid FROM (
          SELECT modelid FROM public.f_daily
           WHERE accountid=$1 AND region=$2 AND endpoint='runtime'
             AND event_date >= current_date - $3::int
          UNION ALL
          SELECT modelid FROM public.f_daily_tagged
           WHERE accountid=$1 AND region=$2
             AND event_date >= current_date - $3::int
          UNION ALL
          SELECT modelid FROM public.f_proxy_dim_hourly
           WHERE accountid=$1 AND region=$2 AND endpoint='runtime'
             AND dim_key='__all__' AND event_date >= current_date - $3::int
        ) observed
        """, account, region, days,
    )
    rows, warnings = read_profiles(client, account, region,
                                   {r["modelid"] for r in observed})
    cached = await conn.fetch(
        """SELECT profile_id, profile_arn, model_id FROM public.dim_inference_profiles
           WHERE accountid=$1 AND region=$2""", account, region)
    mappings = {r["profile_id"]: (r["profile_arn"], r["model_id"]) for r in cached}
    mappings.update({r[2]: (r[3], r[5]) for r in rows})
    resolved_aliases = {alias for pid, (arn, model) in mappings.items() if model
                        for alias in (pid, arn)}
    unresolved_observed = {
        r["modelid"] for r in observed
        if is_profile_reference(r["modelid"], account, region)
        and r["modelid"] not in resolved_aliases
    }
    # No delete/reinsert: a deleted profile's mapping is still needed by history.
    # Visibility is updated only after a COMPLETE, validated list traversal.
    async with conn.transaction():
        await conn.execute(
            """UPDATE public.dim_inference_profiles SET api_visible=FALSE
               WHERE accountid=$1 AND region=$2""", account, region)
        if rows:
            await conn.executemany(
                """
                INSERT INTO public.dim_inference_profiles
                  (accountid, region, profile_id, profile_arn, profile_name,
                   model_id, model_arns, destination_regions)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (accountid, region, profile_id) DO UPDATE SET
                  profile_arn=EXCLUDED.profile_arn,
                  profile_name=EXCLUDED.profile_name,
                  model_id=EXCLUDED.model_id,
                  model_arns=EXCLUDED.model_arns,
                  destination_regions=EXCLUDED.destination_regions,
                  last_seen_at=now(), api_visible=TRUE
                """, rows)
    return {"profiles": len(rows), "unresolved": sum(r[5] is None for r in rows),
            "unresolved_observed": len(unresolved_observed),
            "warnings": sorted(set(warnings))}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    _add_common_args(ap)
    ap.add_argument("--regions", default="")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens"))
    args = ap.parse_args()
    accounts = discover_accounts(args)
    regions = ([r.strip() for r in args.regions.split(",") if r.strip()]
               if args.regions else load_config().resolved_regions())
    if not accounts:
        return 2
    failures = 0
    conn = await asyncpg.connect(args.db_url)
    try:
        for monitored in accounts:
            account = monitored.accountId
            try:
                session = session_for(account, role_name=args.role_name,
                                      external_id=args.external_id)
            except Exception as exc:
                print(f"[inference_profiles/{account}] {type(exc).__name__}: {exc}")
                failures += 1
                continue
            for region in regions:
                try:
                    client = session.client("bedrock", region_name=region, config=Config(
                        connect_timeout=5, read_timeout=15,
                        retries={"mode": "standard", "max_attempts": 3}))
                    result = await refresh_region(conn, client, account, region, args.days)
                    print(f"[inference_profiles/{account}/{region}] {result}")
                    failures += bool(result["warnings"] or result["unresolved"]
                                     or result["unresolved_observed"])
                except Exception as exc:
                    print(f"[inference_profiles/{account}/{region}] "
                          f"cached mappings retained: {type(exc).__name__}: {exc}")
                    failures += 1
        await conn.execute(
            """INSERT INTO ingestion_meta (key, value, updated_at)
               VALUES ('last_inference_profiles_refresh', $1, now()),
                      ('inference_profiles_refresh_failures', $2, now())
               ON CONFLICT (key) DO UPDATE
               SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at""",
            datetime.now(timezone.utc).isoformat(), str(failures))
    finally:
        await conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
