"""Tests for the global CI-concurrency gate (established 2026-08-27, post Forgejo OOM).

Covers:
  - triggers_ci auto-detection from scope_files (workflow files, compiled/tested
    extensions) and explicit override in either direction.
  - checkin() surfaces ci_gate as a signal without ever blocking the declare.
  - checkout_work() ENFORCES the gate: under the threshold -> PROCEED and status
    transitions to checked_out; at the threshold -> WAIT and status does NOT
    transition (no slot consumed).
  - The counter is GLOBAL across repos, not scoped to a single repo_path.
  - release_work() frees a slot for the next queued checkout_work.

Uses real tmp_path directories (non-git) as repo_path, matching the existing
pattern in test_checkout.py's `test_checkout_with_explicit_diff_files_bypasses_detection`:
no subprocess mocking needed because a non-git directory makes `gh repo view` /
`git ...` fail fast and gracefully (repo_slug=None, diff detection skipped via
explicit diff_files). All checkout() calls below pass explicit diff_files so the
auto-detect git cascade is never exercised.
"""

from __future__ import annotations

import importlib
import subprocess
from unittest.mock import patch


def _fake_gh(cmd, **kwargs):
    """`repo_path="."` resolves to coord-mcp's own repo, which has a REAL
    GitHub remote — without this, `gh repo view` / `gh issue list` inside
    checkin() would make real network calls. Same fake used by
    test_priority_and_waves.py: `gh repo view` fails, so checkin() treats
    repo_slug as unknown and skips the similar-issues search entirely."""
    if cmd[:3] == ["gh", "repo", "view"]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not a repo")
    raise AssertionError(f"unexpected gh call: {cmd}")


def _reload_checkout():
    """checkout.py binds `connection` from db.py at import time — must reload
    after temp_db's env var + db/work_items reload so it targets the fresh
    per-test SQLite file. Same pattern as test_checkout.py."""
    import src.checkout as co
    importlib.reload(co)
    return co


# ────────────────────────────────────────────────────────────────────
# triggers_ci auto-detection
# ────────────────────────────────────────────────────────────────────


def test_triggers_ci_auto_detected_true_for_compiled_source(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="Backend change", scope_files=["src/Foo.kt"])
    assert result["triggers_ci"] is True


