"""Cognito Pre-Sign-Up Lambda trigger — domain allowlist gate.

Rejects any sign-up attempt where the user's email domain isn't on the
allowlist (case-insensitive). Cognito invokes this BEFORE the account is
created — raising any exception aborts sign-up cleanly, the user sees the
error message, and no account is provisioned.

The user STILL has to verify ownership of the email via the standard
6-digit code Cognito sends — we deliberately do NOT auto-confirm. The
domain check stops random non-allowed emails from even reaching that step,
but ownership is what proves "this is the person who owns this address",
and that happens at the verification stage.

Environment variables:
    ALLOWED_EMAIL_DOMAINS  Comma-separated list of allowed email domains
                           (no leading '@'). Special value "*" disables the
                           domain check entirely (any domain accepted) —
                           use only if you really want public sign-ups.

                           Required: deploy.sh prompts for this and refuses
                           to deploy without it. There is intentionally no
                           default in code — a forked, public-repo deploy
                           must NOT silently inherit someone else's allowlist.
"""
import os


def _parse_domains(raw):
    return [
        d.strip().lower().lstrip("@")
        for d in (raw or "").split(",")
        if d.strip()
    ]


_RAW = os.environ.get("ALLOWED_EMAIL_DOMAINS", "")
ALLOW_ALL = _RAW.strip() == "*"
ALLOWED_DOMAINS = [] if ALLOW_ALL else _parse_domains(_RAW)


def handler(event, context):  # noqa: ARG001
    email = (event.get("request", {}).get("userAttributes", {}).get("email") or "").lower()

    # Defensive: a missing email shouldn't pass — Cognito's email-required
    # attribute catches this too, but we double-check at the gate.
    if not email or "@" not in email:
        raise Exception("An email address is required to sign up.")

    if ALLOW_ALL:
        return event

    if not ALLOWED_DOMAINS:
        # Misconfiguration — env var was empty/missing at deploy time.
        # Fail closed: refuse all sign-ups until the deployer fixes it.
        raise Exception(
            "Sign-up is currently disabled. "
            "Ask the dashboard administrator to configure ALLOWED_EMAIL_DOMAINS."
        )

    domain = email.rsplit("@", 1)[-1]
    if domain not in ALLOWED_DOMAINS:
        allowed = ", ".join("@" + d for d in ALLOWED_DOMAINS)
        raise Exception(
            f"Sign-up is restricted to {allowed} email addresses. "
            f"The address you used ({email}) is not permitted."
        )

    return event
