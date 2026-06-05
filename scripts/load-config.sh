#!/usr/bin/env bash
# ============================================================================
# Loads config.yaml and exports the shell-side variables that deploy.sh
# and setup-pipeline.sh consume. Single source of truth for both Python
# and shell.
#
# Usage (in another script):
#   source "$(dirname "$0")/load-config.sh"
#
# After sourcing, these vars are exported:
#   DEPLOY_REGION
#   MONITORED_ACCOUNTS_MODE       single|explicit|discover-org
#   MONITORED_ACCOUNTS_IDS_CSV    "111,222,..." (only when mode=explicit)
#   MONITORED_REGIONS_PRESET      us-major|us-eu-apac|all|explicit
#   MONITORED_REGIONS_CSV         "us-east-1,us-west-2,..." (resolved)
#   INVOCATION_LOGGING_ENABLED    true|false
#   INVOCATION_LOG_TEXT|IMAGE|EMBEDDING|VIDEO
#   BEDROCK_REGION
#   BEDROCK_MODEL_ID
#   BEDROCK_OPS_LENS_ROLE_NAME
#   BEDROCK_OPS_LENS_EXTERNAL_ID
# ============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

# Run the python emitter once into a temp file, then source it. Process-
# substitution `<(...)` works on most setups but quietly fails when the
# parent shell can't open /dev/fd, so the temp-file form is more portable.
__BL_TMP="$(mktemp -t bedrock-lens-cfg.XXXXXX.sh)"
( cd "$ROOT" && "$PY" - <<'EOF' > "$__BL_TMP"
from ingestion.config import load_config
cfg = load_config()
def out(k, v): print(f'export {k}="{v}"')
out("DEPLOY_REGION", cfg.deploy_region)
out("MONITORED_ACCOUNTS_MODE", cfg.monitored_accounts.mode)
out("MONITORED_ACCOUNTS_IDS_CSV", ",".join(cfg.monitored_accounts.ids))
out("MONITORED_REGIONS_PRESET", cfg.monitored_regions.preset)
out("MONITORED_REGIONS_CSV", ",".join(cfg.resolved_regions()))
out("INVOCATION_LOGGING_ENABLED", str(cfg.invocation_logging.enabled).lower())
out("INVOCATION_LOG_TEXT", str(cfg.invocation_logging.text_data).lower())
out("INVOCATION_LOG_IMAGE", str(cfg.invocation_logging.image_data).lower())
out("INVOCATION_LOG_EMBEDDING", str(cfg.invocation_logging.embedding_data).lower())
out("INVOCATION_LOG_VIDEO", str(cfg.invocation_logging.video_data).lower())
out("BEDROCK_REGION", cfg.ops_review.bedrock_region)
out("BEDROCK_MODEL_ID", cfg.ops_review.bedrock_model_id)
out("BEDROCK_OPS_LENS_ROLE_NAME", cfg.iam.reader_role_name)
out("BEDROCK_OPS_LENS_EXTERNAL_ID", cfg.iam.external_id)
EOF
)
# shellcheck disable=SC1090
source "$__BL_TMP"
rm -f "$__BL_TMP"
unset __BL_TMP
