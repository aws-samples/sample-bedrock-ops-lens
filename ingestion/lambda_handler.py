"""Lambda entrypoint for the orchestrated ingester.

Runs in two contexts:
  * EventBridge daily schedule  (event = {"source": "aws.events", ...})
  * Manual `aws lambda invoke`  (event = {"only": ["cw_metrics", ...]} or {})

Order matters: lifecycle first (cheap dim table), then volumetric
(cw_metrics), then cost, then tag-attributed (invocation_logs), and
quotas LAST. quotas is the slowest module at org/multi-region scale
(Service Quotas API rate-limits), so it runs last to avoid starving the
primary-data modules of the 15-min Lambda budget. Each step is isolated —
one failing or timed-out module does NOT abort the rest.

After everything finishes the cache_generation key in Memcached is bumped so
the dashboard sees fresh data on the next request.

The same Lambda container image is shared with the backend; only the CMD
differs at deploy time.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback


# ------------------------------------------------------------------ helpers
def _bump_cache_generation() -> None:
    """Bump cache_generation in Memcached so all stale entries become invalid.

    Best-effort. Never raises — a failure here just means users see stale
    data for one TTL window, not a broken dashboard.
    """
    host = os.environ.get("MEMCACHED_HOST", "").strip()
    if not host:
        return
    port = int(os.environ.get("MEMCACHED_PORT", "11211") or "11211")
    try:
        from pymemcache.client.base import Client  # type: ignore
        c = Client((host, port), connect_timeout=2, timeout=2)
        try:
            c.incr("bedrock_lens:cache_generation", 1)
        except Exception:
            c.set("bedrock_lens:cache_generation", b"1", expire=0)
        print(f"[ingester] bumped cache_generation on {host}")
    except Exception as e:
        print(f"[ingester] cache bump failed (ignored): {e}")


async def _run_module(name: str, coro_factory) -> dict:
    """Run one ingester's main() coroutine; capture timing + result."""
    t0 = time.monotonic()
    print(f"\n========== [{name}] starting ==========")
    try:
        rc = await coro_factory()
        elapsed = time.monotonic() - t0
        print(f"========== [{name}] done in {elapsed:.1f}s rc={rc} ==========")
        return {"module": name, "rc": rc, "elapsed_s": round(elapsed, 1)}
    except SystemExit as e:
        # argparse calls sys.exit; treat as failure but don't abort run.
        elapsed = time.monotonic() - t0
        print(f"========== [{name}] SystemExit({e.code}) after {elapsed:.1f}s ==========")
        return {"module": name, "rc": e.code or 0, "elapsed_s": round(elapsed, 1)}
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"========== [{name}] FAILED after {elapsed:.1f}s ==========")
        traceback.print_exc()
        return {"module": name, "rc": "error", "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(elapsed, 1)}


# Each module uses argparse on sys.argv. We swap sys.argv per call so each
# ingester sees the args it expects (mostly defaults + the days window).
def _set_argv(argv: list[str]) -> None:
    sys.argv = argv


async def _orchestrate(only: list[str] | None, days: int) -> dict:
    """Run ingesters in order. `only` filters to a subset by module name."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        # Compose from DB_* env vars + Secrets Manager.
        # In the Lambda image the package layout is `app/...` (LAMBDA_TASK_ROOT
        # is on PYTHONPATH); locally it's `backend/app/...`. Try both.
        try:
            from app.config import _compose_database_url  # type: ignore
        except ImportError:
            from backend.app.config import _compose_database_url  # type: ignore
        db_url = _compose_database_url() or ""
    if not db_url:
        raise RuntimeError("ingester needs DATABASE_URL or DB_HOST/DB_USER/DB_SECRET_ARN")
    # Self-account ingest uses the Lambda execution role's own creds — no
    # AssumeRole hop needed (the IngesterLambdaRole policy carries everything
    # the ingesters call against the local account). Don't set
    # BEDROCK_OPS_LENS_FORCE_ASSUME_SELF — that would force an assume into a
    # non-existent BedrockOpsLensReader role and 4-of-5 modules would skip.

    # Order matters. quotas runs LAST: at org scale across many regions the
    # Service Quotas API rate-limits hard and quotas can run for 10+ minutes,
    # so if it ran early it would starve the volumetric/cost modules of the
    # 15-min Lambda budget and they'd never execute. Running the fast,
    # primary-data modules (lifecycle, cw_metrics, cost) first guarantees the
    # dashboard's core data always lands even if quotas later times out; a
    # partial quotas pass simply resumes on the next scheduled run.
    schedule = [
        ("model_lifecycle", "ingestion.model_lifecycle",
         ["model_lifecycle", "--db-url", db_url]),
        ("cw_metrics",      "ingestion.cw_metrics",
         ["cw_metrics",      "--db-url", db_url, "--days", str(days)]),
        # AWS/BedrockMantle namespace (the bedrock-mantle endpoint, launched
        # 2026-06-01). Returns empty in regions / accounts without Mantle
        # traffic — does not fail. Same days/auth as cw_metrics.
        ("cw_mantle_metrics", "ingestion.cw_mantle_metrics",
         ["cw_mantle_metrics", "--db-url", db_url, "--days", str(days)]),
        ("cost",            "ingestion.cost",
         ["cost",            "--db-url", db_url, "--days", str(max(days, 30))]),
    ]
    # quotas is appended LAST (after invocation_logs below) — see the
    # ordering note in the module docstring.

    # invocation_logs is opt-in: it requires Bedrock model invocation logging
    # to be enabled with an S3 destination. If BEDROCK_LOGS_BUCKET isn't set,
    # skip — running it without --bucket would just SystemExit(2).
    logs_bucket = os.environ.get("BEDROCK_LOGS_BUCKET", "").strip()
    if logs_bucket:
        # Account list: use the SAME source as cw_metrics (discover-org /
        # explicit / single from config.yaml), not just the central account.
        # The invocation-logs bucket is a single CENTRAL bucket whose keys are
        # partitioned by account (AWSLogs/<accountId>/BedrockModelInvocationLogs/
        # ...), and the ingester already loops over the account list reading
        # each account's prefix. Passing the full org list means per-code data
        # covers every account that delivers logs into the central bucket — the
        # same coverage cw_metrics provides for volumetric data. (Accounts with
        # no logs under their prefix simply contribute nothing — harmless.)
        try:
            from ingestion.accounts import discover_accounts as _disc
        except ImportError:
            from accounts import discover_accounts as _disc  # type: ignore

        class _A:  # minimal args shim: no CLI overrides → falls back to config.yaml
            accounts = None
            accounts_config = None
            discover_org = False
        try:
            _accts = [a.accountId for a in _disc(_A())]
        except Exception:
            _accts = []
        if not _accts:
            # Fall back to the running account so the module still does useful
            # work even if org discovery is unavailable.
            self_acct = os.environ.get("AWS_ACCOUNT_ID", "")
            if not self_acct:
                try:
                    import boto3  # type: ignore
                    self_acct = boto3.client("sts").get_caller_identity()["Account"]
                except Exception:
                    self_acct = ""
            _accts = [self_acct] if self_acct else []
        accounts_csv = ",".join(a for a in _accts if a)

        # Logs are read from the region the bucket lives in. Bedrock invocation
        # logging is per-region and is commonly enabled in us-east-1 even when
        # the dashboard deploys elsewhere, so prefer BEDROCK_LOGS_REGION and
        # fall back to the deploy region (BEDROCK_REGION).
        logs_region = (os.environ.get("BEDROCK_LOGS_REGION", "").strip()
                       or os.environ.get("BEDROCK_REGION", "us-east-1"))
        schedule.append((
            "invocation_logs", "ingestion.invocation_logs",
            ["invocation_logs", "--db-url", db_url, "--days", str(days),
             "--bucket", logs_bucket,
             "--accounts", accounts_csv,
             "--regions", logs_region]
        ))

    # proxy_events is opt-in: a GenAI proxy fronting Bedrock drops one
    # metadata-only event per request into an S3 bucket we read cross-account
    # (Task A — per-workload attribution when the proxy signs everything with
    # one IAM role). Skip unless PROXY_EVENTS_BUCKET is set. Region list mirrors
    # the deploy/logs region convention; the proxy partitions by region under
    # proxy-events/<region>/... so we read every configured region.
    proxy_bucket = os.environ.get("PROXY_EVENTS_BUCKET", "").strip()
    if proxy_bucket:
        proxy_regions = (os.environ.get("PROXY_EVENTS_REGIONS", "").strip()
                         or os.environ.get("BEDROCK_LOGS_REGION", "").strip()
                         or os.environ.get("BEDROCK_REGION", "us-east-1"))
        schedule.append((
            "proxy_events", "ingestion.proxy_events",
            ["proxy_events", "--db-url", db_url, "--days", str(days),
             "--bucket", proxy_bucket,
             "--regions", proxy_regions]
        ))

    # quotas LAST: slowest module at org/multi-region scale (Service Quotas
    # API rate-limits). Appended after invocation_logs so a long/timed-out
    # quotas pass can never starve the primary-data modules above. A partial
    # quotas run resumes on the next scheduled invocation.
    schedule.append((
        "quotas", "ingestion.quotas",
        ["quotas", "--db-url", db_url]
    ))

    if only:
        schedule = [s for s in schedule if s[0] in set(only)]

    results = []
    for name, mod_path, argv in schedule:
        import importlib
        mod = importlib.import_module(mod_path)
        _set_argv(argv)
        results.append(await _run_module(name, mod.main))

    _bump_cache_generation()
    return {"runs": results}


async def _purge_proxy() -> dict:
    """Truncate proxy-derived tables + the object-dedup ledger so a fresh
    ingest re-reads only what's currently in the proxy bucket. Used to clear
    test/synthetic events; harmless if the tables are already empty."""
    import asyncpg
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        try:
            from app.config import _compose_database_url  # type: ignore
        except ImportError:
            from backend.app.config import _compose_database_url  # type: ignore
        db_url = _compose_database_url() or ""
    conn = await asyncpg.connect(db_url)
    out = {}
    try:
        for tbl in ("f_request_events", "f_proxy_dim_hourly", "dim_proxy_dimensions",
                    "proxy_events_objects"):
            try:
                await conn.execute(f"TRUNCATE {tbl}")
                out[tbl] = "truncated"
            except Exception as e:
                out[tbl] = f"skip: {type(e).__name__}"
    finally:
        await conn.close()
    _bump_cache_generation()
    print(f"[purge_proxy] {out}")
    return {"purged": out, "status": "ok"}


async def _apply_migrations() -> dict:
    """Apply db/migrations/*.sql (lexical order) to the live DB. Each migration
    is idempotent, so re-running is safe. Lets an operator apply a new migration
    to an existing stack without a full CFN update (which is what normally
    triggers the schema-init custom resource)."""
    import asyncpg
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        try:
            from app.config import _compose_database_url  # type: ignore
        except ImportError:
            from backend.app.config import _compose_database_url  # type: ignore
        db_url = _compose_database_url() or ""

    task_root = os.environ.get("LAMBDA_TASK_ROOT", os.getcwd())
    mig_dir = os.path.join(task_root, "db", "migrations")
    if not os.path.isdir(mig_dir):
        # Fall back to the repo layout when run outside Lambda.
        mig_dir = os.path.join(os.path.dirname(__file__), "..", "db", "migrations")

    out: dict = {}
    conn = await asyncpg.connect(db_url)
    try:
        names = sorted(n for n in os.listdir(mig_dir) if n.endswith(".sql"))
        for name in names:
            with open(os.path.join(mig_dir, name)) as fh:
                sql = fh.read()
            try:
                await conn.execute(sql)
                out[name] = "ok"
            except Exception as e:  # noqa: BLE001 — report per-file, keep going
                out[name] = f"error: {type(e).__name__}: {e}"
    finally:
        await conn.close()
    _bump_cache_generation()
    print(f"[migrate] {out}")
    return {"migrations": out, "status": "ok"}


# ------------------------------------------------------------------ handler
def handler(event, context):
    """Lambda entrypoint.

    Event shapes accepted:
      {}                              → run all
      {"only": ["cw_metrics"]}        → run only those
      {"days": 30, "only": [...]}    → with custom lookback
      {"source": "aws.events", ...}   → EventBridge schedule (no params)
    """
    if not isinstance(event, dict):
        event = {}

    # Maintenance action: purge proxy-derived tables. Use ONLY to clear test/
    # synthetic proxy events from a DB — real proxy data re-populates on the
    # next ingest from whatever is in the customer's PROXY_EVENTS_BUCKET.
    # Never runs unless explicitly requested via {"admin":"purge_proxy"}.
    if event.get("admin") == "purge_proxy":
        return asyncio.run(_purge_proxy())

    # Maintenance action: apply db/migrations/*.sql to the live DB. Migrations
    # normally run via the schema-init custom resource on stack create/update;
    # this lets an operator apply a new idempotent migration to an existing
    # stack without a full CFN update. Only runs on explicit {"admin":"migrate"}.
    if event.get("admin") == "migrate":
        return asyncio.run(_apply_migrations())

    only = event.get("only")
    if only and not isinstance(only, list):
        only = [str(only)]
    days = int(event.get("days", os.environ.get("INGESTER_DAYS_DEFAULT", "14")))

    print(f"[ingester] event={json.dumps(event)[:300]}  only={only}  days={days}")
    result = asyncio.run(_orchestrate(only=only, days=days))

    # Surface a summary for log-greppability.
    failed = [r for r in result["runs"] if r.get("rc") not in (0, None)]
    result["status"] = "ok" if not failed else "partial"
    result["failed_count"] = len(failed)
    return result


if __name__ == "__main__":
    # Local-dev shortcut: `python -m ingestion.lambda_handler` runs everything.
    import json as _j
    print(_j.dumps(handler({}, None), indent=2, default=str))
