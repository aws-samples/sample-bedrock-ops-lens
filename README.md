# Bedrock Ops Review - AI-Powered Operational Assessment Tool

## Overview

### Problem Statement
Organizations running Amazon Bedrock workloads lack a systematic way to assess their operational posture - quota utilization, model lifecycle status, CRIS configuration, throttling risk, and cost exposure. Manual reviews are time-consuming and error-prone, often missing critical issues like approaching model end-of-life dates or silent throttling from CRIS quota gaps.

### Bedrock Ops Review
This MCP server automates the entire operational review process using public AWS APIs. It collects quota configuration, model inventory, CloudWatch metrics, and logging status across your accounts and regions, then generates a comprehensive assessment report through your AI assistant. All data stays in your account - no external APIs, no data sharing.

### Key Capabilities
- **Model Lifecycle Assessment** - identifies LEGACY models with active traffic, calculates financial exposure, and provides copy-paste-ready upgrade paths with pricing impact
- **CRIS Gap Analysis** - detects mismatches between regional CRIS quotas and Global CRIS quotas that cause silent throttling
- **Financial Impact Analysis** - calculates actual spend per model from CloudWatch token counts, with projected monthly/annual costs and migration cost comparisons
- **Quota Configuration Review** - per-model RPM/TPM analysis across accounts and regions
- **Throttling Detection** - identifies models experiencing throttle events from CloudWatch basic metrics
- **Deterministic Analysis** - all numbers computed by Python, never by the LLM. The AI only writes the narrative assessment from pre-computed facts

## Architecture

```
You ask: "Run ops review for accounts 111111111111 in regions us-east-1,us-west-2"
    |
    v
AI Assistant (Kiro, Amazon Q, Cursor, VS Code, Claude Code)
    |
    +-- bedrock-ops-review-mcp
    |       |
    |       +-- collect_public.py  ->  boto3 API calls (service-quotas, bedrock, cloudwatch)
    |       +-- analyze.py         ->  deterministic number crunching (Python)
    |       +-- returns metrics report + assessment skill prompt
    |
    v
AI generates narrative assessment from the numbers (never counts or calculates)
```

## Prerequisites

### AWS Requirements
- **AWS CLI** configured with credentials (`aws configure`, SSO, or environment variables)
- **IAM Permissions**:

| Permission | Service | Purpose |
|---|---|---|
| `bedrock:ListFoundationModels` | Bedrock | Model inventory and lifecycle status |
| `bedrock:ListInferenceProfiles` | Bedrock | CRIS routing configuration |
| `bedrock:GetModelInvocationLoggingConfiguration` | Bedrock | Logging status check |
| `service-quotas:ListServiceQuotas` | Service Quotas | Applied (non-default) quotas |
| `service-quotas:ListAWSDefaultServiceQuotas` | Service Quotas | Default quota baselines |
| `cloudwatch:ListMetrics` | CloudWatch | Discover models with metrics |
| `cloudwatch:GetMetricStatistics` | CloudWatch | Invocations, throttles, tokens, latency |

### System Requirements
- **Python**: 3.10 or higher
- **Operating System**: macOS, Linux, or Windows with WSL

## MCP Deployment

### MCP Installation Options

#### Option A: Local Install (via pip)

Runs the MCP server locally on your machine, connecting directly to AWS APIs using your local credentials.

> **1) Quick install** (recommended for first time):

```bash
git clone <this-repo>
cd Bedrock-MCP
bash install.sh
```

The install script creates a venv, installs dependencies, and auto-configures Kiro CLI.

> **2) Configure your AI assistant:**

**<img src="https://kiro.dev/favicon.ico" width="16" height="16"> Kiro CLI:**
```bash
# Auto-configured by install.sh, or add manually:
kiro-cli mcp add --force --name bedrock-ops-review-mcp \
  --command "<path-to>/Bedrock-MCP/venv/bin/python3" \
  --args "<path-to>/Bedrock-MCP/mcp_server.py" \
  --env AWS_PROFILE=default
```

**<img src="https://kiro.dev/favicon.ico" width="16" height="16"> Kiro IDE / Cursor / VS Code:**

Add to your MCP settings (`.kiro/settings/mcp.json`, `.cursor/mcp.json`, or VS Code MCP config):
```json
{
  "mcpServers": {
    "bedrock-ops-review-mcp": {
      "command": "<path-to>/Bedrock-MCP/venv/bin/python3",
      "args": ["<path-to>/Bedrock-MCP/mcp_server.py"],
      "env": {
        "AWS_PROFILE": "default"
      }
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add bedrock-ops-review-mcp -- <path-to>/Bedrock-MCP/venv/bin/python3 <path-to>/Bedrock-MCP/mcp_server.py
```

**Amazon Q Developer CLI:**

Go to Settings -> Capabilities -> MCP tab -> "+ Add Server":
- ID: `bedrock-ops-review-mcp`
- Name: `Bedrock Ops Review`
- Command: `<path-to>/Bedrock-MCP/venv/bin/python3`
- Arguments: `<path-to>/Bedrock-MCP/mcp_server.py`
- Timeout (s): `300`

> **Requirements**: Python 3.10+, AWS credentials configured.

#### Option B: Remote Deploy (via Lambda)

Deploys the MCP server as a Lambda function in your AWS account. Team members connect via [mcp-proxy-for-aws](https://pypi.org/project/mcp-proxy-for-aws/) - no local Python or venv needed.

> **1) Deploy to Lambda:**

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. Build and upload the Lambda deployment package to S3
2. Deploy a CloudFormation stack with Lambda (Graviton/ARM64) and Function URL
3. Output the Function URL for client configuration

