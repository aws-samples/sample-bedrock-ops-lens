"""Process-wide configuration. All knobs come from env vars so the same image
runs locally, in dev, and in Fargate without code changes."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def _compose_database_url() -> str:
    """Resolve the Postgres connection URL.

    Priority:
      1. DATABASE_URL — explicit override, always wins.
      2. DB_HOST + DB_PORT + DB_USER + DB_PASSWORD + DB_NAME — the Fargate
         pattern, where each piece comes from CFN/Secrets so the password
         never appears in plaintext env. Supports a Secrets-Manager-shaped
         password value too: if DB_PASSWORD is JSON {"password":"..."},
         we extract the inner string (Aurora's secret rotates this way).
      3. local-dev fallback to a Homebrew Postgres on localhost.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw:
        return raw

    host = os.environ.get("DB_HOST", "").strip()
    if host:
        import json as _json
        from urllib.parse import quote as _q
        port = os.environ.get("DB_PORT", "5432").strip() or "5432"
        user = os.environ.get("DB_USER", "bedrock_lens").strip() or "bedrock_lens"
        name = os.environ.get("DB_NAME", "bedrock_lens").strip() or "bedrock_lens"
        # Password sources by precedence:
        #   1. DB_PASSWORD env var (Fargate / docker-compose).
        #   2. DB_SECRET_ARN — Secrets Manager ARN (Lambda CFN). Fetched at
        #      runtime so password rotation Just Works.
        password = os.environ.get("DB_PASSWORD", "")
        if password.startswith("{"):
            try:
                password = _json.loads(password).get("password", password)
            except Exception:
                pass
        if not password:
            secret_arn = os.environ.get("DB_SECRET_ARN", "").strip()
            if secret_arn:
                try:
                    import boto3  # type: ignore
                    sec = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
                    payload = _json.loads(sec["SecretString"])
                    password = payload.get("password", "") or password
                    # Honour the secret's username if env didn't override.
                    if user == "bedrock_lens":
                        user = payload.get("username", user) or user
                except Exception:
                    pass
        return f"postgresql://{_q(user)}:{_q(password)}@{host}:{port}/{name}"

    return "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens"


@dataclass(frozen=True)
class Settings:
    database_url: str = _compose_database_url()
    db_pool_min: int = int(os.environ.get("DB_POOL_MIN", "2"))
    db_pool_max: int = int(os.environ.get("DB_POOL_MAX", "10"))

    cache_ttl_seconds: int = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
    cache_max_entries: int = int(os.environ.get("CACHE_MAX_ENTRIES", "5000"))

    # CORS — for local dev the frontend on :5173 needs to hit the backend on :8000.
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.environ.get(
            "CORS_ORIGINS",
            "http://localhost:5173,http://localhost:3000,http://localhost:8000",
        ).split(",") if o.strip()
    )

    # Auth toggle. False = no-op middleware (local dev). True = Cognito JWT verify.
    auth_enabled: bool = _bool("AUTH_ENABLED", False)
    cognito_user_pool_id: str = os.environ.get("COGNITO_USER_POOL_ID", "")
    # COGNITO_APP_CLIENT_ID can come either directly from env, or via SSM
    # (used when CFN can't put the literal value into Lambda env without
    # creating a circular dep — see infra/cloudformation.yaml). Resolved
    # lazily on first call to settings.cognito_app_client_id_resolved().
    cognito_app_client_id: str = os.environ.get("COGNITO_APP_CLIENT_ID", "")
    cognito_app_client_id_ssm_param: str = os.environ.get("COGNITO_APP_CLIENT_ID_SSM_PARAM", "")
    cognito_region: str = os.environ.get("COGNITO_REGION", "us-east-1")
    # Cognito Hosted UI base, e.g.
    #   https://bedrock-lens-<account-id>.auth.<region>.amazoncognito.com
    cognito_domain: str = os.environ.get("COGNITO_DOMAIN", "").rstrip("/")
    # Public URL of the dashboard (CloudFront distribution). Used as the
    # OAuth redirect_uri on /api/auth/callback and for the post-logout target.
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    # Name of the cookie that holds the Cognito ID token after login. Single
    # cookie keeps things simple — refresh-token rotation is a follow-up.
    session_cookie_name: str = os.environ.get("SESSION_COOKIE_NAME", "bedrock_lens_id")

    # Bedrock — used for the Ops Review LLM call.
    # IMPORTANT: Modern Claude models (4.x and later) require an inference
    # profile ARN/ID (CRIS) — bare on-demand IDs are rejected with
    # ValidationException. Use the `us.` (or `eu.` / `global.`) CRIS prefix.
    bedrock_region: str = os.environ.get("BEDROCK_REGION", "us-east-1")
    bedrock_model_id: str = os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-1-20250805-v1:0"
    )
    # Ops Review synthesis runs against the bedrock-mantle endpoint (Anthropic
    # Messages API) — the dashboard dogfoods the endpoint it recommends. Mantle
    # uses short model ids (no CRIS prefix). Sonnet 5: flagship quality, fast
    # enough to finish inside the 120s CloudFront/Lambda budget.
    ops_review_model: str = os.environ.get("OPS_REVIEW_MODEL", "anthropic.claude-sonnet-5")
    # Mantle model availability is per-region: Sonnet 5 lives on us-east-1
    # mantle (us-west-2 mantle only has Haiku 4.5). This is a standalone HTTPS
    # call, so it can target a different region than the app's BEDROCK_REGION
    # (which is used for the app's own region context). Overridable per deploy.
    ops_review_mantle_region: str = os.environ.get("OPS_REVIEW_MANTLE_REGION", "us-east-1")
    # Try the mantle endpoint FIRST for Ops Review synthesis (dogfooding) when
    # true; runtime-first when false. Default false: us-west-2 mantle is
    # currently 500-ing, and runtime Sonnet 5 is fast (~20s) + reliable. The
    # other endpoint is always the fallback either way. Flip to "true" once the
    # regional mantle endpoint is healthy to lead with mantle again.
    ops_review_use_mantle: bool = _bool("OPS_REVIEW_USE_MANTLE", False)


settings = Settings()


# Lazy-resolved Cognito App Client ID. Cached after first SSM read so we
# don't pay the IMDS round-trip on every request.
_cognito_app_client_id_cache: list[str] = [settings.cognito_app_client_id]


def cognito_app_client_id() -> str:
    """Returns the Cognito App Client ID, resolving from SSM on first call
    if not already in env. Used by auth.py and routers that need to know
    which client to authenticate against."""
    if _cognito_app_client_id_cache[0]:
        return _cognito_app_client_id_cache[0]
    if settings.cognito_app_client_id_ssm_param:
        try:
            import boto3
            ssm = boto3.client("ssm", region_name=settings.cognito_region or os.environ.get("AWS_REGION", "us-east-1"))
            resp = ssm.get_parameter(Name=settings.cognito_app_client_id_ssm_param)
            _cognito_app_client_id_cache[0] = resp["Parameter"]["Value"]
            return _cognito_app_client_id_cache[0]
        except Exception as e:
            print(f"WARNING: failed to resolve Cognito App Client ID from SSM: {e}")
    return ""
