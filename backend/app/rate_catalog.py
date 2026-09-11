"""Runtime-editable output-token burndown rate catalog.

Why this exists
---------------
`burndown.py` hardcodes the multipliers from the AWS doc. That is correct today
and wrong the moment AWS publishes a new SKU or changes a rate: fixing it means
editing Python, rebuilding the container image, and redeploying. A dashboard
whose numbers silently understate quota burn by 10x until someone ships code is
not operable.

So the rates become data. The catalog lives in one JSON row of `ingestion_meta`
(key `burndown_rate_catalog`), is edited from Settings by an admin, and is picked
up by the backend and the ingester/findings jobs within `_TTL_SECONDS` — no
restart, no redeploy.

Deliberate design choices
-------------------------
* **The native metric comes first, everywhere.** AWS's own
  `EstimatedTPMQuotaUsage` already has burndown applied, so where a datapoint
  exists no multiplier is needed at all. This catalog only covers what the native
  metric cannot: hours with no datapoint, per-workload proxy attribution (a
  model-level CloudWatch aggregate has no workload dimension), and showing the
  user an explicit "output tokens burn at 10x" policy statement.

* **Effective dates, not retroactive rewrites.** An entry applies from
  `effective_from` onward. Re-rating history with today's multiplier would
  silently change last month's reported utilization, so a rate edit made today
  must not move a chart drawn for July. Entries with no `effective_from` apply to
  all dates (that is how the seeded bundled values behave).

* **"Verified on" is not "effective from".** The doc-verification date is a fact
  about our review process; the policy start date is a fact about AWS. Conflating
  them invents history, so they are separate fields and `verified_on` is never
  used for date selection.

* **An unverified fallback is labelled as such.** When nothing in the catalog
  covers a model we fall back to the bundled table and say so
  (`source="bundled_default"`), rather than presenting a guess as confirmed AWS
  policy. The UI can then withhold or caveat it.

* **Arithmetic stays separate from configuration.** `burndown.output_burndown_rate`
  remains a pure synchronous function with no I/O. This module is the only thing
  that touches the database, so unit tests and the hot path are unaffected.

* **Snapshot per request/job, not per model.** A batch resolving 500 models must
  not issue 500 queries. `snapshot()` returns a cached, immutable view; the
  revision travels into response-cache keys so a rate edit cannot serve stale
  arithmetic from the HTTP cache.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from . import db
from .burndown import output_burndown_rate

META_KEY = "burndown_rate_catalog"

# How long a loaded snapshot is reused before the next read. Short enough that an
# admin's edit shows up without a restart, long enough that a dashboard refresh
# storm does not hammer `ingestion_meta`.
_TTL_SECONDS = 60

DOC_URL = ("https://docs.aws.amazon.com/bedrock/latest/userguide/"
           "quotas-token-burndown.html")

# The bundled, doc-verified starting point. `match` is compared against a
# separator-free copy of the model id and the public name, exactly like
# burndown.py, so "gpt-5-6-sol" and "GPT-5.6 Sol" both hit. `all_of` means every
# token must be present.
BUNDLED_ENTRIES: list[dict] = [
    {"id": "claude-opus-4-8", "label": "Anthropic Claude Opus 4.8",
     "all_of": ["opus48"], "rate": 15, "endpoint": "runtime"},
    {"id": "claude-sonnet-5", "label": "Anthropic Claude Sonnet 5",
     "all_of": ["claude", "sonnet5"], "rate": 10, "endpoint": "runtime"},
    {"id": "claude-opus-5", "label": "Anthropic Claude Opus 5",
     "all_of": ["claude", "opus5"], "rate": 10, "endpoint": "runtime"},
    {"id": "claude-fable-5-1", "label": "Anthropic Claude Fable 5.1",
     "all_of": ["claude", "fable51"], "rate": 10, "endpoint": "runtime"},
    {"id": "gpt-5-6-sol", "label": "OpenAI GPT-5.6 Sol",
     "all_of": ["gpt56sol"], "rate": 10, "endpoint": "runtime"},
    {"id": "gpt-5-6-terra", "label": "OpenAI GPT-5.6 Terra",
     "all_of": ["gpt56terra"], "rate": 10, "endpoint": "runtime"},
    {"id": "gpt-5-6-luna", "label": "OpenAI GPT-5.6 Luna",
     "all_of": ["gpt56luna"], "rate": 10, "endpoint": "runtime"},
]

VALID_ENDPOINTS = ("runtime", "mantle", "any")


def _squash(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


@dataclass(frozen=True)
class RateResolution:
    """One resolved multiplier, with enough provenance to render honestly."""
    rate: int
    source: str                      # catalog | bundled_default | endpoint_rule
    entry_id: str | None = None
    label: str | None = None
    verified_on: str | None = None
    source_url: str | None = None
    verified: bool = False           # False => do not present as AWS policy

    def as_dict(self) -> dict:
        return {"rate": self.rate, "source": self.source,
                "entry_id": self.entry_id, "label": self.label,
                "verified_on": self.verified_on, "source_url": self.source_url,
                "verified": self.verified}


@dataclass(frozen=True)
class Catalog:
    """An immutable snapshot. `revision` is safe to put in a cache key."""
    entries: tuple[dict, ...] = ()
    revision: str = "bundled"
    updated_at: str | None = None
    loaded_at: float = 0.0
    stale: bool = False              # served from the last good snapshot
    seeded: bool = False             # nothing stored yet; bundled values in use

    def rate_for(self, model_id: str | None, public_name: str | None = None,
                 on_date: date | None = None,
                 is_mantle: bool = False) -> RateResolution:
        """Resolve the multiplier for one model on one date.

        Mantle short-circuits to 1 before anything else: the doc is explicit that
        models on the bedrock-mantle endpoint have separate input/output quotas,
        so a burndown multiplier there would be a fabrication regardless of what
        the catalog says.
        """
        if is_mantle:
            return RateResolution(rate=1, source="endpoint_rule",
                                  label="bedrock-mantle: separate input/output "
                                        "quotas, no burndown",
                                  verified=True, source_url=DOC_URL)
        hay = _squash(model_id) + "|" + _squash(public_name)
        when = on_date or datetime.now(timezone.utc).date()

        best: dict | None = None
        best_from: date | None = None
        for e in self.entries:
            if e.get("endpoint") not in (None, "any", "runtime"):
                continue
            toks = e.get("all_of") or []
            if not toks or not all(_squash(t) in hay for t in toks):
                continue
            eff = _parse_date(e.get("effective_from"))
            if eff is not None and eff > when:
                continue          # not yet in force for the observed date
            # Most recent applicable effective date wins; a specific date beats
            # an open-ended entry so a scheduled change supersedes the default.
            if best is None or _is_later(eff, best_from):
                best, best_from = e, eff

        if best is not None:
            return RateResolution(
                rate=int(best["rate"]), source="catalog",
                entry_id=best.get("id"), label=best.get("label"),
                verified_on=best.get("verified_on"),
                source_url=best.get("source_url") or DOC_URL,
                verified=bool(best.get("verified_on")))

        # Nothing matched: fall back to the bundled arithmetic. This covers the
        # generic Claude<=4.7 -> 5x rule and the 1:1 default, neither of which is
        # a per-SKU catalog entry.
        return RateResolution(
            rate=output_burndown_rate(model_id, public_name, is_mantle=False),
            source="bundled_default", source_url=DOC_URL, verified=False,
            label="No catalog entry; bundled default applied")

    def unmapped(self, model_ids) -> list[str]:
        """Models with traffic that no catalog entry covers and whose bundled
        rate is not 1 — i.e. where the number shown depends on an unverified
        assumption. Surfacing these is how a new SKU gets noticed."""
        out = []
        for mid in model_ids:
            r = self.rate_for(mid)
            if r.source == "bundled_default" and r.rate != 1:
                out.append(mid)
        return sorted(set(out))


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _is_later(a: date | None, b: date | None) -> bool:
    """Ordering where None (open-ended) is the earliest possible start."""
    if a is None:
        return False
    if b is None:
        return True
    return a > b


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
_cache: Catalog | None = None
_last_good: Catalog | None = None


def _bundled_catalog(seeded: bool = True) -> Catalog:
    return Catalog(entries=tuple(BUNDLED_ENTRIES), revision="bundled",
                   loaded_at=time.time(), seeded=seeded)


def _revision_of(payload: dict, updated_at: str | None) -> str:
    """Cheap content revision for cache keys — the stored counter when present,
    else the update timestamp."""
    rev = payload.get("revision")
    return str(rev) if rev is not None else (updated_at or "0")


async def snapshot(force: bool = False) -> Catalog:
    """Return a cached catalog, reloading at most once per `_TTL_SECONDS`.

    A read failure returns the LAST GOOD snapshot marked `stale=True` rather than
    raising or silently reverting to bundled values: a transient database blip
    must not change every quota number on the page.
    """
    global _cache, _last_good
    now = time.time()
    if not force and _cache is not None and (now - _cache.loaded_at) < _TTL_SECONDS:
        return _cache

    try:
        row = await db.fetchrow(
            "SELECT value, updated_at FROM ingestion_meta WHERE key = $1", META_KEY)
    except Exception:
        if _last_good is not None:
            _cache = Catalog(**{**_last_good.__dict__, "stale": True,
                                "loaded_at": now})
            return _cache
        _cache = _bundled_catalog()
        return _cache

    if not row:
        _cache = _last_good = _bundled_catalog()
        return _cache

    try:
        payload = json.loads(row["value"])
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
        updated = row["updated_at"]
        _cache = _last_good = Catalog(
            entries=tuple(entries),
            revision=_revision_of(payload, updated.isoformat() if updated else None),
            updated_at=updated.isoformat() if updated else None,
            loaded_at=now)
    except (TypeError, ValueError, KeyError):
        # Corrupt stored JSON: prefer the last good snapshot, else bundled.
        _cache = _last_good or _bundled_catalog()
        _cache = Catalog(**{**_cache.__dict__, "stale": True, "loaded_at": now})
    return _cache


def reset_cache() -> None:
    """Drop the memo so the next `snapshot()` re-reads. Called after a save."""
    global _cache
    _cache = None


def _parse_stored(value: str | None, updated_at: Any) -> Catalog:
    """Build a Catalog from a raw stored value. Shared by both load paths."""
    if not value:
        return _bundled_catalog()
    try:
        payload = json.loads(value)
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
    except (TypeError, ValueError):
        return Catalog(**{**_bundled_catalog().__dict__, "stale": True})
    iso = updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
    return Catalog(entries=tuple(entries), revision=_revision_of(payload, iso),
                   updated_at=iso, loaded_at=time.time())


async def snapshot_with_conn(conn) -> Catalog:
    """Load the catalog over an EXPLICIT asyncpg connection.

    The ingester and the scheduled findings job run in a different Lambda from
    the API and never initialise the backend's connection pool, so `snapshot()`
    would fail there. They already hold a connection, so they pass it in. Not
    memoized: a job is short-lived and reads once per batch by construction.
    """
    try:
        row = await conn.fetchrow(
            "SELECT value, updated_at FROM ingestion_meta WHERE key = $1", META_KEY)
    except Exception:
        return _bundled_catalog()
    if not row:
        return _bundled_catalog()
    return _parse_stored(row["value"], row["updated_at"])


# --------------------------------------------------------------------------- #
# Validation + save
# --------------------------------------------------------------------------- #
class CatalogError(ValueError):
    """Rejected catalog input, with a message safe to show the admin."""


def validate(entries: Any) -> list[dict]:
    """Normalize and reject bad input. Returns the cleaned entry list."""
    if not isinstance(entries, list):
        raise CatalogError("entries must be a list")
    if len(entries) > 500:
        raise CatalogError("too many entries (max 500)")

    seen: set[tuple] = set()
    clean: list[dict] = []
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise CatalogError(f"entry {i} must be an object")
        toks = raw.get("all_of")
        if isinstance(toks, str):
            toks = [toks]
        if not isinstance(toks, list) or not toks:
            raise CatalogError(f"entry {i}: all_of must be a non-empty list")
        toks = [str(t).strip() for t in toks if str(t).strip()]
        if not toks:
            raise CatalogError(f"entry {i}: all_of must contain a token")
        try:
            rate = int(raw["rate"])
        except (KeyError, TypeError, ValueError):
            raise CatalogError(f"entry {i}: rate must be an integer")
        if not (1 <= rate <= 1000):
            raise CatalogError(f"entry {i}: rate must be between 1 and 1000")
        endpoint = raw.get("endpoint") or "runtime"
        if endpoint not in VALID_ENDPOINTS:
            raise CatalogError(
                f"entry {i}: endpoint must be one of {', '.join(VALID_ENDPOINTS)}")
        eff = raw.get("effective_from") or None
        if eff is not None and _parse_date(eff) is None:
            raise CatalogError(f"entry {i}: effective_from must be YYYY-MM-DD")
        ver = raw.get("verified_on") or None
        if ver is not None and _parse_date(ver) is None:
            raise CatalogError(f"entry {i}: verified_on must be YYYY-MM-DD")

        # Two entries matching the same tokens, endpoint and effective date are a
        # genuine conflict: which rate applies would depend on list order.
        key = (tuple(sorted(_squash(t) for t in toks)), endpoint, eff)
        if key in seen:
            raise CatalogError(
                f"entry {i}: duplicate match for {toks} / {endpoint} / "
                f"{eff or 'always'} — two rates would both apply")
        seen.add(key)

        clean.append({
            "id": str(raw.get("id") or "-".join(toks)).strip()[:80],
            "label": str(raw.get("label") or "").strip()[:200] or None,
            "all_of": toks,
            "rate": rate,
            "endpoint": endpoint,
            "effective_from": eff,
            "verified_on": ver,
            "source_url": str(raw.get("source_url") or "").strip()[:400] or None,
        })
    return clean


async def save(entries: Any) -> Catalog:
    """Validate and store atomically, bumping the revision.

    The whole catalog is one row, so the UPDATE is the atomic unit — there is no
    window where half the rates are new.
    """
    clean = validate(entries)
    prev = await snapshot(force=True)
    try:
        next_rev = int(prev.revision) + 1
    except (TypeError, ValueError):
        next_rev = 1
    payload = json.dumps({"revision": next_rev, "entries": clean})
    await db.fetchval(
        """
        INSERT INTO ingestion_meta (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value, updated_at = now()
        RETURNING key
        """, META_KEY, payload)
    reset_cache()
    return await snapshot(force=True)


def _bundled_with_provenance() -> list[dict]:
    """Bundled entries stamped with today's verification date and the doc URL.

    `effective_from` is left unset deliberately: we do not know the date AWS's
    policy actually began, and inventing one would let a "restore" silently
    re-rate historical charts.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    return [{**e, "verified_on": today, "source_url": DOC_URL}
            for e in BUNDLED_ENTRIES]


async def restore_bundled() -> Catalog:
    """Overwrite the stored catalog with the bundled values. Always writes."""
    return await save(_bundled_with_provenance())


async def seed_if_absent() -> Catalog:
    """Write the bundled entries on first run so the Settings form opens with the
    real, doc-verified values instead of an empty table the admin must retype."""
    existing = await db.fetchval(
        "SELECT value FROM ingestion_meta WHERE key = $1", META_KEY)
    if existing:
        return await snapshot()
    return await save(_bundled_with_provenance())
