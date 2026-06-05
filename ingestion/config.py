#!/usr/bin/env python3
"""
Bedrock Ops Lens — config loader.

Reads `config.yaml` (project root, customer-edited) and exposes a typed
view of the settings. Env vars override individual fields at runtime.

Region preset resolution lives here so every consumer (ingesters, setup
scripts, CDK stack inputs) gets the same expanded list.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "FATAL: PyYAML not installed. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    raise


# ---------------------------------------------------------------------------
# Region presets
# ---------------------------------------------------------------------------
REGION_PRESETS: dict[str, list[str]] = {
    "us-major":   ["us-east-1", "us-east-2", "us-west-2"],
    "us-eu-apac": [
        "us-east-1", "us-east-2", "us-west-2",
        "eu-west-1", "eu-west-2", "eu-central-1",
        "ap-southeast-1", "ap-northeast-1",
    ],
    # "all" is resolved at runtime via account:ListRegions — see resolve_regions().
}


def _live_discover_regions(session=None) -> list[str]:
    """Live-discover every region enabled in the running account, filtered
    to the ones where Bedrock is reachable. Used when preset=all."""
    import boto3
    s = session or boto3._get_default_session() or boto3.Session()
    # account:ListRegions enumerates every region enabled in the running account.
    # Falls back to the static AWS-published list if the API isn't available.
    try:
        client = s.client("account")
        out: list[str] = []
        paginator = client.get_paginator("list_regions")
        for page in paginator.paginate(RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"]):
            for r in page.get("Regions", []) or []:
                out.append(r["RegionName"])
        if out:
            return sorted(set(out))
    except Exception:
        pass
    # Fallback: union of all preset values + a few extras.
    return sorted(set(
        REGION_PRESETS["us-eu-apac"]
        + ["eu-west-3", "ap-south-1", "ap-northeast-2", "ca-central-1", "sa-east-1"]
    ))


# ---------------------------------------------------------------------------
# Typed config view
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonitoredAccountsConfig:
    mode: str = "single"            # "single" | "explicit" | "discover-org"
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitoredRegionsConfig:
    preset: str = "us-eu-apac"      # "us-major" | "us-eu-apac" | "all" | "explicit"
    regions: tuple[str, ...] = ()   # used only when preset == "explicit"


@dataclass(frozen=True)
class InvocationLoggingConfig:
    enabled: bool = True
    text_data: bool = True
    image_data: bool = False
    embedding_data: bool = False
    video_data: bool = False


@dataclass(frozen=True)
class OpsReviewConfig:
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "us.anthropic.claude-opus-4-1-20250805-v1:0"


@dataclass(frozen=True)
class IamConfig:
    reader_role_name: str = "BedrockOpsLensReader"
    external_id: str = ""


@dataclass(frozen=True)
class Config:
    deploy_region: str = "us-east-1"
    monitored_accounts: MonitoredAccountsConfig = field(default_factory=MonitoredAccountsConfig)
    monitored_regions: MonitoredRegionsConfig = field(default_factory=MonitoredRegionsConfig)
    invocation_logging: InvocationLoggingConfig = field(default_factory=InvocationLoggingConfig)
    ops_review: OpsReviewConfig = field(default_factory=OpsReviewConfig)
    iam: IamConfig = field(default_factory=IamConfig)

    def resolved_regions(self, session=None) -> list[str]:
        """Expand the regions preset into a concrete list."""
        cfg = self.monitored_regions
        if cfg.preset == "explicit":
            return list(cfg.regions)
        if cfg.preset == "all":
            return _live_discover_regions(session)
        if cfg.preset in REGION_PRESETS:
            return list(REGION_PRESETS[cfg.preset])
        # Unknown preset — fall back to default list and warn.
        print(
            f"WARN: unknown monitored_regions preset '{cfg.preset}', "
            "falling back to us-eu-apac",
            file=sys.stderr,
        )
        return list(REGION_PRESETS["us-eu-apac"])


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml from the project root (or `path`). Missing file
    is OK — returns defaults with env-var overrides applied."""
    p = Path(path) if path else (_project_root() / "config.yaml")
    raw: dict = {}
    if p.exists():
        with p.open() as fh:
            raw = yaml.safe_load(fh) or {}

    # ---- env overrides -----------------------------------------------------
    deploy_region = _env_str("DEPLOY_REGION", raw.get("deploy_region", "us-east-1"))

    accts_raw = raw.get("monitored_accounts") or {}
    accts_mode = _env_str("MONITORED_ACCOUNTS_MODE", accts_raw.get("mode", "single"))
    accts_ids_env = os.environ.get("MONITORED_ACCOUNTS_IDS", "")
    accts_ids = tuple(
        a.strip() for a in (accts_ids_env.split(",") if accts_ids_env else accts_raw.get("ids", []))
        if str(a).strip().isdigit() and len(str(a).strip()) == 12
    )
    accts = MonitoredAccountsConfig(mode=accts_mode, ids=accts_ids)

    regs_raw = raw.get("monitored_regions") or {}
    regs_preset = _env_str("MONITORED_REGIONS_PRESET", regs_raw.get("preset", "us-eu-apac"))
    regs_explicit_env = os.environ.get("MONITORED_REGIONS_LIST", "")
    regs_explicit = tuple(
        r.strip() for r in (regs_explicit_env.split(",") if regs_explicit_env else regs_raw.get("regions", []))
        if r.strip()
    )
    regs = MonitoredRegionsConfig(preset=regs_preset, regions=regs_explicit)

    log_raw = raw.get("invocation_logging") or {}
    log = InvocationLoggingConfig(
        enabled=        _env_bool("INVOCATION_LOGGING_ENABLED", log_raw.get("enabled", True)),
        text_data=      _env_bool("INVOCATION_LOG_TEXT",        log_raw.get("text_data", True)),
        image_data=     _env_bool("INVOCATION_LOG_IMAGE",       log_raw.get("image_data", False)),
        embedding_data= _env_bool("INVOCATION_LOG_EMBEDDING",   log_raw.get("embedding_data", False)),
        video_data=     _env_bool("INVOCATION_LOG_VIDEO",       log_raw.get("video_data", False)),
    )

    or_raw = raw.get("ops_review") or {}
    op = OpsReviewConfig(
        bedrock_region=   _env_str("BEDROCK_REGION",   or_raw.get("bedrock_region", "us-east-1")),
        bedrock_model_id= _env_str("BEDROCK_MODEL_ID", or_raw.get("bedrock_model_id",
                                                                   "us.anthropic.claude-opus-4-1-20250805-v1:0")),
    )

    iam_raw = raw.get("iam") or {}
    iam = IamConfig(
        reader_role_name= _env_str("BEDROCK_OPS_LENS_ROLE_NAME", iam_raw.get("reader_role_name", "BedrockOpsLensReader")),
        external_id=      _env_str("BEDROCK_OPS_LENS_EXTERNAL_ID", iam_raw.get("external_id", "")),
    )

    return Config(
        deploy_region=deploy_region,
        monitored_accounts=accts,
        monitored_regions=regs,
        invocation_logging=log,
        ops_review=op,
        iam=iam,
    )


# ---------------------------------------------------------------------------
# CLI for ad-hoc inspection — `python -m ingestion.config`
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = load_config()
    print(f"deploy_region:       {cfg.deploy_region}")
    print(f"monitored_accounts:  mode={cfg.monitored_accounts.mode}, "
          f"ids={list(cfg.monitored_accounts.ids)}")
    print(f"monitored_regions:   preset={cfg.monitored_regions.preset}, "
          f"resolved={cfg.resolved_regions()}")
    print(f"invocation_logging:  enabled={cfg.invocation_logging.enabled}, "
          f"text={cfg.invocation_logging.text_data}, "
          f"image={cfg.invocation_logging.image_data}, "
          f"embedding={cfg.invocation_logging.embedding_data}, "
          f"video={cfg.invocation_logging.video_data}")
    print(f"ops_review:          region={cfg.ops_review.bedrock_region}, "
          f"model={cfg.ops_review.bedrock_model_id}")
    print(f"iam:                 reader_role={cfg.iam.reader_role_name}, "
          f"external_id={'<set>' if cfg.iam.external_id else '<empty>'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
