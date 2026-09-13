"""Tests for src/graphiti_bridge.py — the MCP server stays mocked, never called for real."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import src.graphiti_bridge as gb


async def _echo(value):
    return value


def test_run_sync_without_running_loop_uses_asyncio_run():
    """Plain call (e.g. under pytest) — no event loop running, asyncio.run works directly."""
    assert gb._run_sync(_echo(42)) == 42


def test_run_sync_inside_running_loop_falls_back_to_thread():
    """Regression: coord-mcp's FastMCP server runs tool calls on its own event loop —
    a naive asyncio.run() here raises 'cannot be called from a running event loop'.
    This is the bug caught live on 2026-08-08 (first checkin after wiring the bridge)."""

    async def _call_run_sync_from_inside_loop():
        return gb._run_sync(_echo(7))

    assert asyncio.run(_call_run_sync_from_inside_loop()) == 7


def test_infer_group_id_known_repos():
    assert gb.infer_group_id("/Users/bdelpech/dev/github/alert-immo") == "alert_immo"
    assert gb.infer_group_id("/Users/bdelpech/dev/github/delpech-infra") == "delpech_infra"


def test_infer_group_id_unknown_repo_returns_none():
    assert gb.infer_group_id("/Users/bdelpech/dev/github/some-other-repo") is None


def test_search_prior_decisions_empty_topic_short_circuits():
    with patch.object(gb, "_search_nodes_async") as mock_search:
        result = gb.search_prior_decisions("", "alert_immo")
    mock_search.assert_not_called()
    assert result == {"nodes": [], "warnings": []}


def test_search_prior_decisions_success():
    fake_nodes = [{"name": "ADR-143", "summary": "socle ontologique"}]
    with patch.object(gb, "_search_nodes_async", new=AsyncMock(return_value=(fake_nodes, None))):
        result = gb.search_prior_decisions("PV d'AG", "alert_immo")

    assert result["nodes"] == fake_nodes
    assert result["warnings"] == []


def test_search_prior_decisions_server_error_degrades_to_warning():
    with patch.object(gb, "_search_nodes_async", new=AsyncMock(return_value=([], "graphiti search_nodes error: boom"))):
        result = gb.search_prior_decisions("PV d'AG", "alert_immo")

    assert result["nodes"] == []
    assert len(result["warnings"]) == 1
    assert "boom" in result["warnings"][0]


def test_search_prior_decisions_server_unreachable_never_raises():
    """Server down / network error — checkin must never crash on this."""
    with patch.object(gb, "_search_nodes_async", new=AsyncMock(side_effect=ConnectionError("refused"))):
        result = gb.search_prior_decisions("PV d'AG", "alert_immo")

    assert result["nodes"] == []
    assert len(result["warnings"]) == 1
    assert "unreachable" in result["warnings"][0]


def test_search_prior_decisions_timeout_never_raises():
    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(9999)

    with patch.object(gb, "_search_nodes_async", new=_hang):
        with patch.object(gb, "GRAPHITI_TIMEOUT_S", 0.01):
            result = gb.search_prior_decisions("PV d'AG", "alert_immo")

    assert result["nodes"] == []
    assert len(result["warnings"]) == 1


def test_persist_outcome_empty_summary_short_circuits():
    with patch.object(gb, "_add_memory_async") as mock_add:
        result = gb.persist_outcome("title", "", "alert_immo")
    mock_add.assert_not_called()
    assert result["persisted"] is False


def test_persist_outcome_success():
    with patch.object(gb, "_add_memory_async", new=AsyncMock(return_value=None)):
        result = gb.persist_outcome("release_work: PV d'AG", "boucle e2e livrée", "alert_immo")

    assert result == {"persisted": True, "warnings": []}


def test_persist_outcome_server_unreachable_never_raises():
    with patch.object(gb, "_add_memory_async", new=AsyncMock(side_effect=ConnectionError("refused"))):
        result = gb.persist_outcome("title", "outcome", "alert_immo")

    assert result["persisted"] is False
    assert "unreachable" in result["warnings"][0]
