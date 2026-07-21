"""SQL filter builder — turns the dashboard's standard filter set
(`days`/`start`/`end`/`provider`/`region`/`accounts`/`traffic_type`/`tag_filter`)
into composable WHERE-clause fragments + asyncpg-positional bind parameters.

This replaces the reference's `pf()`/`dual_query`/`_mirror_filter` mess.
Single source of truth, used by every endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import Query


# ---------------------------------------------------------------------------
# Hard allow-lists. Anything else gets ignored (defense against SQL injection
# even though we use parameterized queries — these values are sometimes
# interpolated into LIKE patterns or column references).
# ---------------------------------------------------------------------------
ALLOWED_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2",
    "ap-south-1", "ca-central-1", "sa-east-1",
}

PROVIDER_PREFIX = {
    "anthropic": "anthropic.",
    "amazon":    "amazon.",
    "meta":      "meta.",
    "cohere":    "cohere.",
    "mistral":   "mistral.",
    "ai21":      "ai21.",
    "stability": "stability.",
}

TRAFFIC_TYPE_MAP = {
    "regional_cris": ("CROSS_REGION_OD_INFERENCE_REQUEST",),
    "source_cris":   ("SOURCE_REGION_OD_INFERENCE_REQUEST",),
    "global_cris":   ("CROSS_REGION_OD_INFERENCE_REQUEST", "SOURCE_REGION_OD_INFERENCE_REQUEST"),
    "cris":          ("CROSS_REGION_OD_INFERENCE_REQUEST", "SOURCE_REGION_OD_INFERENCE_REQUEST"),
    "on_demand":     ("ON_DEMAND_INFERENCE_REQUEST",),
    "provisioned":   ("PROVISIONED_THROUGHPUT_V1",),
}


@dataclass
class FilterSet:
    """Parsed, validated filter inputs. Pass through to `build_where()` to
    get SQL fragments."""
    start: date
    end: date
    provider: str = "all"
    region: str = "all"
    accounts: tuple[str, ...] = ()
    traffic_type: str = "all"
    # Tag filter: list of (tag_key, tag_value) AND-d across keys, OR-d within a key.
    # Frontend serializes as ?tag_filter=team:platform,team:ml&tag_filter=env:prod
    tag_filter: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Bedrock endpoint slice: 'runtime' (AWS/Bedrock CW namespace, the
    # bedrock-runtime API), 'mantle' (AWS/BedrockMantle, bedrock-mantle
    # endpoint), or 'all' to sum across both. Defaults to 'all'.
    endpoint: str = "all"


def parse_filters(
    days: int = Query(7, ge=1, le=365),
    start: str | None = Query(None, description="YYYY-MM-DD"),
    end: str | None = Query(None, description="YYYY-MM-DD"),
    provider: str = Query("all"),
    region: str = Query("all"),
    accounts: str | None = Query(None, description="comma-separated 12-digit IDs"),
    traffic_type: str = Query("all"),
    tag_filter: list[str] | None = Query(None, description="key:value, repeatable"),
    endpoint: str = Query("all", description="bedrock-runtime / bedrock-mantle / all"),
) -> FilterSet:
    """FastAPI dependency — pass as a function param to inherit all filters."""
    today = date.today()
    if start and end:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    else:
        end_d = today
        start_d = today - timedelta(days=days - 1)

    if region not in ALLOWED_REGIONS and region != "all":
        region = "all"  # silently drop invalid

    if provider not in PROVIDER_PREFIX and provider != "all":
        provider = "all"

    if traffic_type not in TRAFFIC_TYPE_MAP and traffic_type != "all":
        traffic_type = "all"

    # Endpoint allowlist. Drop unknown values rather than 400-ing — keeps
    # the dashboard tolerant of stale URLs.
    if endpoint not in ("runtime", "mantle", "all"):
        endpoint = "all"

    accounts_tuple: tuple[str, ...] = ()
    if accounts:
        accounts_tuple = tuple(
            a.strip() for a in accounts.split(",")
            if a.strip().isdigit() and len(a.strip()) == 12
        )

    # tag_filter: collapse repeats per key
    tag_grouped: dict[str, list[str]] = {}
    for entry in (tag_filter or []):
        if ":" not in entry:
            continue
        k, v = entry.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        tag_grouped.setdefault(k, []).append(v)
    tag_tuple = tuple((k, tuple(vs)) for k, vs in tag_grouped.items())

    return FilterSet(
        start=start_d,
        end=end_d,
        provider=provider,
        region=region,
        accounts=accounts_tuple,
        traffic_type=traffic_type,
        tag_filter=tag_tuple,
        endpoint=endpoint,
    )


@dataclass
class Where:
    """Result of build_where(): SQL fragment (joined with AND) + positional bind params."""
    sql: str
    params: list[Any] = field(default_factory=list)


def build_where(
    f: FilterSet,
    *,
    table_alias: str = "",
    has_traffic_type: bool = True,
    has_account: bool = True,
    has_endpoint: bool = True,
) -> Where:
    """Build a WHERE-clause fragment from the FilterSet.

    `has_traffic_type`, `has_account`, `has_endpoint` let you turn off
    filters when the target table doesn't have those columns (e.g.,
    f_daily_cost has no `endpoint` column — Cost Explorer is endpoint-
    agnostic, so passing `has_endpoint=False` makes the cost router
    immune to UI endpoint switches).
    """
    a = (table_alias + ".") if table_alias else ""
    parts: list[str] = []
    params: list[Any] = []

    # Date range — required.
    parts.append(f"{a}event_date BETWEEN ${len(params)+1}::date AND ${len(params)+2}::date")
    params.extend([f.start, f.end])

    if f.provider != "all":
        parts.append(f"{a}modelId LIKE ${len(params)+1}")
        params.append(PROVIDER_PREFIX[f.provider] + "%")

    if f.region != "all":
        parts.append(f"{a}region = ${len(params)+1}")
        params.append(f.region)

    if has_account and f.accounts:
        parts.append(f"{a}accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))

    if has_traffic_type and f.traffic_type != "all":
        tts = TRAFFIC_TYPE_MAP[f.traffic_type]
        parts.append(f"{a}traffic_type = ANY(${len(params)+1}::text[])")
        params.append(list(tts))

    if has_endpoint and f.endpoint != "all":
        parts.append(f"{a}endpoint = ${len(params)+1}")
        params.append(f.endpoint)

    return Where(sql=" AND ".join(parts), params=params)


def append_account_filter(w: Where, accounts: tuple[str, ...] | None,
                           table_alias: str = "") -> Where:
    """Add an account filter to an existing WHERE — used when a query already
    has a base WHERE and we want to overlay an extra account constraint
    (e.g., from authz)."""
    if not accounts:
        return w
    a = (table_alias + ".") if table_alias else ""
    new_sql = (w.sql + " AND " if w.sql else "") + f"{a}accountId = ANY(${len(w.params)+1}::text[])"
    return Where(sql=new_sql, params=w.params + [list(accounts)])