def test_triggers_ci_auto_detected_true_for_workflow_file(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(
            repo_path=".", title="CI change", scope_files=[".github/workflows/ci.yml"]
        )
    assert result["triggers_ci"] is True


def test_triggers_ci_auto_detected_false_for_docs_only(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="Docs change", scope_files=["docs/notes.md"])
    assert result["triggers_ci"] is False


def test_triggers_ci_explicit_override_wins_over_heuristic(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        # .py would auto-detect True — explicit False must win.
        result_false = wi.checkin(
            repo_path=".", title="Local script, not CI-tested",
            scope_files=["scratch/one_off.py"], triggers_ci=False,
        )
        # .md would auto-detect False — explicit True must win.
        result_true = wi.checkin(
            repo_path=".", title="Docs that actually gate a docs-CI job",
            scope_files=["docs/notes.md"], triggers_ci=True,
        )
    assert result_false["triggers_ci"] is False
    assert result_true["triggers_ci"] is True


# ────────────────────────────────────────────────────────────────────
# checkin() ci_gate — signal only, never blocks the declare
# ────────────────────────────────────────────────────────────────────


def test_checkin_ci_gate_proceed_when_no_active_ci_items(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="Solo CI work", scope_files=["src/a.py"])
    assert result["ci_gate"]["you_should"] == "PROCEED"
    assert result["ci_gate"]["wait_on"] == []
    assert result["ci_gate"]["active_ci_count"] == 0
    assert result["ci_gate"]["limit"] == 2  # default


def test_checkin_ci_gate_not_ci_for_non_triggering_item(temp_db):
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="Docs only", scope_files=["README.md"])
    assert result["ci_gate"]["strategy"] == "not-ci"
    assert result["ci_gate"]["you_should"] == "PROCEED"


def test_ci_concurrency_limit_configurable_via_env(temp_db, monkeypatch):
    monkeypatch.setenv("COORD_MCP_CI_CONCURRENCY_LIMIT", "5")
    _db, wi = temp_db
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(repo_path=".", title="CI work", scope_files=["src/a.py"])
    assert result["ci_gate"]["limit"] == 5


# ────────────────────────────────────────────────────────────────────
# checkout_work() enforcement — under threshold PROCEEDs, at threshold WAITs
# ────────────────────────────────────────────────────────────────────


def test_checkout_proceeds_and_consumes_slot_under_threshold(temp_db, tmp_path):
    _db, wi = temp_db
    co = _reload_checkout()

    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    item = wi.checkin(repo_path=str(repo_dir), title="First CI item", scope_files=["src/a.py"])
    wi_id = item["work_item_id"]
    assert item["triggers_ci"] is True

    result = co.checkout(wi_id, diff_files=["src/a.py"])

    assert result["triggers_ci"] is True
    assert result["ci_gate"]["you_should"] == "PROCEED"
    assert result["status"] == "checked_out"
    assert result["ready_to_merge"] is True
    assert wi.get_work_item(wi_id)["status"] == "checked_out"


def test_checkout_waits_at_threshold_and_does_not_consume_slot(temp_db, tmp_path):
    """Default limit is 2: first two CI-triggering checkouts succeed, the third
    (any repo) is told to WAIT and its status is NOT advanced to checked_out."""
    _db, wi = temp_db
    co = _reload_checkout()

    ids = []
    for i, name in enumerate(("repo-a", "repo-b", "repo-c")):
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        item = wi.checkin(
            repo_path=str(repo_dir), title=f"CI item {i}", scope_files=[f"src/{i}.py"],
        )
        ids.append(item["work_item_id"])

    r1 = co.checkout(ids[0], diff_files=["src/0.py"])
    r2 = co.checkout(ids[1], diff_files=["src/1.py"])
    r3 = co.checkout(ids[2], diff_files=["src/2.py"])

    assert r1["status"] == "checked_out"
    assert r1["ci_gate"]["you_should"] == "PROCEED"
    assert r2["status"] == "checked_out"
    assert r2["ci_gate"]["you_should"] == "PROCEED"

    # Third hits the cap (limit=2, two already checked_out).
    assert r3["ci_gate"]["you_should"] == "WAIT"
    assert sorted(r3["ci_gate"]["wait_on"]) == sorted([ids[0], ids[1]])
    assert r3["ci_gate"]["active_ci_count"] == 2
    assert r3["ci_gate"]["limit"] == 2
    assert r3["ready_to_merge"] is False
    assert any("CI concurrency cap" in b for b in r3["blockers"])
    # Status must NOT have transitioned — no slot consumed by the waiting item.
    assert r3["status"] == "declared"
    assert wi.get_work_item(ids[2])["status"] == "declared"


def test_ci_gate_is_global_across_repos_not_per_repo(temp_db, tmp_path):
    """The cap must NOT be scoped per repo_path — that's exactly the boundary
    the 2026-08-26 Forgejo OOM ignored. Three different repos, same global cap."""
    _db, wi = temp_db
    co = _reload_checkout()

    repo_x = tmp_path / "alert-immo"
    repo_y = tmp_path / "delpech-infra"
    repo_z = tmp_path / "coord-mcp-itself"
    for d in (repo_x, repo_y, repo_z):
        d.mkdir()

    item_x = wi.checkin(repo_path=str(repo_x), title="X", scope_files=["a.kt"])
    item_y = wi.checkin(repo_path=str(repo_y), title="Y", scope_files=["b.kt"])
    item_z = wi.checkin(repo_path=str(repo_z), title="Z", scope_files=["c.kt"])

    r_x = co.checkout(item_x["work_item_id"], diff_files=["a.kt"])
    r_y = co.checkout(item_y["work_item_id"], diff_files=["b.kt"])
    r_z = co.checkout(item_z["work_item_id"], diff_files=["c.kt"])

    assert r_x["status"] == "checked_out"
    assert r_y["status"] == "checked_out"
    # Different repo than x/y, still hits the GLOBAL cap.
    assert r_z["ci_gate"]["you_should"] == "WAIT"
    assert r_z["status"] != "checked_out"


def test_checkout_non_ci_item_never_gated(temp_db, tmp_path):
    """A non-CI-triggering item must PROCEED regardless of how many CI-active
    items already occupy the cap — the gate only applies to triggers_ci items."""
    _db, wi = temp_db
    co = _reload_checkout()

    # Fill the cap (limit=2) with CI-triggering items.
    for i, name in enumerate(("repo-a", "repo-b")):
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        item = wi.checkin(repo_path=str(repo_dir), title=f"CI {i}", scope_files=[f"{i}.py"])
        co.checkout(item["work_item_id"], diff_files=[f"{i}.py"])

    repo_docs = tmp_path / "repo-docs"
    repo_docs.mkdir()
    docs_item = wi.checkin(repo_path=str(repo_docs), title="Docs", scope_files=["README.md"])
    assert docs_item["triggers_ci"] is False

    r_docs = co.checkout(docs_item["work_item_id"], diff_files=["README.md"])
    assert r_docs["ci_gate"]["strategy"] == "not-ci"
    assert r_docs["ci_gate"]["you_should"] == "PROCEED"
    assert r_docs["status"] == "checked_out"


# ────────────────────────────────────────────────────────────────────
# release_work() frees a slot for the next queued checkout_work
# ────────────────────────────────────────────────────────────────────


def test_release_work_frees_a_slot_for_the_next_waiting_checkout(temp_db, tmp_path):
    _db, wi = temp_db
    co = _reload_checkout()

    ids = []
    for i, name in enumerate(("repo-a", "repo-b", "repo-c")):
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        item = wi.checkin(repo_path=str(repo_dir), title=f"CI {i}", scope_files=[f"{i}.py"])
        ids.append(item["work_item_id"])

    r1 = co.checkout(ids[0], diff_files=["0.py"])
    r2 = co.checkout(ids[1], diff_files=["1.py"])
    r3 = co.checkout(ids[2], diff_files=["2.py"])

    assert r1["status"] == "checked_out"
    assert r2["status"] == "checked_out"
    assert r3["ci_gate"]["you_should"] == "WAIT"
    assert r3["status"] != "checked_out"

    # Release one of the two occupying slots.
    release_result = co.release(ids[0], outcome="done, merged")
    assert release_result["status"] == "released"

    # Retry the waiting item's checkout_work — a slot must now be free.
    r3_retry = co.checkout(ids[2], diff_files=["2.py"])
    assert r3_retry["ci_gate"]["you_should"] == "PROCEED"
    assert r3_retry["ci_gate"]["active_ci_count"] == 1  # only ids[1] still checked_out
    assert r3_retry["status"] == "checked_out"
    assert wi.get_work_item(ids[2])["status"] == "checked_out"
