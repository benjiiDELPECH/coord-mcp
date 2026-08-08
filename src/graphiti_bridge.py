"""
Best-effort bridge to the Graphiti MCP server for cross-session decision memory.

Mirrors gitnexus_bridge.py's philosophy exactly, applied to the other half of
"don't lose the thread on a vertical": checkin's file-overlap conflict detection
tells you who else is editing the same code RIGHT NOW; this tells you what was
already DECIDED about the topic, in a prior session, possibly weeks ago and on
a branch nobody merged. Both gaps produce the same failure — redoing or
contradicting work that already exists — from opposite time horizons.

Never raises: any failure (server down, network, malformed response, timeout)
degrades to an empty result with a warning. Graphiti awareness is a pure
enhancement on checkin/release_work, never a hard dependency — both must
always succeed even without the Graphiti server reachable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

GRAPHITI_URL = "http://localhost:8001/mcp/"
GRAPHITI_TIMEOUT_S = 10

# Canonical group_ids (underscores — RediSearch/FalkorDB FTS treats "-" as negation).
_REPO_TO_GROUP_ID = {
    "alert-immo": "alert_immo",
    "delpech-infra": "delpech_infra",
}


def infer_group_id(repo_path: str) -> str | None:
    """Map a repo path to its Graphiti group_id by known repo-name substrings.

    Returns None for repos with no known Graphiti group — callers must skip
    the Graphiti call entirely in that case rather than guess.
    """
    for repo_name, group_id in _REPO_TO_GROUP_ID.items():
        if repo_name in repo_path:
            return group_id
    return None


async def _search_nodes_async(query: str, group_ids: list[str]) -> tuple[list[dict], str | None]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(GRAPHITI_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "search_nodes",
                {"query": query, "group_ids": group_ids},
            )
            if result.isError:
                text = result.content[0].text if result.content else "unknown error"
                return [], f"graphiti search_nodes error: {text[:300]}"
            import json

            payload = json.loads(result.content[0].text) if result.content else {}
            return payload.get("nodes", []) or payload.get("result", {}).get("nodes", []), None


async def _add_memory_async(name: str, episode_body: str, group_id: str) -> str | None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(GRAPHITI_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "add_memory",
                {"name": name, "episode_body": episode_body, "group_id": group_id},
            )
            if result.isError:
                text = result.content[0].text if result.content else "unknown error"
                return f"graphiti add_memory error: {text[:300]}"
            return None


def _run_sync(coro):
    """Run an async call from sync code, whether or not a loop is already running.

    coord-mcp's FastMCP server (streamable-http) executes tool functions on its
    own event loop thread — a plain `asyncio.run()` here raises
    "cannot be called from a running event loop". Detect that case and run the
    coroutine on a fresh loop in a separate thread instead; when no loop is
    running (e.g. under pytest), `asyncio.run()` works directly and is cheaper.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def search_prior_decisions(topic: str, group_id: str) -> dict:
    """Query Graphiti for entities/decisions matching a checkin's topic.

    Best-effort — never raises. Returns {"nodes": [...], "warnings": [...]}.
    An empty result with a warning means "couldn't check", not "nothing found" —
    callers must not treat a warning as a clean bill of health.
    """
    if not topic or not topic.strip():
        return {"nodes": [], "warnings": []}

    try:
        nodes, warning = _run_sync(
            asyncio.wait_for(_search_nodes_async(topic, [group_id]), timeout=GRAPHITI_TIMEOUT_S)
        )
        warnings = [warning] if warning else []
        return {"nodes": nodes, "warnings": warnings}
    except Exception as e:  # noqa: BLE001 — best-effort by design, any failure degrades silently
        return {"nodes": [], "warnings": [f"graphiti unreachable ({type(e).__name__}: {e})"]}


def persist_outcome(title: str, outcome_summary: str, group_id: str) -> dict:
    """Push a release_work outcome as a Graphiti episode.

    Best-effort — never raises. Returns {"persisted": bool, "warnings": [...]}.
    """
    if not outcome_summary or not outcome_summary.strip():
        return {"persisted": False, "warnings": ["no outcome_summary provided — nothing to persist"]}

    try:
        error = _run_sync(
            asyncio.wait_for(
                _add_memory_async(name=f"release_work: {title}", episode_body=outcome_summary, group_id=group_id),
                timeout=GRAPHITI_TIMEOUT_S,
            )
        )
        if error:
            return {"persisted": False, "warnings": [error]}
        return {"persisted": True, "warnings": []}
    except Exception as e:  # noqa: BLE001 — best-effort by design, any failure degrades silently
        return {"persisted": False, "warnings": [f"graphiti unreachable ({type(e).__name__}: {e})"]}
