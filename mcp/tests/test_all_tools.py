"""Functional smoke test: exercise every backend tool in both modes.

Direct (Tier A) — boto3 against the running creds. Skipped per-tool when
                  the AWS API rejects (e.g. CE not enabled in account).

Deployed (Tier B/C) — set BEDROCK_LENS_API + BEDROCK_LENS_USER + _PASSWORD
                       and re-run; HTTP backend is exercised end-to-end.

Run:
    mcp/.venv/bin/python mcp/tests/test_all_tools.py
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

# Make the package importable when running from repo root or mcp/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bedrock_lens_mcp.backends import auto_select


def _short(payload: Any, n: int = 220) -> str:
    s = json.dumps(payload, default=str)
    return s if len(s) <= n else s[:n] + "...(truncated)"


def run_one(label: str, fn) -> tuple[bool, str]:
    try:
        out = fn()
        return True, _short(out)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    backend = auto_select()
    mode = backend.mode
    print(f"\n{'='*70}\n  Bedrock Ops Lens MCP — backend mode: {mode}\n{'='*70}\n")

    tests = [
        ("health",            lambda: backend.health()),
        ("overview_summary",  lambda: backend.overview_summary(days=14)),
        ("cost_summary",      lambda: backend.cost_summary(days=30)),
        ("cost_by_account",   lambda: backend.cost_by_account(days=30)),
        ("cost_by_model",     lambda: backend.cost_by_model(days=30, top_n=10)),
        ("quotas",            lambda: backend.quotas(region=None)),
        ("model_insights",    lambda: backend.model_insights(days=14, top_n=10)),
        ("model_lifecycle",   lambda: backend.model_lifecycle()),
        ("errors_summary",    lambda: backend.errors_summary(days=14)),
        ("latency_summary",   lambda: backend.latency_summary(days=14)),
        ("ops_review",        lambda: backend.ops_review_synthesize(days=14)),
        ("by_user",           lambda: backend.by_user(days=14, top_n=10, group_by="group")),
        ("agents",            lambda: backend.agents(days=14)),
        ("compliance",        lambda: backend.compliance(days=14)),
        ("governance",        lambda: backend.governance(days=14)),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        ok, msg = run_one(name, fn)
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name:<22}  {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n  PASSED {passed}/{len(tests)}    FAILED {failed}/{len(tests)}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
