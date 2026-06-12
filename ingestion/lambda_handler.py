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
        self_acct = os.environ.get("AWS_ACCOUNT_ID", "")
        if not self_acct:
            try:
                import boto3  # type: ignore
                self_acct = boto3.client("sts").get_caller_identity()["Account"]
            except Exception:
                self_acct = ""
        region = os.environ.get("BEDROCK_REGION", "us-east-1")
        schedule.append((
            "invocation_logs", "ingestion.invocation_logs",
            ["invocation_logs", "--db-url", db_url, "--days", str(days),
             "--bucket", logs_bucket,
             "--accounts", self_acct or "",
             "--regions", region]
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
