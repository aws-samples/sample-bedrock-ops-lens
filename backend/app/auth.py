"""Auth middleware + Cognito Hosted UI handlers.

Modes:
  - settings.auth_enabled = False   no-op middleware. request.state.user gets
                                    a synthetic dev identity so user_preferences
                                    works locally without Cognito.
  - settings.auth_enabled = True    verify the Cognito ID token from the session
                                    cookie (set by /api/auth/callback after the
                                    Hosted UI exchange). 401 → SPA redirects to
                                    /api/auth/login, which 302s to the Hosted UI.

Endpoints (only registered when auth_enabled):
  GET  /api/auth/login      302 → Cognito Hosted UI (Authorization Code flow)
  GET  /api/auth/callback   exchange ?code=… for tokens, set HttpOnly cookie,
                            302 → /
  GET  /api/auth/logout     clear cookie, 302 → Cognito's /logout (which then
                            redirects back to PUBLIC_BASE_URL).

JWT verification:
  - JWKS keys cached in-memory at first request. Cognito rotates rarely;
    we re-fetch on signature failure as a self-healing fallback.
  - Validates: signature, issuer, aud (= app client id), token_use=id, exp.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings, cognito_app_client_id


# ---------------------------------------------------------------------------
# JWKS cache + verifier
# ---------------------------------------------------------------------------
_JWKS_CACHE: dict[str, Any] = {"keys": None}


def _jwks_url() -> str:
    return (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
        f"{settings.cognito_user_pool_id}/.well-known/jwks.json"
    )


def _fetch_jwks() -> dict:
    with urllib.request.urlopen(_jwks_url(), timeout=5) as r:
        return json.loads(r.read())


def _get_key(kid: str) -> dict | None:
    if not _JWKS_CACHE["keys"]:
        _JWKS_CACHE["keys"] = _fetch_jwks()
    for k in _JWKS_CACHE["keys"].get("keys", []):
        if k["kid"] == kid:
            return k
    # Refresh once on miss (key rotation case).
    _JWKS_CACHE["keys"] = _fetch_jwks()
    for k in _JWKS_CACHE["keys"].get("keys", []):
        if k["kid"] == kid:
            return k
    return None


def _verify_id_token(token: str) -> dict | None:
    """Return claims dict on success, None on any failure. python-jose handles
    signature, exp, iat, nbf, aud, iss verification in one call."""
    try:
        from jose import jwt as jose_jwt
    except ImportError:
        print("ERROR: python-jose is required for AUTH_ENABLED=true. "
              "pip install python-jose[cryptography]")
        return None

    try:
        unverified_header = jose_jwt.get_unverified_header(token)
        key = _get_key(unverified_header.get("kid", ""))
        if not key:
            return None
        claims = jose_jwt.decode(
            token,
            key,
            algorithms=[unverified_header.get("alg", "RS256")],
            audience=cognito_app_client_id(),
            issuer=(
                f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
                f"{settings.cognito_user_pool_id}"
            ),
        )
    except Exception as e:
        print(f"JWT verify failed: {type(e).__name__}: {e}")
        return None

    if claims.get("token_use") != "id":
        return None
    return claims


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.auth_enabled:
            request.state.user = {"sub": "default", "email": "local@dev", "groups": []}
            return await call_next(request)

        # Public endpoints — no auth required.
        # /api/auth/*  → the auth dance itself (login, callback, logout)
        # /api/health  → ALB target-group health check — MUST stay public,
        #                otherwise ECS will mark every task unhealthy and
        #                restart-loop forever (no auth cookie from the LB).
        path = request.url.path
        if path.startswith("/api/auth/") or path == "/api/health":
            return await call_next(request)

        token = request.cookies.get(settings.session_cookie_name, "")
        claims = _verify_id_token(token) if token else None
        if claims:
            request.state.user = {
                "sub":    claims.get("sub", ""),
                "email":  claims.get("email", ""),
                "groups": claims.get("cognito:groups", []) or [],
                "via":    "cognito",
            }
            return await call_next(request)

        # No Cognito cookie — but maybe the request was SigV4-signed and reached
        # us via Lambda Function URL's AuthType: AWS_IAM directly (i.e., not
        # through CloudFront). The Function URL's resource policy already
        # restricted Principal to our own account ID, so anyone reaching this
        # branch is provably an IAM principal in this account. We honour them
        # as authenticated. Browser users never reach here (they always have
        # a cookie or hit a public path above).
        sigv4_user = _extract_sigv4_caller(request)
        if sigv4_user is not None:
            request.state.user = sigv4_user
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"error": "unauthenticated", "login_url": "/api/auth/login"},
        )


def current_user_id(request: Request) -> str:
    return getattr(request.state, "user", {}).get("sub", "default")


# Cognito group that grants admin (Settings write access). Mirrors the
# frontend UserContext check. In local dev (AUTH_ENABLED=false) there are no
# groups, so is_admin() treats everyone as admin — same as the SPA.
ADMIN_GROUP = "bedrock-lens-admins"


def is_admin(request: Request) -> bool:
    """True if the caller may change stack-wide settings.

    Admin = member of the bedrock-lens-admins Cognito group, OR auth is
    disabled (local dev), OR the caller is a SigV4 IAM principal (CLI/MCP
    access to the Function URL is already account-scoped by the resource
    policy, so treat it as trusted-operator)."""
    if os.environ.get("AUTH_ENABLED", "false").lower() != "true":
        return True
    user = getattr(request.state, "user", {}) or {}
    groups = user.get("groups", []) or []
    if ADMIN_GROUP in groups:
        return True
    # SigV4 IAM callers get a synthetic 'bedrock-lens-iam' group (see above).
    if "bedrock-lens-iam" in groups:
        return True
    return False


def _extract_sigv4_caller(request: Request) -> dict | None:
    """If this request reached us via direct SigV4 (Lambda Function URL
    AWS_IAM auth, NOT via CloudFront), return a synthetic user dict.
    Otherwise None.

    The browser path is:    Browser → CloudFront → (OAC SigV4) → Lambda
    The CLI/MCP path is:    boto3   → (SigV4) → Lambda Function URL

    Both populate `requestContext.authorizer.iam.userArn`, so the userArn
    alone can't distinguish them. Instead we check whether the request
    carries CloudFront's request-id header — present on every CF-routed
    request, absent on direct Function URL invocations. If `x-amz-cf-id`
    is present, the request came through CloudFront and MUST have had a
    valid Cognito cookie (handled above); reaching this code path means
    the cookie path failed → 401, no SigV4 fallback.

    Function URL's resource policy in CFN restricts Principal to our own
    account, so a direct (non-CF) caller is provably an IAM principal in
    this account.
    """
    # If CloudFront forwarded this request, do NOT honour SigV4 — the
    # request must have a valid Cognito cookie or be on a public path.
    # x-amz-cf-id is set by every CloudFront edge on the origin request.
    if request.headers.get("x-amz-cf-id"):
        return None
    if "cloudfront" in (request.headers.get("via") or "").lower():
        return None

    aws_event = request.scope.get("aws.event") or {}
    request_ctx = aws_event.get("requestContext") or {}
    authorizer = request_ctx.get("authorizer") or {}
    iam = authorizer.get("iam") or {}
    user_arn = iam.get("userArn") or ""
    account_id = iam.get("accountId") or ""

    if not user_arn or not account_id:
        return None

    return {
        "sub":    user_arn,
        "email":  user_arn,            # no email; show the ARN instead
        "groups": ["bedrock-lens-iam"],  # synthetic group for downstream code
        "via":    "sigv4",
        "account_id": account_id,
    }


# ---------------------------------------------------------------------------
# Custom-form auth endpoints
#
# We deliberately do NOT use the Cognito Hosted UI. The SPA renders its own
# Cloudscape sign-in / sign-up / verify forms, POSTs credentials here, and
# this module proxies to Cognito via boto3. Same User Pool, same Pre-Sign-Up
# Lambda gate, same JWT — just a much nicer UI.
# ---------------------------------------------------------------------------
import boto3                          # noqa: E402 — local to keep cold-start fast for AUTH_ENABLED=false
from botocore.exceptions import ClientError  # noqa: E402

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


def _cognito_client():
    return boto3.client("cognito-idp", region_name=settings.cognito_region)


def _set_session_cookie(resp, id_token: str, request: "Request | None" = None) -> None:
    # Cookie `Secure` flag must be true on HTTPS, false on http://localhost.
    # Two ways to know which: explicit PUBLIC_BASE_URL env, or the incoming
    # request's X-Forwarded-Proto. Behind CloudFront we use the latter to
    # avoid baking the dashboard URL into Lambda env (would create a CFN
    # circular dep). Locally PUBLIC_BASE_URL is set explicitly.
    is_https = settings.public_base_url.startswith("https://")
    if not is_https and request is not None:
        is_https = (request.headers.get("x-forwarded-proto", "").lower() == "https")
    resp.set_cookie(
        key=settings.session_cookie_name,
        value=id_token,
        max_age=3600,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _cognito_error(e: ClientError) -> JSONResponse:
    """Translate a Cognito ClientError into a clean JSON response.

    Cognito returns ~20 distinct error codes; we map the user-facing ones to
    HTTP statuses + a short English message. Anything we don't recognize gets
    a generic 400 with the AWS message bubbled up so the UI shows something
    useful (and we can add it to the mapping later).
    """
    code = e.response.get("Error", {}).get("Code", "")
    msg = e.response.get("Error", {}).get("Message", "Sign-in failed.")
    mapping = {
        "NotAuthorizedException":      (401, "Incorrect email or password."),
        "UserNotFoundException":       (401, "Incorrect email or password."),
        "UserNotConfirmedException":   (403, "Please verify your email before signing in."),
        "PasswordResetRequiredException": (403, "Your password needs to be reset."),
        "UsernameExistsException":     (409, "An account with that email already exists."),
        "InvalidPasswordException":    (400, msg),  # message is descriptive (length/symbol rules)
        "InvalidParameterException":   (400, msg),
        "CodeMismatchException":       (400, "That code is incorrect."),
        "ExpiredCodeException":        (400, "That code has expired. Request a new one."),
        "LimitExceededException":      (429, "Too many attempts. Try again in a minute."),
        "TooManyRequestsException":    (429, "Too many attempts. Try again in a minute."),
        "TooManyFailedAttemptsException": (429, "Too many failed attempts. Try again later."),
        # Pre-Sign-Up Lambda errors come back wrapped — surface the raw message
        # because the Lambda authored a user-facing string already.
        "UserLambdaValidationException": (400, msg.replace("PreSignUp failed with error ", "")),
    }
    status, friendly = mapping.get(code, (400, msg))
    return _err(status, code or "unknown", friendly)


@auth_router.post("/signin")
async def signin(request: Request, body: dict):
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return _err(400, "missing_fields", "Email and password are required.")
    client = _cognito_client()
    try:
        resp = client.initiate_auth(
            ClientId=cognito_app_client_id(),
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except ClientError as e:
        return _cognito_error(e)

    auth = resp.get("AuthenticationResult") or {}
    id_token = auth.get("IdToken", "")
    if not id_token:
        challenge = resp.get("ChallengeName", "")
        # NEW_PASSWORD_REQUIRED: admin-create-user without --permanent puts
        # the user in FORCE_CHANGE_PASSWORD. Cognito returns a Session token
        # we hand back to the frontend so it can call /set-new-password
        # without re-entering credentials.
        if challenge == "NEW_PASSWORD_REQUIRED":
            return JSONResponse(content={
                "ok": False,
                "challenge": "NEW_PASSWORD_REQUIRED",
                "session": resp.get("Session", ""),
                "email": email,
                "message": "You must set a new password before signing in.",
            })
        # MFA / SOFTWARE_TOKEN_MFA / etc. — we don't yet handle these on the
        # custom form. Tell the user clearly rather than leaving them stuck.
        return _err(400, "challenge_required",
                    f"Additional authentication required ({challenge}). Not supported yet.")
    if not _verify_id_token(id_token):
        return _err(401, "invalid_token", "Token verification failed.")
    out = JSONResponse(content={"ok": True})
    _set_session_cookie(out, id_token, request)
    return out


@auth_router.post("/set-new-password")
async def set_new_password(request: Request, body: dict):
    """Completes a NEW_PASSWORD_REQUIRED challenge.

    Frontend calls this after /signin returned `challenge=NEW_PASSWORD_REQUIRED`.
    The Session token from that response is the only authn the user needs —
    they don't re-send their temp password (Cognito's design).
    """
    session = body.get("session") or ""
    email = (body.get("email") or "").strip().lower()
    new_password = body.get("password") or ""
    if not session or not email or not new_password:
        return _err(400, "missing_fields",
                    "Session, email, and new password are required.")
    client = _cognito_client()
    try:
        resp = client.respond_to_auth_challenge(
            ClientId=cognito_app_client_id(),
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=session,
            ChallengeResponses={
                "USERNAME": email,
                "NEW_PASSWORD": new_password,
            },
        )
    except ClientError as e:
        return _cognito_error(e)

    auth = resp.get("AuthenticationResult") or {}
    id_token = auth.get("IdToken", "")
    if not id_token:
        # Cognito chained another challenge (unusual after NEW_PASSWORD_REQUIRED).
        return _err(400, "challenge_required",
                    f"Unexpected follow-up challenge: {resp.get('ChallengeName','')}")
    if not _verify_id_token(id_token):
        return _err(401, "invalid_token", "Token verification failed.")
    out = JSONResponse(content={"ok": True})
    _set_session_cookie(out, id_token, request)
    return out


@auth_router.post("/signup")
async def signup(request: Request, body: dict):
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return _err(400, "missing_fields", "Email and password are required.")
    client = _cognito_client()
    try:
        client.sign_up(
            ClientId=cognito_app_client_id(),
            Username=email,
            Password=password,
            UserAttributes=[{"Name": "email", "Value": email}],
        )
    except ClientError as e:
        return _cognito_error(e)
    return JSONResponse(content={"ok": True, "next": "verify"})


@auth_router.post("/confirm")
async def confirm(request: Request, body: dict):
    """Confirm sign-up with the 6-digit code Cognito emailed."""
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    if not email or not code:
        return _err(400, "missing_fields", "Email and verification code are required.")
    client = _cognito_client()
    try:
        client.confirm_sign_up(
            ClientId=cognito_app_client_id(),
            Username=email,
            ConfirmationCode=code,
        )
    except ClientError as e:
        return _cognito_error(e)
    return JSONResponse(content={"ok": True})


@auth_router.post("/resend-code")
async def resend_code(request: Request, body: dict):
    email = (body.get("email") or "").strip().lower()
    if not email:
        return _err(400, "missing_fields", "Email is required.")
    client = _cognito_client()
    try:
        client.resend_confirmation_code(
            ClientId=cognito_app_client_id(),
            Username=email,
        )
    except ClientError as e:
        return _cognito_error(e)
    return JSONResponse(content={"ok": True})


@auth_router.post("/forgot-password")
async def forgot_password(request: Request, body: dict):
    email = (body.get("email") or "").strip().lower()
    if not email:
        return _err(400, "missing_fields", "Email is required.")
    client = _cognito_client()
    try:
        client.forgot_password(
            ClientId=cognito_app_client_id(),
            Username=email,
        )
    except ClientError as e:
        return _cognito_error(e)
    return JSONResponse(content={"ok": True})


@auth_router.post("/reset-password")
async def reset_password(request: Request, body: dict):
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    new_password = body.get("password") or ""
    if not email or not code or not new_password:
        return _err(400, "missing_fields", "Email, code, and new password are required.")
    client = _cognito_client()
    try:
        client.confirm_forgot_password(
            ClientId=cognito_app_client_id(),
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )
    except ClientError as e:
        return _cognito_error(e)
    return JSONResponse(content={"ok": True})


@auth_router.post("/logout")
async def logout(request: Request):
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(settings.session_cookie_name, path="/")
    return resp


def install_auth(app: FastAPI) -> None:
    """Register middleware + auth endpoints on the FastAPI app."""
    app.add_middleware(AuthMiddleware)
    if settings.auth_enabled:
        app.include_router(auth_router)
