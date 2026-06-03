"""Lambda handler for Bedrock Ops Review MCP Server using Function URL.
Handles the MCP JSON-RPC protocol directly."""
import sys
import os
import json
import asyncio

LAMBDA_TASK_ROOT = os.environ.get("LAMBDA_TASK_ROOT", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LAMBDA_TASK_ROOT)

import mcp_server  # registers tools on mcp_server.mcp

mcp = mcp_server.mcp


async def _handle_jsonrpc(body: dict) -> dict:
    """Process a single JSON-RPC request through the MCP server."""
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    if method == "initialize":
        from mcp.types import InitializeResult, ServerCapabilities, Implementation
        result = InitializeResult(
            protocolVersion="2025-03-26",
            capabilities=ServerCapabilities(
                tools={"listChanged": False},
                resources={"subscribe": False, "listChanged": False},
                prompts={"listChanged": False},
            ),
            serverInfo=Implementation(name="Bedrock Ops Review", version="1.0.0"),
        )
        return {"jsonrpc": "2.0", "id": req_id, "result": result.model_dump()}

    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        tools = await mcp.list_tools()
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [t.model_dump() for t in tools]}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        content, structured = await mcp.call_tool(name, arguments)
        result = {"content": [c.model_dump() for c in content]}
        if structured is not None:
            result["structuredContent"] = structured
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def handler(event, context):
    """Lambda Function URL handler."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")

    if method != "POST":
        return {"statusCode": 405, "body": json.dumps({"error": "Method not allowed"})}

    body = event.get("body", "{}")
    if isinstance(body, str):
        body = json.loads(body)

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(_handle_jsonrpc(body))
    loop.close()

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result, default=str),
    }
