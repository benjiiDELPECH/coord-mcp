"""Tests for _resolve_priority (Wait-Die) and plan_parallel_waves (greedy graph coloring).

None of these tests declare scope_symbols, so the default null_resolver (see
scope_resolver.py) is exercised as-is — no resolver needs to be wired. graphiti_bridge
needs no mocking either since repo_path="." never matches a known repo name in
_REPO_TO_GROUP_ID, so it degrades to an empty result with zero network calls.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch


def _fake_gh(cmd, **kwargs):
    if cmd[:3] == ["gh", "repo", "view"]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not a repo")
    raise AssertionError(f"unexpected gh call: {cmd}")


def test_no_conflicts_resolution_is_proceed(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="Solo work", scope_files=["a.py"], agent_id="a")

    assert result["resolution"] == {"strategy": "none", "you_should": "PROCEED", "wait_on": []}


def test_conflict_resolution_is_wait_or_abort_oldest_first(temp_db):
    """Three agents declare overlapping scope in order A, B, C. C's conflicts list
    (and resolution.wait_on) must be sorted oldest-first: A before B."""
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        r_a = wi.checkin(repo_path=".", title="A", scope_files=["shared.py"], agent_id="agent-a")
        r_b = wi.checkin(repo_path=".", title="B", scope_files=["shared.py"], agent_id="agent-b")
        r_c = wi.checkin(repo_path=".", title="C", scope_files=["shared.py"], agent_id="agent-c")

    assert r_c["resolution"]["strategy"].startswith("wait-die")
    assert r_c["resolution"]["you_should"] == "WAIT_OR_ABORT"
    assert r_c["resolution"]["wait_on"] == [r_a["work_item_id"], r_b["work_item_id"]]


def test_plan_parallel_waves_no_conflicts_all_run_together(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.checkin(repo_path=".", title="A", scope_files=["a.py"], agent_id="agent-a")
        wi.checkin(repo_path=".", title="B", scope_files=["b.py"], agent_id="agent-b")
        wi.checkin(repo_path=".", title="C", scope_files=["c.py"], agent_id="agent-c")

        plan = wi.plan_parallel_waves(repo_path=".")

    assert plan["wave_count"] == 1
    assert len(plan["waves"][0]["can_run_in_parallel"]) == 3


def test_plan_parallel_waves_fully_connected_needs_n_waves(temp_db):
    """Every agent conflicts with every other agent (all touch the same file):
    no two can share a wave, so wave_count must equal the item count."""
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        for name in ("A", "B", "C"):
            wi.checkin(repo_path=".", title=name, scope_files=["shared.py"], agent_id=name)

        plan = wi.plan_parallel_waves(repo_path=".")

    assert plan["wave_count"] == 3
    for wave in plan["waves"]:
        assert len(wave["can_run_in_parallel"]) == 1


def test_plan_parallel_waves_chain_reuses_wave_across_gap(temp_db):
    """A conflicts with B, B conflicts with C, A does NOT conflict with C.
    Optimal coloring needs only 2 waves: {A, C} then {B} — proves the greedy
    algorithm actually reuses an earlier wave rather than always incrementing."""
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.checkin(repo_path=".", title="A", scope_files=["x.py"], agent_id="A")
        wi.checkin(repo_path=".", title="B", scope_files=["x.py", "y.py"], agent_id="B")
        wi.checkin(repo_path=".", title="C", scope_files=["y.py"], agent_id="C")

        plan = wi.plan_parallel_waves(repo_path=".")

    assert plan["wave_count"] == 2
    wave1_agents = {i["agent_id"] for i in plan["waves"][0]["can_run_in_parallel"]}
    wave2_agents = {i["agent_id"] for i in plan["waves"][1]["can_run_in_parallel"]}
    assert wave1_agents == {"A", "C"}
    assert wave2_agents == {"B"}