> **2) Configure your AI assistant:**

**Kiro CLI:**
```bash
kiro-cli mcp add --force --name bedrock-ops-review-mcp \
  --command uvx \
  --args "mcp-proxy-for-aws@latest" "<FUNCTION_URL>mcp" "--service" "lambda" "--profile" "default" "--region" "us-east-1"
```

**Kiro IDE / Cursor / VS Code:**
```json
{
  "mcpServers": {
    "bedrock-ops-review-mcp": {
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "<FUNCTION_URL>mcp",
        "--service", "lambda",
        "--profile", "default",
        "--region", "us-east-1"
      ]
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add bedrock-ops-review-mcp -- uvx mcp-proxy-for-aws@latest <FUNCTION_URL>mcp --service lambda --profile default --region us-east-1
```

**Amazon Q Developer CLI:**

Go to Settings -> Capabilities -> MCP tab -> "+ Add Server":
- ID: `bedrock-ops-review-mcp`
- Name: `Bedrock Ops Review`
- Command: `uvx`
- Arguments: `mcp-proxy-for-aws@latest <FUNCTION_URL>mcp --service lambda --profile default --region us-east-1`
- Timeout (s): `300`

> **Note**: Requires `uv` installed ([install guide](https://docs.astral.sh/uv/getting-started/installation/)). Replace `<FUNCTION_URL>` with the Lambda Function URL from the deployment output.

> **Authentication**: [mcp-proxy-for-aws](https://pypi.org/project/mcp-proxy-for-aws/) runs locally as a client-side bridge that signs requests with AWS SigV4 using your local AWS credentials. The Lambda Function URL uses IAM auth - no separate OAuth or API keys needed.

**Cleanup:**
```bash
aws cloudformation delete-stack --stack-name bedrock-ops-review-mcp --region us-east-1
```

### Verify It Works

After deployment, restart your AI assistant and try:

```
Run ops review for account 111111111111 in regions us-east-1,us-west-2
```

## Usage

### Example Prompts

```
# Standard review (last 14 days of metrics)
Run ops review for account 111111111111 in regions us-east-1,us-west-2

# Extended lookback (up to 455 days)
Run ops review for account 111111111111 in regions us-east-1,us-west-2 for 90 days

# Specific region
Run ops review for account 111111111111 in regions eu-west-1
```

### What You Get

| Section | Description |
|---|---|
| **Model Lifecycle** | LEGACY models with active traffic, invocation counts, financial exposure, upgrade paths with exact model IDs |
| **CRIS Gap Analysis** | Regional vs Global CRIS quota mismatches with gap ratios |
| **Financial Impact** | Per-model spend in the period, projected monthly/annual, migration cost comparison |
| **Per-Model Quotas** | RPM/TPM configuration across accounts and regions |
| **Model Inventory** | Full catalog with ACTIVE/LEGACY status and provider breakdown |
| **Recommendations** | Prioritized actions with effort/impact/cost ratings |
| **Priority Actions** | Ordered table: CRITICAL to HIGH to MEDIUM to LOW |

### Tool Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `accounts` | Yes | - | AWS account ID |
| `regions` | No | `us-east-1,us-west-2` | Comma-separated AWS regions |
| `days` | No | `14` | CloudWatch metrics lookback period (max: 455 days) |

### Multi-Account Reviews

The tool runs using your active AWS credentials. For reviewing multiple accounts:

**Option 1: Run per account (simplest)**
```
# Switch profile and run
export AWS_PROFILE=account-a
Run ops review for account 111111111111 in regions us-east-1,us-west-2
```

**Option 2: Cross-account IAM roles**
Set up `~/.aws/config` with assume-role profiles:
```ini
[profile target-account]
role_arn = arn:aws:iam::222222222222:role/BedrockOpsReviewRole
source_profile = default
```
Then `export AWS_PROFILE=target-account` before running.

## Security and Privacy

- All API calls use your local AWS credentials - no external services
- Data collection runs locally on your machine (Option A) or in your own Lambda (Option B)
- No data is sent to any third party
- The MCP server has no network access beyond AWS APIs
- Bedrock processes all inference requests within [AWS Mantle](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html) - a zero-operator-access architecture

## Project Structure

```
Bedrock-MCP/
+-- mcp_server.py          # MCP server - orchestrates collection + analysis
+-- collect_public.py      # Data collection via public AWS APIs (boto3)
+-- analyze.py             # Deterministic analysis engine (Python)
+-- skills/
|   +-- bedrock_ops_review.md   # Assessment generation skill
|   +-- orchestrator.md         # Multi-step orchestration skill
+-- install.sh             # One-command local installer (Option A)
+-- deploy.sh              # Lambda remote deployment (Option B)
+-- lambda_handler.py      # Lambda Function URL handler
+-- bedrock-ops-review-mcp.yaml  # CloudFormation template
+-- requirements.txt       # Python dependencies (mcp, boto3)
+-- README.md
```

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `AccessDeniedException` | Missing IAM permissions | Add permissions from Prerequisites table |
| `No metrics data` | No Bedrock invocations in the lookback period | Increase `days` parameter (e.g., 30 or 90) |
| `MCP connection failed` | Server crashed on startup | Run `python3 mcp_server.py` directly to see the error |
| Slow execution | Many accounts x regions | Each account-region combo makes ~5 API calls. May take 1-2 minutes |
| `Connection closed` in Amazon Q | print() to stdout corrupts MCP protocol | Ensure all logging goes to stderr (already fixed in current version) |

## Contributing

Issues and PRs welcome. The analysis engine (`analyze.py`) is designed to be extended - add new sections by following the existing pattern of data collection, aggregation, text output.
