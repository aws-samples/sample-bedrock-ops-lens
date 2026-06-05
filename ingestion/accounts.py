#!/usr/bin/env python3
"""
Account discovery + cross-account session helper for Bedrock Ops Lens
ingestion.

Three account sources, picked by argv:

  1. --accounts ID,ID,...    Explicit comma-separated list (testing, small fleets).
  2. --accounts-config FILE  JSON file: {"accounts": [{"accountId": "123", "name": "..."}]}
  3. --discover-org          Calls organizations:ListAccounts from the running
                             credentials. Requires the central account to be
                             the AWS Organizations management account, or a
                             delegated administrator for Organizations.

For each discovered accountId, the ingester needs a boto3.Session that
makes API calls AS that account. The session is constructed via:

   sts:AssumeRole(
     RoleArn='arn:aws:iam::<accountId>:role/BedrockOpsLensReader',
     ExternalId=<optional, set in env>,
   )

The role must already exist in the target account — deployed via the
StackSet template at infra/monitored-account-role.yaml.

Special case: if accountId == the running credentials' account, we skip
the assume-role and use the running credentials directly (the role would
have to exist in the central account too, which is unnecessary churn for
the development loop). Cross-account behavior is identical regardless;
this just removes one IAM dependency for the home account.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_ROLE_NAME = os.environ.get("BEDROCK_OPS_LENS_ROLE_NAME", "BedrockOpsLensReader")
DEFAULT_EXTERNAL_ID = os.environ.get("BEDROCK_OPS_LENS_EXTERNAL_ID", "")
DEFAULT_SESSION_TTL_SECONDS = int(os.environ.get("BEDROCK_OPS_LENS_SESSION_TTL", "3600"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonitoredAccount:
    accountId: str
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "MonitoredAccount":
        return cls(accountId=str(d["accountId"]).strip(), name=d.get("name", ""))


# ---------------------------------------------------------------------------
# Discovery — three sources
# ---------------------------------------------------------------------------
def discover_from_list(csv: str) -> list[MonitoredAccount]:
    out: list[MonitoredAccount] = []
    for piece in (csv or "").split(","):
        a = piece.strip()
        if a.isdigit() and len(a) == 12:
            out.append(MonitoredAccount(accountId=a))
    return out


def discover_from_file(path: str) -> list[MonitoredAccount]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"accounts config not found: {path}")
    raw = json.loads(p.read_text())
    accts = raw.get("accounts") or raw   # allow bare list or {"accounts":[...]}
    if not isinstance(accts, list):
        raise ValueError(f"{path} must contain a list (or {{accounts: [...]}})")
    return [MonitoredAccount.from_dict(a) for a in accts]


def discover_from_org() -> list[MonitoredAccount]:
    """List all ACTIVE accounts in the Organization. Requires the running
    credentials to be in the management account (or a delegated administrator)."""
    org = boto3.client(
        "organizations",
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    out: list[MonitoredAccount] = []
    paginator = org.get_paginator("list_accounts")
    for page in paginator.paginate():
        for a in page.get("Accounts", []) or []:
            if a.get("Status") == "ACTIVE":
                out.append(MonitoredAccount(accountId=a["Id"], name=a.get("Name", "")))
    return out


# ---------------------------------------------------------------------------
# Assume-role
# ---------------------------------------------------------------------------
class _SessionCache:
    """Process-local cache of assumed-role sessions, keyed by accountId.

    Sessions expire when their underlying STS credentials expire (the
    refresh_using mechanism handles that automatically). We re-use the
    same Session across CW Metrics + Service Quotas + Invocation logs in a
    single ingestion run."""
    def __init__(self) -> None:
        self._cache: dict[str, boto3.Session] = {}
        self._sts = boto3.client(
            "sts",
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )
        # Cache the running creds' account so we can short-circuit self-assume.
        try:
            self._self_account = self._sts.get_caller_identity()["Account"]
        except (BotoCoreError, ClientError) as e:
            raise RuntimeError(f"failed to read caller identity: {e}") from e

    @property
    def self_account(self) -> str:
        return self._self_account

    def session_for(self, account_id: str, role_name: str = DEFAULT_ROLE_NAME,
                     external_id: str = DEFAULT_EXTERNAL_ID) -> boto3.Session:
        cached = self._cache.get(account_id)
        if cached is not None:
            return cached

        # Self-account short-circuit, UNLESS the operator has explicitly
        # opted in to "always assume-role" for testing the cross-account
        # path against the central account itself.
        force_assume = os.environ.get("BEDROCK_OPS_LENS_FORCE_ASSUME_SELF") in ("1", "true", "yes")
        if account_id == self._self_account and not force_assume:
            # Use the running credentials directly. Functionally identical to
            # assume-role into self, but avoids requiring the role to exist
            # in the central account.
            session = boto3.Session()
            self._cache[account_id] = session
            return session

        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        kwargs: dict = dict(
            RoleArn=role_arn,
            RoleSessionName=f"bedrock-ops-lens-{int(datetime.now(timezone.utc).timestamp())}",
            DurationSeconds=DEFAULT_SESSION_TTL_SECONDS,
        )
        if external_id:
            kwargs["ExternalId"] = external_id

        resp = self._sts.assume_role(**kwargs)
        creds = resp["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        self._cache[account_id] = session
        return session


# Singleton — keeps assumed-role creds alive across all ingester modules in
# a given process.
_session_cache: _SessionCache | None = None


def session_cache() -> _SessionCache:
    global _session_cache
    if _session_cache is None:
        _session_cache = _SessionCache()
    return _session_cache


def session_for(account_id: str, role_name: str = DEFAULT_ROLE_NAME,
                 external_id: str = DEFAULT_EXTERNAL_ID) -> boto3.Session:
    return session_cache().session_for(account_id, role_name, external_id)


# ---------------------------------------------------------------------------
# CLI for ad-hoc inspection
# ---------------------------------------------------------------------------
def _add_common_args(ap: argparse.ArgumentParser) -> None:
    src = ap.add_mutually_exclusive_group()
    src.add_argument(
        "--accounts",
        help="comma-separated 12-digit account IDs (overrides config/discovery)",
    )
    src.add_argument(
        "--accounts-config",
        help="JSON file with {\"accounts\": [{\"accountId\": \"123\", \"name\": \"...\"}]}",
    )
    src.add_argument(
        "--discover-org", action="store_true",
        help="enumerate every ACTIVE account via organizations:ListAccounts",
    )
    ap.add_argument(
        "--role-name", default=DEFAULT_ROLE_NAME,
        help=f"cross-account role name (default: {DEFAULT_ROLE_NAME}; env BEDROCK_OPS_LENS_ROLE_NAME)",
    )
    ap.add_argument(
        "--external-id", default=DEFAULT_EXTERNAL_ID,
        help="external ID passed in sts:AssumeRole (env BEDROCK_OPS_LENS_EXTERNAL_ID)",
    )


def discover_accounts(args) -> list[MonitoredAccount]:
    """Resolve the active account-source argv into a list of MonitoredAccount.

    Precedence (first match wins):
      1. --accounts CSV         (explicit args override everything)
      2. --accounts-config FILE (explicit args override everything)
      3. --discover-org         (explicit args override everything)
      4. config.yaml monitored_accounts.mode + ids
      5. Default: just the running credentials' account.
    """
    if getattr(args, "accounts", None):
        return discover_from_list(args.accounts)
    if getattr(args, "accounts_config", None):
        return discover_from_file(args.accounts_config)
    if getattr(args, "discover_org", False):
        return discover_from_org()

    # Fall back to config.yaml. Lazy-import to keep this module importable
    # without PyYAML when only the CLI flags are used.
    try:
        from .config import load_config
        cfg = load_config()
        m = cfg.monitored_accounts.mode
        if m == "explicit" and cfg.monitored_accounts.ids:
            return [MonitoredAccount(accountId=a) for a in cfg.monitored_accounts.ids]
        if m == "discover-org":
            return discover_from_org()
        # m == "single" or unknown -> fallthrough
    except Exception:
        pass

    sc = session_cache()
    return [MonitoredAccount(accountId=sc.self_account, name="(running creds)")]


def main() -> int:
    """Stand-alone CLI for inspecting which accounts will be ingested.
    Useful for verifying StackSet rollout: `python -m ingestion.accounts --discover-org`."""
    ap = argparse.ArgumentParser(description="Inspect monitored-account discovery.")
    _add_common_args(ap)
    ap.add_argument(
        "--probe-assume", action="store_true",
        help="also call sts:AssumeRole on each discovered account and print the result",
    )
    args = ap.parse_args()

    accts = discover_accounts(args)
    print(f"Discovered {len(accts)} account(s):")
    for a in accts:
        print(f"  {a.accountId}  {a.name}")

    if args.probe_assume:
        print("\nProbing assume-role on each...")
        for a in accts:
            try:
                s = session_for(a.accountId, role_name=args.role_name, external_id=args.external_id)
                ident = s.client("sts").get_caller_identity()
                print(f"  ✓ {a.accountId}  → {ident['Arn']}")
            except (BotoCoreError, ClientError) as e:
                print(f"  ✗ {a.accountId}  → {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
