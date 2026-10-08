"""On-demand per-minute CloudWatch read for one account, Region and model.

Invoked by the backend when a user presses "Pull live data" in the Quota
drill-down, so they can see minute-by-minute quota usage instead of the stored
daily peak and hourly averages.

WHY A SEPARATE FUNCTION. Monitored accounts' reader roles trust only the
ingester's role. This handler runs under that role, so no monitored account needs
a trust-policy change, but in its own function with its own entry point: the
internet-facing backend is granted permission to invoke THIS function and
nothing else, never the ingester and its maintenance actions.

SAME ACCOUNTS AS INGESTION. setup-pipeline.sh (or an operator) sets the account
scope, reader-role name and external ID on the ingester function in place. Each
pull reads those settings from the ingester's live configuration, refuses any
account outside that scope before an STS call, and assumes the reader role
exactly as ingestion does.

It needs no database and no VPC. The backend resolves which identifiers to
combine (the model plus any application inference profiles that resolve to it)
and the burndown rate from the configured catalog, and passes both in. Every
field is validated here again before any AWS call.

Event:
    {"account_id": "123456789012", "region": "us-east-1",
     "model_id": "anthropic.claude-sonnet-4-5-20250929-v1:0",
     "identifiers": ["anthropic.claude-...", "a1b2c3d4e5f6"],
     "hours": 3, "rate": 5, "rate_source": "bundled_default"}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .accounts import DEFAULT_ROLE_NAME, discover_from_list, discover_from_org, new_session_cache
from .config import MonitoredAccountsConfig, load_config
from .cw_minute_peak import combine_minutes, fetch_minutes

ALLOWED_HOURS = (1, 3, 6, 12, 24)
MAX_IDENTIFIERS = 512
IDENTIFIERS_PER_BATCH = 12
_ACCOUNT = re.compile(r"[0-9]{12}")   # ASCII only: \d also matches other scripts' digits
_REGION = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]")
# Foundation-model ids, cross-Region ids, profile ARNs and bare profile ids.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")

_NO_ACCESS = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
              "InvalidClientTokenId", "ExpiredToken", "NoSuchEntity"}
_THROTTLED = {"Throttling", "ThrottlingException", "TooManyRequestsException",
              "RequestLimitExceeded", "LimitExceeded"}

# The ingester settings that decide which accounts it reads and how it assumes
# their reader roles.
_INGESTER_KEYS = ("MONITORED_ACCOUNTS_MODE", "MONITORED_ACCOUNTS_IDS",
                  "BEDROCK_OPS_LENS_ROLE_NAME", "BEDROCK_OPS_LENS_EXTERNAL_ID")
ORG_CACHE_SECONDS = 300
_org_cache: tuple[float, frozenset[str]] | None = None


class NotMonitored(Exception):
    """The account is outside the ingester's account scope."""


class ScopeUnavailable(Exception):
    """The ingester's account scope could not be determined."""


def validate(event: dict) -> dict:
    """Return a normalised request or raise ValueError with a safe message."""
    if not isinstance(event, dict):
        raise ValueError("request must be an object")
    account = str(event.get("account_id", ""))
    region = str(event.get("region", ""))
    model = str(event.get("model_id", ""))
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("account_id must be a 12-digit AWS account id")
    if not _REGION.fullmatch(region):
        raise ValueError("region is not a valid AWS Region code")
    if not _IDENTIFIER.fullmatch(model):
        raise ValueError("model_id is not a valid model identifier")
    try:
        hours = int(event.get("hours", 3))
    except (TypeError, ValueError):
        raise ValueError("hours must be an integer") from None
    if hours not in ALLOWED_HOURS:
        raise ValueError(f"hours must be one of {ALLOWED_HOURS}")
    identifiers = event.get("identifiers") or [model]
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError("identifiers must be a non-empty list")
    clean: list[str] = []
    for ident in identifiers:
        ident = str(ident)
        if not _IDENTIFIER.fullmatch(ident):
            raise ValueError("an identifier is not valid")
        if ident not in clean:
            clean.append(ident)
    if model not in clean:
        clean.insert(0, model)
    if len(clean) > MAX_IDENTIFIERS:
        raise ValueError(f"at most {MAX_IDENTIFIERS} identifiers may be combined")
    try:
        rate = int(event.get("rate", 1))
    except (TypeError, ValueError):
        raise ValueError("rate must be an integer") from None
    if not 1 <= rate <= 100:
        raise ValueError("rate must be between 1 and 100")
    return {"account_id": account, "region": region, "model_id": model,
            "identifiers": clean, "hours": hours, "rate": rate,
            "rate_source": str(event.get("rate_source") or "")[:64]}


