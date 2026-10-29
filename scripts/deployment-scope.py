#!/usr/bin/env python3
"""Build persisted deployment scope without printing external IDs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.config import load_config


def scope_parameters(cfg, stack, runtime, organizations_enabled):
    """New installs use config; updates preserve the running ingester's scope."""
    if stack:
        parameters = {p["ParameterKey"]: p["ParameterValue"]
                      for p in stack.get("Parameters", [])}
        env = runtime.get("Environment", {}).get("Variables", {})
        if runtime.get("Environment", {}).get("Error"):
            raise ValueError("Cannot read the running ingester configuration")
        mode = env.get("MONITORED_ACCOUNTS_MODE", parameters.get("MonitoredAccountsMode"))
        if not mode:
            raise ValueError("Existing ingester has no explicit scope; configure it before upgrading")
        ids = env.get("MONITORED_ACCOUNTS_IDS", parameters.get("MonitoredAccountIds", ""))
        ous = env.get("MONITORED_OU_IDS", parameters.get("MonitoredOuIds", ""))
        external_id = env.get("BEDROCK_OPS_LENS_EXTERNAL_ID", parameters.get("ReaderExternalId", ""))
        if external_id == "****":
            raise ValueError("Cannot preserve the reader external ID from the running ingester")
    else:
        mode = cfg.monitored_accounts.mode
        ids = ",".join(cfg.monitored_accounts.ids)
        ous = ",".join(cfg.monitored_accounts.ou_ids)
        external_id = cfg.iam.external_id
    if mode not in {"single", "explicit", "discover-org", "discover-ou"}:
        raise ValueError("Unknown monitored account mode")
    if mode in {"discover-org", "discover-ou"} and not organizations_enabled:
        raise ValueError("Selected scope needs ENABLE_ORGANIZATIONS_DISCOVERY=true")
    if mode == "explicit" and (not ids or any(
            not re.fullmatch(r"\d{12}", item) for item in ids.split(","))):
        raise ValueError("Explicit scope requires valid 12-digit account IDs")
    if mode == "discover-ou" and (not ous or any(
            not re.fullmatch(r"(ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}|r-[a-z0-9]{4,32})", item)
            for item in ous.split(","))):
        raise ValueError("OU scope requires valid OU/root IDs")
    values = {"MonitoredAccountsMode": mode, "MonitoredAccountIds": ids,
              "MonitoredOuIds": ous, "ReaderExternalId": external_id}
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--stack-file", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--organizations-enabled", choices=["true", "false"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    stack = next(iter(json.loads(Path(args.stack_file).read_text()).get("Stacks", [])), None)
    runtime = {}
    if stack:
        import boto3
        runtime = boto3.client("lambda", region_name=args.region).get_function_configuration(
            FunctionName=f'{stack["StackName"]}-ingester')
    params = scope_parameters(load_config(args.config), stack, runtime,
                              args.organizations_enabled == "true")
    # External IDs are not credentials, but still keep them out of terminal logs.
    fd = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(params, stream)
    print(f'    account scope: {params[0]["ParameterValue"]}'
          + (" (preserved from running ingester)" if stack else " (from config.yaml)"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not echo provider exceptions that might contain environment values.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"ERROR: could not preserve deployment account scope: {message}", file=sys.stderr)
        sys.exit(2)
