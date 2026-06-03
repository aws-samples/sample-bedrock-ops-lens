#!/usr/bin/env python3
"""
Bedrock Ops Review MCP — Customer-facing version using public AWS APIs.
Collects data via boto3, runs analysis, returns report + assessment prompt.
"""
from mcp.server.fastmcp import FastMCP
import os, sys
from datetime import datetime
from typing import Optional

mcp = FastMCP("Bedrock Ops Review")
LIB_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_BASE = "/tmp" if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else LIB_DIR
sys.path.insert(0, LIB_DIR)


@mcp.tool()
async def run_bedrock_ops_review(
    accounts: str,
    regions: Optional[str] = "us-east-1,us-west-2",
    days: Optional[int] = 14,
) -> str:
    """Run automated Bedrock operational review — collects metrics via public AWS APIs,
    runs analysis, returns report + assessment prompt.

    Args:
        accounts: Comma-separated AWS account IDs
        regions: Comma-separated AWS regions (default: us-east-1,us-west-2)
        days: CloudWatch metrics lookback period in days (default: 14, max: 455)
    """
    import re
    if not re.match(r'^[\d,\s]+$', accounts):
        return "❌ Invalid accounts format. Use comma-separated account IDs (digits only)."
    if not re.match(r'^[a-z0-9,\-]+$', regions):
        return "❌ Invalid regions format. Use comma-separated region names."

    account_list = [a.strip() for a in accounts.split(",")]
    region_list = [r.strip() for r in regions.split(",")]
    days = min(days or 14, 455)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(OUTPUT_BASE, f"review_{timestamp}")

    # Step 1: Collect via public APIs
    from collect_public import collect_all
    try:
        collect_all(account_list, region_list, output_dir, days=days)
    except Exception as e:
        return f"❌ Data collection failed: {e}"

    # Step 2: Analyze
    from analyze import run as analyze_run
    report_path = os.path.join(output_dir, "analysis-report.txt")
    try:
        analyze_run(output_dir, report_path)
    except Exception as e:
        return f"❌ Analysis failed: {e}"

    # Read report
    with open(report_path, encoding="utf-8") as f:
        report = f.read()

    # Read skill prompt
    prompt_path = os.path.join(LIB_DIR, "skills", "bedrock_ops_review.md")
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read()

    return (
        f"✅ Data collection and analysis complete. Output: {output_dir}/\n\n"
        f"--- ASSESSMENT PROMPT ---\n\n{prompt}\n\n"
        f"--- METRICS REPORT ---\n\n{report}\n\n"
        f"--- INSTRUCTION ---\n\n"
        f"Now follow the assessment prompt above using the metrics report as input. "
        f"Generate the full operational assessment."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
