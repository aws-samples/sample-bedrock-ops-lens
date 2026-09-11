"""SQL filter builder — turns the dashboard's standard filter set
(`days`/`start`/`end`/`provider`/`region`/`accounts`/`traffic_type`/`tag_filter`)
into composable WHERE-clause fragments + asyncpg-positional bind parameters.

This replaces the reference's `pf()`/`dual_query`/`_mirror_filter` mess.
Single source of truth, used by every endpoint.
"""
from __future__ import annotations

import re
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

# Inference-profile (CRIS) geo prefixes that may precede the provider segment in
# a stored modelId: "us.anthropic.claude-...". A predicate of
# `modelId LIKE 'anthropic.%'` misses every one of those rows, so a fleet of a
# million prefixed Anthropic requests could filter down to zero (finding 13).
CRIS_GEO_PREFIXES = ("us", "eu", "apac", "us-gov", "jp", "au", "ca", "amer", "global")
_GEO_STRIP_RE = "^(" + "|".join(CRIS_GEO_PREFIXES) + r")\."

# A provider is any leading dotted segment of a modelId. The dashboard derives
# its dropdown from the DATA (/distinct-filters), so a hardcoded allowlist was
# narrower than the UI and any provider missing from it (openai, deepseek,
# qwen, ...) was silently ignored — returning the WHOLE fleet instead of that
# provider's slice. Validate the SHAPE instead, and keep the map for labels.
_SAFE_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Region shape check: real AWS regions keep appearing (ap-south-2 was missing
# from the old allowlist and therefore silently unfiltered).
_SAFE_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

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
    # Filters whose value could not be honoured: [(field, value), ...]. Any
    # entry makes build_where() produce an empty population rather than a
    # silently broadened one, and endpoints surface it to the UI.
    invalid: tuple[tuple[str, str], ...] = ()


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

    # UNSUPPORTED VALUES ARE NOT SILENTLY DROPPED (finding 13). Widening the
    # scope to "all" made an unsatisfiable filter look like an unfiltered
    # answer: `?provider=openai` returned all 23,204,626 requests. Anything
    # that fails validation is recorded in `invalid` and build_where() makes the
    # population EMPTY, so the UI shows "no data for this selection" instead of
    # the whole fleet.
    invalid: list[tuple[str, str]] = []

    if region != "all" and region not in ALLOWED_REGIONS \
            and not _SAFE_REGION_RE.match(region):
        invalid.append(("region", region))

    if provider != "all" and not _SAFE_PROVIDER_RE.match(provider):
        invalid.append(("provider", provider))

    if traffic_type not in TRAFFIC_TYPE_MAP and traffic_type != "all":
        invalid.append(("traffic_type", traffic_type))

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
        invalid=tuple(invalid),
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
    has_model: bool = True,
) -> Where:
    """Build a WHERE-clause fragment from the FilterSet.

    `has_traffic_type`, `has_account`, `has_endpoint`, `has_model` let you
    turn off filters when the target table doesn't have those columns (e.g.,
    f_daily_cost has no `endpoint` column — Cost Explorer is endpoint-
    agnostic, so passing `has_endpoint=False` makes the cost router
    immune to UI endpoint switches). `has_model=False` is for tables with
    no modelId column (f_daily_guardrails, f_daily_agentcore) — otherwise
    the global provider filter generates SQL against a missing column and
    the route 500s, which the UI shows as stale data.
    """
    a = (table_alias + ".") if table_alias else ""
    parts: list[str] = []
    params: list[Any] = []

    # Date range — required.
    parts.append(f"{a}event_date BETWEEN ${len(params)+1}::date AND ${len(params)+2}::date")
    params.extend([f.start, f.end])

    # An unhonourable filter must not widen the result set.
    if f.invalid:
        parts.append("FALSE")

    if has_model and f.provider != "all":
        # Strip any inference-profile geo prefix before matching the provider
        # segment, so "us.anthropic.claude-..." counts as anthropic.
        parts.append(
            f"regexp_replace({a}modelId, '{_GEO_STRIP_RE}', '') LIKE ${len(params)+1}")
        params.append(PROVIDER_PREFIX.get(f.provider, f.provider + ".") + "%")

    if f.region != "all":
        parts.append(f"{a}region = ${len(params)+1}")
        params.append(f.region)

    if has_account and f.accounts:
        parts.append(f"{a}accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))

    if has_traffic_type and f.traffic_type != "all":
        # .get(): an invalid value is already recorded in f.invalid (which adds
        # FALSE above); don't KeyError on the way out.
        tts = TRAFFIC_TYPE_MAP.get(f.traffic_type, ())
        if not tts:
            tts = ()
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
