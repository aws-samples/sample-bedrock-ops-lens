"""HTTP client for the deployed Bedrock Ops Lens API.

Two-step authentication:
  1. POST /api/auth/signin {email, password} → returns either:
       a. A Set-Cookie header carrying the Cognito ID token (signed-in immediately)
       b. {"challenge":"NEW_PASSWORD_REQUIRED", "session": "...", "email": "..."}
          — the user is in FORCE_CHANGE_PASSWORD; we don't try to handle that
          flow programmatically. Tell the caller to log in via the dashboard
          first to set a permanent password.
  2. Subsequent /api/* calls send the cookie automatically.

Token is cached in memory for the duration of the MCP server process. If
the deployed endpoint returns 401, we re-sign-in once on demand.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class AuthError(RuntimeError):
    """Raised when sign-in fails or the user is in a state we can't handle."""


class ApiClient:
    """Stateful HTTP client for /api/* on the deployed dashboard."""

    def __init__(self, base_url: str, email: str, password: str,
                 *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._default_timeout = timeout_s
        # Default 30s; ops-review/synthesize calls Bedrock which can take
        # 1-2 minutes — use a per-request override there.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._signed_in = False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _sign_in(self) -> None:
        """POST /api/auth/signin and stash the session cookie on the client."""
        r = self._client.post(
            "/api/auth/signin",
            json={"email": self._email, "password": self._password},
            headers={"Content-Type": "application/json"},
        )
        # 200 = signed in; cookie is auto-stored on the httpx Client.
        # The body might still indicate a challenge.
        try:
            data = r.json()
        except Exception:
            data = {}

        if data.get("challenge") == "NEW_PASSWORD_REQUIRED":
            raise AuthError(
                f"User {self._email} must set a permanent password before this MCP "
                "can authenticate. Sign in once via the dashboard URL, set a new "
                "password, then re-run this MCP with that permanent password."
            )

        if r.status_code == 200 and data.get("ok"):
            self._signed_in = True
            return

        msg = data.get("message") or r.text[:200] or f"HTTP {r.status_code}"
        raise AuthError(f"Sign-in failed: {msg}")

    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------
    def get(self, path: str, *, params: dict | None = None) -> Any:
        """GET /api/<path>. Auto sign-in on first call; auto retry on 401."""
        if not self._signed_in:
            self._sign_in()

        url = path if path.startswith("/api") else f"/api{path}"
        r = self._client.get(url, params=params or {})
        if r.status_code == 401:
            # Cookie expired — try once more with a fresh sign-in.
            self._signed_in = False
            self._sign_in()
            r = self._client.get(url, params=params or {})
        r.raise_for_status()
        return r.json()

    def post(self, path: str, *, json_body: dict | None = None,
             timeout_s: float | None = None) -> Any:
        if not self._signed_in:
            self._sign_in()
        url = path if path.startswith("/api") else f"/api{path}"
        kwargs = {"json": json_body or {}}
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s
        r = self._client.post(url, **kwargs)
        if r.status_code == 401:
            self._signed_in = False
            self._sign_in()
            r = self._client.post(url, **kwargs)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    def close(self) -> None:
        self._client.close()


class SigV4ApiClient:
    """SigV4-signed direct caller against the Lambda Function URL.

    For CLI tools / MCPs / IDE agents whose user already has AWS IAM creds.
    Bypasses CloudFront + Cognito entirely:

        MCP → (SigV4-signed by user creds) → Lambda Function URL

    The Lambda's resource policy in CFN explicitly allows
    `Principal: <account-id>` to invoke `lambda:InvokeFunctionUrl` with
    `AuthType: AWS_IAM` — so any IAM principal in the same account can
    reach the API. Browser users still go through CloudFront/Cognito.

    No password, no cookie. The SigV4 signature includes the body hash
    (Lambda Function URL requires it for POST/PUT) so we compute that
    explicitly via boto3's SigV4Auth.
    """

    def __init__(self, function_url: str, *, timeout_s: float = 30.0) -> None:
        # boto3 imports are deferred so this module still imports cleanly
        # in Tier A direct-collector contexts that don't need them.
        import boto3                              # noqa: F401
        from botocore.auth import SigV4Auth       # noqa: F401
        from botocore.awsrequest import AWSRequest  # noqa: F401

        self.base_url = function_url.rstrip("/")
        self._default_timeout = timeout_s
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._session = boto3.Session()
        creds = self._session.get_credentials()
        if creds is None:
            raise AuthError(
                "BEDROCK_LENS_FUNCTION_URL is set but no AWS credentials are "
                "available. Configure AWS_PROFILE / AWS credentials before "
                "running the MCP."
            )
        self._creds = creds.get_frozen_credentials()
        # Sign for the region the Function URL lives in — parsed from the
        # hostname (…lambda-url.<region>.on.aws). Deriving it from the
        # session/env instead breaks with InvalidSignatureException
        # ("Credential should be scoped to a valid region") whenever the
        # user's default profile region differs from the deploy region.
        import re as _re
        _m = _re.search(r"lambda-url\.([a-z0-9-]+)\.on\.aws", self.base_url)
        self._region = (_m.group(1) if _m else None) \
            or self._session.region_name or "us-east-1"

    def _sign(self, method: str, path: str,
               *, params: dict | None = None,
               json_body: dict | None = None,
               client: "httpx.Client | None" = None) -> "httpx.Request":
        import json as _json
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = self.base_url + (path if path.startswith("/api") else f"/api{path}")
        body = _json.dumps(json_body).encode("utf-8") if json_body is not None else b""

        # Build a botocore request, sign it, and replay the headers onto httpx.
        aws_req = AWSRequest(
            method=method, url=url, data=body,
            params=params or {},
            headers={"Content-Type": "application/json"} if body else {},
        )
        SigV4Auth(self._creds, "lambda", self._region).add_auth(aws_req)
        signed_headers = dict(aws_req.headers.items())
        # AWSRequest includes "Host" — httpx sets it automatically; remove
        # to avoid the duplicate header warning.
        signed_headers.pop("Host", None)

        c = client or self._client
        return c.build_request(
            method, url,
            params=params or {},
            content=body or None,
            headers=signed_headers,
        )

    def get(self, path: str, *, params: dict | None = None) -> Any:
        req = self._sign("GET", path, params=params)
        r = self._client.send(req)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, *, json_body: dict | None = None,
             timeout_s: float | None = None) -> Any:
        # Build a one-off client when caller wants a longer timeout (the
        # ops-review synthesis call needs ~120s; the default 30s on the
        # cached client would cut it off). The signed request must be
        # built BY the same client that will send it — sign + send below.
        if timeout_s is not None and timeout_s > self._default_timeout:
            with httpx.Client(timeout=timeout_s, follow_redirects=False) as c:
                req = self._sign("POST", path, json_body=json_body or {}, client=c)
                r = c.send(req)
        else:
            req = self._sign("POST", path, json_body=json_body or {})
            r = self._client.send(req)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    def close(self) -> None:
        self._client.close()


