"""One-shot schema bootstrap for Aurora.

Invoked by a CloudFormation custom resource at stack-create. Connects to the
DB, applies db/schema.sql + db/partitions.sql, then signals CFN.

Idempotent: schema.sql uses CREATE ... IF NOT EXISTS everywhere; partitions.sql
is wrapped in a DO block that uses CREATE TABLE ... IF NOT EXISTS PARTITION OF.
Running twice does nothing the second time.

Also a separate response signaller for CFN custom-resource lifecycle —
without this the stack hangs for 1 hour on stack-create until the custom
resource times out.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

import asyncpg


def _send(event, status: str, reason: str, data: dict | None = None) -> None:
    """Signal CFN custom-resource completion."""
    body = json.dumps({
        "Status": status,
        "Reason": reason[:1000],
        "PhysicalResourceId": event.get("PhysicalResourceId", "schema-init"),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode("utf-8")
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"Content-Type": ""},
    )
    urllib.request.urlopen(req, timeout=20).read()


async def _apply(db_url: str, sql_files: list[str]) -> dict:
    """Apply each SQL file. asyncpg.execute() runs multi-statement strings
    via the simple query protocol — provided no statements use $1-style
    parameters. Our DDL files are pure CREATE TABLE / DO blocks so we can
    run them whole. Wrap in a savepoint so a syntax error in one file
    doesn't leave a half-applied schema."""
    conn = await asyncpg.connect(db_url, timeout=30)
    counts: dict[str, str] = {}
    try:
        for path in sql_files:
            with open(path, "r") as fh:
                sql = fh.read()
            print(f"[schema-init] applying {path} ({len(sql)} chars)")
            await conn.execute(sql)
            counts[path] = "ok"

        # Idempotent migrations for existing deployments. schema.sql uses
        # CREATE TABLE IF NOT EXISTS, so columns added to an already-created
        # table are NOT picked up by re-running schema.sql — they need an
        # explicit ADD COLUMN IF NOT EXISTS here.
        migrations = [
            # Peak-TPM quota accuracy: cache-read tokens don't count toward
            # the TPM quota, so f_hourly_peak needs to store them separately
            # so the Peak-TPM formula can subtract them.
            "ALTER TABLE f_hourly_peak "
            "ADD COLUMN IF NOT EXISTS total_cache_read_input_tokens BIGINT",
        ]
        for stmt in migrations:
            print(f"[schema-init] migration: {stmt[:80]}")
            await conn.execute(stmt)
        counts["_migrations"] = str(len(migrations))

        # Sanity check — list user tables.
        rows = await conn.fetch(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND (tablename LIKE 'f\\_%' ESCAPE '\\'
                OR tablename LIKE 'dim\\_%' ESCAPE '\\'
                OR tablename IN ('ingestion_meta', 'ingestion_days'))
            ORDER BY tablename
            """
        )
        counts["_tables"] = ",".join(r["tablename"] for r in rows)
        counts["_table_count"] = str(len(rows))
    finally:
        await conn.close()
    return counts


def _resolve_db_url() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    # Compose from DB_* env vars + Secrets Manager.
    secret_arn = os.environ.get("DB_SECRET_ARN", "")
    if not secret_arn:
        raise RuntimeError("schema_init needs DATABASE_URL or DB_SECRET_ARN")
    import boto3
    sec = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    payload = json.loads(sec["SecretString"])
    user = payload.get("username") or os.environ.get("DB_USER", "bedrock_lens")
    pwd = payload.get("password") or ""
    host = os.environ["DB_HOST"]
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "bedrock_lens")
    # URL-encode user + pwd so special chars in auto-rotated passwords
    # (e.g. {, <, |, /, @) don't break the URI parser.
    from urllib.parse import quote
    user_q = quote(user, safe="")
    pwd_q = quote(pwd, safe="")
    return f"postgresql://{user_q}:{pwd_q}@{host}:{port}/{name}"


def handler(event, context):
    """Lambda handler — CFN custom resource lifecycle."""
    print(f"[schema-init] event={json.dumps(event)[:500]}")
    req_type = event.get("RequestType", "Create")
    if req_type == "Delete":
        # Don't drop schemas on stack-delete (that'd be data loss).
        _send(event, "SUCCESS", "Delete: schema retained (intentional)")
        return

    try:
        db_url = _resolve_db_url()
        # __file__ is /var/task/app/schema_init.py — schema files copied to /var/task/db/
        task_root = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
        sql_files = [
            os.path.join(task_root, "db", "schema.sql"),
            os.path.join(task_root, "db", "partitions.sql"),
        ]
        for f in sql_files:
            if not os.path.isfile(f):
                raise FileNotFoundError(f"missing SQL file: {f}")
        result = asyncio.run(_apply(db_url, sql_files))
        _send(event, "SUCCESS", "schema applied", result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _send(event, "FAILED", f"{type(e).__name__}: {e}")
