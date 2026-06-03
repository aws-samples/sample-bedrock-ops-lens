#!/bin/bash
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/venv"

echo "Installing Bedrock Ops Review MCP..."

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet "mcp[cli]>=1.3.0" boto3

# Configure MCP for Kiro CLI
MCP_CONFIG="$HOME/.kiro/settings/mcp.json"
mkdir -p "$(dirname "$MCP_CONFIG")"

if [ -f "$MCP_CONFIG" ]; then
  python3 -c "
import json
with open('$MCP_CONFIG') as f:
    cfg = json.load(f)
cfg.setdefault('mcpServers', {})['bedrock-ops-review-mcp'] = {
    'command': '$VENV_DIR/bin/python3',
    'args': ['$REPO_DIR/mcp_server.py']
}
with open('$MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
"
else
  cat > "$MCP_CONFIG" << EOFMCP
{
  "mcpServers": {
    "bedrock-ops-review-mcp": {
      "command": "$VENV_DIR/bin/python3",
      "args": ["$REPO_DIR/mcp_server.py"]
    }
  }
}
EOFMCP
fi

echo ""
echo "Done. MCP server configured."
echo ""
echo "Prerequisites:"
echo "  - AWS credentials configured (aws configure or SSO)"
echo "  - Permissions: bedrock:List*, service-quotas:List*, cloudwatch:GetMetricStatistics"
echo ""
echo "Next steps:"
echo "  1. Restart your AI assistant (Kiro CLI, Amazon Q, etc.)"
echo "  2. Ask: Run ops review for accounts <ACCOUNT_IDS> in regions us-east-1,us-west-2"