def ingester_settings(client=None) -> dict[str, str]:
    """The ingester function's current account-scope and reader-role settings."""
    name = os.environ.get("INGESTER_FUNCTION_NAME", "").strip()
    if not name:
        raise RuntimeError("INGESTER_FUNCTION_NAME is not set")
    client = client or boto3.client("lambda", config=Config(
        connect_timeout=5, read_timeout=10, retries={"mode": "adaptive", "max_attempts": 3}))
    config = client.get_function_configuration(FunctionName=name)
    env = (config.get("Environment") or {}).get("Variables") or {}
    return {k: str(env.get(k) or "").strip() for k in _INGESTER_KEYS}


def _org_account_ids() -> frozenset[str]:
    global _org_cache
    now = time.monotonic()
    if _org_cache is None or now - _org_cache[0] >= ORG_CACHE_SECONDS:
        _org_cache = (now, frozenset(a.accountId for a in discover_from_org()))
    return _org_cache[1]


def is_monitored(account: str, settings: dict[str, str], self_account: str) -> bool:
    """Whether the ingester's account scope includes `account`.

    Same precedence as accounts.discover_accounts in the ingester: its
    environment overrides config.yaml (baked into this same image); explicit
    mode without ids, single mode, and a failed Organizations listing all mean
    the central account only.
    """
    try:
        configured = load_config().monitored_accounts
    except Exception:  # noqa: BLE001 - the ingester falls back the same way
        configured = MonitoredAccountsConfig()
    mode = settings.get("MONITORED_ACCOUNTS_MODE") or configured.mode
    csv = settings.get("MONITORED_ACCOUNTS_IDS")
    ids = {a.accountId for a in discover_from_list(csv)} if csv else set(configured.ids)
    if mode == "explicit" and ids:
        return account in ids
    if mode == "discover-org":
        try:
            return account in _org_account_ids()
        except Exception:  # noqa: BLE001 - the ingester falls back the same way
            pass
    return account == self_account


def open_session(account: str):
    """A session in `account` opened exactly as the ingester would open it.

    Never served from the process-wide cache: in a warm container a cached
    assumed-role session would outlive its credentials."""
    try:
        settings = ingester_settings()
        sessions = new_session_cache()
        monitored = is_monitored(account, settings, sessions.self_account)
    except Exception as exc:  # noqa: BLE001 - reported as a safe message
        raise ScopeUnavailable(type(exc).__name__) from None
    if not monitored:
        raise NotMonitored(account)
    return sessions.session_for(
        account, settings.get("BEDROCK_OPS_LENS_ROLE_NAME") or DEFAULT_ROLE_NAME,
        settings.get("BEDROCK_OPS_LENS_EXTERNAL_ID") or "")


def _peak(minutes: list[dict], field: str):
    best = None
    for m in minutes:
        v = m.get(field)
        if v is not None and (best is None or v > best[0]):
            best = (v, m["minute"])
    return {"value": best[0], "at": best[1].isoformat()} if best else None


