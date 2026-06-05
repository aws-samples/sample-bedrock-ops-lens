# Bedrock Ops Lens — MCP

A Model Context Protocol server that exposes the same Bedrock observability insights as the dashboard, but inside your IDE. Claude Code, Cursor, Kiro (CLI or IDE), or anything that speaks MCP.

The MCP **does not duplicate any backend code**. It's a thin client that auto-detects whether the deployed dashboard is reachable and dispatches every tool through the right backend:

| Mode | When | Data path |
|---|---|---|
| **Tier A — direct** | None of the deploy env vars set | boto3 → CloudWatch / Cost Explorer / Service Quotas / Bedrock APIs in your current account |
| **Tier B/C — SigV4 (recommended)** | `BEDROCK_LENS_FUNCTION_URL` set | Direct SigV4 to Lambda Function URL using your AWS creds. **No password.** Bypasses CloudFront. |
| **Tier B/C — Cognito** | `BEDROCK_LENS_API` + `BEDROCK_LENS_USER` + `BEDROCK_LENS_PASSWORD` set | HTTPS → CloudFront → deployed `/api/*`. Same path the browser uses. |

**Recommendation for CLI/MCP users with admin AWS creds:** use the SigV4 mode. Just set `BEDROCK_LENS_FUNCTION_URL` to the value of the `BackendLambdaUrl` CFN output. Lambda's resource policy allows IAM principals in your own account to invoke directly — no Cognito password required.

## Tools

Every tool is registered in both modes — same name, same args, same response shape (with a `_note` field on Tier A tools whose answers are degraded).

| Tool | What it answers |
|---|---|
| `health` | Connectivity check: which mode, which account |
| `overview_summary` | Total invocations, tokens, errors, throttles in the window |
| `cost_summary` | Total Bedrock $ + daily breakdown |
| `cost_by_account` | Top accounts by Bedrock spend |
| `cost_by_model` | Top models by spend (Tier B+) or volume (Tier A) |
| `quotas` | Service Quotas snapshot for AWS/Bedrock + bedrock-runtime |
| `model_insights` | Per-model invocations/tokens — the "what are we using" answer |
| `model_lifecycle` | Foundation models + their Active/Legacy/EOL status |
| `errors_summary` | Invocation errors and throttles |
| `latency_summary` | p50/p90/p99 by model (Tier B+ only) |
| `ops_review` | LLM-synthesized exec brief (Tier B+ only) |

## Install

The MCP installs a `bedrock-lens-mcp` command on your `$PATH`. Pick one:

```bash
cd mcp

# Recommended on macOS (Homebrew Python is PEP-668-protected, so plain
# `pip install` fails). pipx handles its own venv.
pipx install -e .

# Or with uv
uv pip install -e .

# Or in a manual venv
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

## Configure your IDE

### Claude Code

Tier A (no deployment, uses your current AWS creds):

```bash
claude mcp add bedrock-lens-mcp -- bedrock-lens-mcp
```

Tier B/C — SigV4 (recommended, no password):

```bash
# Get the Function URL from your CFN stack
FN_URL=$(aws cloudformation describe-stacks --stack-name BedrockOpsLens-<suffix> \
  --query 'Stacks[0].Outputs[?OutputKey==`BackendLambdaUrl`].OutputValue' --output text)

claude mcp add bedrock-lens-mcp \
  --env BEDROCK_LENS_FUNCTION_URL="$FN_URL" \
  -- bedrock-lens-mcp
```

Tier B/C — Cognito password (only if you need to share it with someone who has no AWS access):

```bash
claude mcp add bedrock-lens-mcp \
  --env BEDROCK_LENS_API=https://<your-distribution>.cloudfront.net \
  --env BEDROCK_LENS_USER=you@yourdomain.com \
  --env BEDROCK_LENS_PASSWORD="$BEDROCK_LENS_PASSWORD" \
  -- bedrock-lens-mcp
```

### Cursor / Kiro / VS Code

Add to `~/.cursor/mcp.json` (or equivalent):

```json
{
  "mcpServers": {
    "bedrock-lens-mcp": {
      "command": "bedrock-lens-mcp",
      "env": {
        "BEDROCK_LENS_API":      "https://<your-distribution>.cloudfront.net",
        "BEDROCK_LENS_USER":     "you@yourdomain.com",
        "BEDROCK_LENS_PASSWORD": "${BEDROCK_LENS_PASSWORD}"
      }
    }
  }
}
```

Drop the `env` block entirely for Tier A (direct AWS).

## Verify

In your IDE, ask: *"Run the bedrock-lens health check."*

The MCP responds with which mode it picked and where it's connected.

Then ask: *"What was our Bedrock spend last 30 days?"* — the LLM picks `cost_summary(days=30)`, calls it, and you'll see real `$`.

## Auth notes (Tier B/C)

- The MCP signs in via `/api/auth/signin` once at first request, caches the cookie, and re-auths on 401.
- The user must have a **permanent password** in the user pool (sign in to the dashboard once and complete the FORCE_CHANGE_PASSWORD flow if you got a temp password).
- Admin users (those in the `bedrock-lens-admins` group) can see all accounts; non-admins are scoped to their own account by the dashboard's IAM.

## Tier A constraints

Direct mode skips:
- **per-tag attribution** (no invocation-log ingestion → no `requestMetadata` join)
- **multi-account aggregation** (limited to whatever your current creds can `AssumeRole` into; default is just your own account)
- **per-model $ from Cost Explorer** (CE groups by service, not by Bedrock modelId — that's why the dashboard ingests invocation logs to bridge)
- **historical depth beyond what live AWS APIs return** (CW: 14 days at 5-min, 455 days at 1-hr; CE: 12 months)
- **latency percentiles** (CW Bedrock metrics aren't broken down to p50/p90/p99 by model)
- **ops_review LLM synthesis** (calls Bedrock with the dashboard's curated context)

When a Tier A tool can't fully answer, the response includes a `_note` explaining what to deploy to get the full answer.

## Source

- `src/bedrock_lens_mcp/server.py` — tool registration + dispatch
- `src/bedrock_lens_mcp/backends.py` — `HttpBackend` and `DirectBackend`
- `src/bedrock_lens_mcp/api_client.py` — auth + signed HTTP calls to `/api/*`
- `src/bedrock_lens_mcp/direct_collector.py` — boto3 paths for Tier A