def from_env() -> ApiClient | SigV4ApiClient | None:
    """Build the right client from BEDROCK_LENS_* env vars.

    Precedence:
      1. BEDROCK_LENS_FUNCTION_URL set → SigV4 (no password)
      2. BEDROCK_LENS_API + USER + PASSWORD → Cognito-cookie via CloudFront
      3. None of the above → None (Tier A direct boto3)

    Mode 1 is the recommended path for CLI/MCP users with AWS creds.
    Mode 2 is what browser users hit; sharing it with the MCP works but
    forces a Cognito password.
    """
    fn_url = (os.environ.get("BEDROCK_LENS_FUNCTION_URL") or "").strip()
    if fn_url:
        return SigV4ApiClient(fn_url)

    base = (os.environ.get("BEDROCK_LENS_API") or "").strip()
    if not base:
        return None
    email = (os.environ.get("BEDROCK_LENS_USER")
             or os.environ.get("BEDROCK_LENS_EMAIL")
             or "").strip()
    password = (os.environ.get("BEDROCK_LENS_PASSWORD")
                or os.environ.get("BEDROCK_LENS_PWD")
                or "").strip()
    if not email or not password:
        raise AuthError(
            "BEDROCK_LENS_API is set, but BEDROCK_LENS_USER/BEDROCK_LENS_PASSWORD "
            "are missing. Either export both, set BEDROCK_LENS_FUNCTION_URL "
            "instead (no-password SigV4 mode), or unset BEDROCK_LENS_API to "
            "fall back to Tier A (direct AWS calls)."
        )
    return ApiClient(base, email, password)
