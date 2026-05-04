"""
emcp client — wraps raw MCP responses through the emcp bridge
and returns enriched typed context for injection into Kamil's prompt.
"""

import httpx
import json
from typing import Any

EMCP_URL = "http://127.0.0.1:3001"
_last_context = None


def is_available() -> bool:
    try:
        r = httpx.get(f"{EMCP_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def process(tool_name: str, server_id: str, content: Any) -> dict:
    """
    Process a single raw MCP response through emcp.
    Returns enriched context dict.
    """
    try:
        r = httpx.post(
            f"{EMCP_URL}/process",
            json={"toolName": tool_name, "serverId": server_id, "content": content},
            timeout=10
        )
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def process_many(responses: list[dict]) -> dict:
    """
    Process multiple MCP responses and get cross-linked context.
    Each item: {"toolName": str, "serverId": str, "content": any}
    """
    try:
        r = httpx.post(
            f"{EMCP_URL}/process_many",
            json={"responses": responses},
            timeout=15
        )
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def clear():
    """Reset the emcp context registry."""
    try:
        httpx.post(f"{EMCP_URL}/clear", timeout=5)
    except Exception:
        pass


def format_for_prompt(ctx: dict, max_nodes: int = 20) -> str:
    """
    Format enriched emcp context for injection into system prompt.
    Returns a concise string Kamil can reason about.
    """
    if not ctx.get("success") or not ctx.get("nodes"):
        return ""

    nodes = ctx["nodes"]
    meta = ctx.get("meta", {})

    lines = [f"## External Context ({meta.get('totalNodes', len(nodes))} nodes)"]

    count = 0
    for node_id, node in nodes.items():
        if count >= max_nodes:
            lines.append(f"... and {len(nodes) - max_nodes} more nodes")
            break

        source = node.get("source", "unknown")
        ntype = node.get("type", "UNKNOWN")
        name = node.get("name", node_id)
        confidence = node.get("confidence", {}).get("overall", 0)

        line = f"- [{source}] {ntype}: {name}"

        # Add spatial if present
        spatial = node.get("spatial")
        if spatial:
            line += f" at ({spatial['x']},{spatial['y']}) {spatial['width']}×{spatial['height']}"

        # Add style hints
        style = node.get("style", {})
        if style.get("fill"):
            line += f" fill={style['fill']}"

        # Add linked nodes
        linked = node.get("linkedNodes", [])
        if linked:
            link_names = [f"{l['source']}:{l['nodeId']}" for l in linked[:2]]
            line += f" → links: {', '.join(link_names)}"

        # Flag low confidence
        if confidence < 0.5:
            line += f" (confidence: {confidence:.2f})"

        lines.append(line)
        count += 1

    # Add errors if any
    errors = ctx.get("errors", [])
    if errors:
        lines.append(f"\n⚠ Adapter errors: {', '.join(e['message'] for e in errors[:3])}")

    return "\n".join(lines)