def _read_minutes(cw, identifiers: list[str], start, end, rate):
    """Bound memory to one CloudWatch batch plus the combined minute series.

    Combine the batches at each timestamp, never by adding their maxima. A
    missing measurement in an active batch remains unknown in the combined row.
    """
    combined: dict[datetime, dict] = {}
    incomplete, observed = [], []
    fields = ("requests", "input_tokens", "output_tokens", "quota_tpm")
    for offset in range(0, len(identifiers), IDENTIFIERS_PER_BATCH):
        batch = identifiers[offset:offset + IDENTIFIERS_PER_BATCH]
        series, missing = fetch_minutes(cw, batch, start, end)
        incomplete.extend(missing)
        observed.extend(i for i in batch if any(series.get(i, {}).values()))
        for row in combine_minutes(batch, series, rate):
            stamp = row["minute"]
            if stamp not in combined:
                combined[stamp] = dict(row)
                continue
            total = combined[stamp]
            for field in fields:
                total[field] = (total[field] + row[field]
                                if total[field] is not None and row[field] is not None
                                else None)
            sources = {total["quota_source"], row["quota_source"]} - {"unavailable"}
            total["quota_source"] = ("mixed" if len(sources) > 1 else
                                     next(iter(sources)) if sources else "unavailable")
    return [combined[t] for t in sorted(combined)], incomplete, observed


def pull(req: dict, now: datetime | None = None, opener=None) -> dict:
    """Read and combine per-minute metrics. Returns a JSON-safe dict."""
    clock = now or datetime.now(timezone.utc)
    # The window is [start, end): the open minute is still accumulating and
    # would show a falsely low last point, so it is excluded.
    end = clock.replace(second=0, microsecond=0)
    start = end - timedelta(hours=req["hours"])
    try:
        session = (opener or open_session)(req["account_id"])
        cw = session.client("cloudwatch", region_name=req["region"], config=Config(
            connect_timeout=5, read_timeout=20, retries={"mode": "adaptive", "max_attempts": 3}))
        minutes, incomplete, observed = _read_minutes(
            cw, req["identifiers"], start, end, req["rate"])
    except NotMonitored:
        return {"ok": False, "error": "not_monitored",
                "detail": (f"Account {req['account_id']} is not one of the accounts this "
                           "Lens deployment monitors.")}
    except ScopeUnavailable as exc:
        return {"ok": False, "error": "failed",
                "detail": f"Live pull could not determine the monitored accounts ({exc})."}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        if code in _NO_ACCESS:
            return {"ok": False, "error": "no_access",
                    "detail": (f"Lens cannot read CloudWatch in account {req['account_id']} "
                               f"({req['region']}). Deploy the reader role there, or check "
                               "that it allows cloudwatch:GetMetricData.")}
        if code in _THROTTLED:
            return {"ok": False, "error": "throttled",
                    "detail": "CloudWatch is throttling requests. Try again in a minute."}
        return {"ok": False, "error": "failed", "detail": f"CloudWatch request failed ({code})."}
    except Exception as exc:  # noqa: BLE001 - surface a safe message, never a trace
        return {"ok": False, "error": "failed",
                "detail": f"Live pull failed ({type(exc).__name__})."}

    fields = ("quota_tpm", "requests", "input_tokens", "output_tokens")
    return {
        "ok": True,
        "status": "partial" if incomplete else "complete",
        "incomplete_series": incomplete[:10],
        "account_id": req["account_id"], "region": req["region"], "model_id": req["model_id"],
        "pulled_at": clock.isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "hours": req["hours"]},
        "identifiers": req["identifiers"], "identifiers_with_data": observed,
        "burndown_rate": req["rate"], "burndown_rate_source": req["rate_source"],
        "active_minutes": len(minutes),
        # Minutes with activity whose value could not be established. Any
        # non-zero count makes that peak a lower bound.
        "unknown_minutes": {f: sum(1 for m in minutes if m[f] is None) for f in fields},
        "quota_sources": {s: sum(1 for m in minutes
                                 if m["quota_tpm"] is not None and m["quota_source"] == s)
                          for s in ("aws_estimate", "reconstructed", "mixed")},
        "peak": {"quota_tpm": _peak(minutes, "quota_tpm"), "rpm": _peak(minutes, "requests"),
                 "input_tokens": _peak(minutes, "input_tokens"),
                 "output_tokens": _peak(minutes, "output_tokens")},
        "minutes": [dict(m, minute=m["minute"].isoformat()) for m in minutes],
    }


def handler(event, context):  # noqa: ARG001 - Lambda signature
    try:
        req = validate(event)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_request", "detail": str(exc)}
    return pull(req)
