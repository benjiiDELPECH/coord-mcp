"""Tests for src/work_items.py — focus on the milestone_number → title bug fix."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess.CompletedProcess for `gh` invocations."""
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_claim_new_resolves_milestone_number_to_title(temp_db):
    """claim_new must call `gh issue create --milestone <title>`, not <number>."""
    _db, wi = temp_db

    # Step 1: checkin with milestone_number=10. We mock subprocess.run because
    # checkin calls `gh repo view` (repo slug detection) and `gh issue list`
    # (similar issue search).
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(stdout="[]")
        raise AssertionError(f"unexpected subprocess.run call during checkin: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        checkin_result = wi.checkin(
            repo_path=".",
            title="Feature with milestone",
            scope_files=["src/foo.py"],
            milestone_number=10,
            agent_id="test-agent",
        )

    work_item_id = checkin_result["work_item_id"]
    assert checkin_result["status"] == "declared"

    # Step 2: claim_new — must resolve milestone 10 → "Sprint Q4" before invoking
    # `gh issue create`.
    captured_create_args: list[list[str]] = []

    def fake_run_claim(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:2] == ["gh", "api"] and "milestones/10" in cmd[2]:
            # Milestone resolution endpoint
            return _fake_completed(stdout="Sprint Q4\n")
        if cmd[:3] == ["gh", "issue", "create"]:
            captured_create_args.append(list(cmd))
            return _fake_completed(stdout="https://github.com/owner/repo/issues/42\n")
        raise AssertionError(f"unexpected subprocess.run call during claim_new: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run_claim):
        claim_result = wi.claim_new(work_item_id=work_item_id, body="body text")

    assert claim_result.get("error") is None, claim_result
    assert claim_result["github_issue_number"] == 42
    assert claim_result["status"] == "claimed"

    # The critical assertion: `gh issue create` received --milestone "Sprint Q4"
    # (the resolved title), NOT --milestone "10" (the raw number).
    assert len(captured_create_args) == 1
    create_cmd = captured_create_args[0]
    assert "--milestone" in create_cmd
    milestone_idx = create_cmd.index("--milestone")
    assert create_cmd[milestone_idx + 1] == "Sprint Q4"
    assert "10" not in create_cmd[milestone_idx + 1:milestone_idx + 2], (
        "claim_new must pass the milestone TITLE, not the raw number, to gh CLI"
    )


def test_claim_new_explicit_error_when_milestone_does_not_exist(temp_db):
    """Unknown milestone_number → explicit error, NOT opaque 'gh issue create failed'."""
    _db, wi = temp_db

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(stdout="[]")
        raise AssertionError(f"unexpected call during checkin: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        checkin_result = wi.checkin(
            repo_path=".",
            title="Feature with ghost milestone",
            milestone_number=999,
            agent_id="test-agent",
        )

    work_item_id = checkin_result["work_item_id"]

    def fake_run_claim(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:2] == ["gh", "api"] and "milestones/999" in cmd[2]:
            # Simulate GH API 404 — gh exits non-zero, no stdout
            return _fake_completed(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        if cmd[:3] == ["gh", "issue", "create"]:
            raise AssertionError(
                "gh issue create must NOT be called when milestone resolution fails"
            )
        raise AssertionError(f"unexpected call during claim_new: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run_claim):
        claim_result = wi.claim_new(work_item_id=work_item_id, body="body text")

    assert "error" in claim_result
    error = claim_result["error"]
    # Must mention the milestone number explicitly so the user can act on it.
    assert "999" in error
    assert "milestone" in error.lower()
    assert error != "gh issue create failed", (
        "Error must be explicit about the cause (milestone not found), "
        "not an opaque gh failure message"
    )


def test_claim_new_no_milestone_skips_resolution(temp_db):
    """When milestone_number is None, no GH API milestone lookup should happen."""
    _db, wi = temp_db

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(stdout="[]")
        raise AssertionError(f"unexpected call: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        checkin_result = wi.checkin(
            repo_path=".",
            title="No milestone here",
            agent_id="test-agent",
        )

    work_item_id = checkin_result["work_item_id"]

    api_calls: list[list[str]] = []
    create_calls: list[list[str]] = []

    def fake_run_claim(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:2] == ["gh", "api"]:
            api_calls.append(list(cmd))
            return _fake_completed(stdout="should-not-be-called\n")
        if cmd[:3] == ["gh", "issue", "create"]:
            create_calls.append(list(cmd))
            return _fake_completed(stdout="https://github.com/owner/repo/issues/7\n")
        raise AssertionError(f"unexpected call: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run_claim):
        claim_result = wi.claim_new(work_item_id=work_item_id)

    assert claim_result.get("error") is None, claim_result
    assert claim_result["github_issue_number"] == 7
    assert api_calls == [], "no milestone lookup expected when milestone_number is None"
    assert len(create_calls) == 1
    assert "--milestone" not in create_calls[0]


def test_claim_new_surfaces_stderr_on_gh_failure(temp_db):
    """When `gh issue create` fails (non-milestone reason), stderr must be surfaced."""
    _db, wi = temp_db

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(stdout="[]")
        raise AssertionError(f"unexpected call: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        checkin_result = wi.checkin(
            repo_path=".",
            title="Plain feature",
            agent_id="test-agent",
        )

    work_item_id = checkin_result["work_item_id"]

    def fake_run_claim(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "create"]:
            return _fake_completed(
                returncode=1,
                stdout="",
                stderr="GraphQL: Label 'unknown' not found",
            )
        raise AssertionError(f"unexpected call: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run_claim):
        claim_result = wi.claim_new(work_item_id=work_item_id, labels=["unknown"])

    assert claim_result["error"] == "gh issue create failed"
    assert "stderr" in claim_result
    assert "Label 'unknown' not found" in claim_result["stderr"]
    assert claim_result["returncode"] == 1


def test_relink_issue_repoints_number_without_gh_or_status_change(temp_db):
    """relink_issue must update github_issue_number only — no gh call, no status change."""
    _db, wi = temp_db

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _fake_completed(stdout=json.dumps({"nameWithOwner": "owner/repo"}))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(stdout="[]")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        checkin_result = wi.checkin(repo_path=".", title="Migrated item", agent_id="test-agent")
    work_item_id = checkin_result["work_item_id"]

    def fail_run(cmd, **kwargs):
        raise AssertionError(f"relink_issue must never call subprocess: {cmd}")

    with patch.object(subprocess, "run", side_effect=fail_run):
        result = wi.relink_issue(work_item_id, issue_number=257, note="GitHub -> Forgejo migration")

    assert result["work_item_id"] == work_item_id
    assert result["old_issue_number"] is None
    assert result["new_issue_number"] == 257

    row = wi.get_work_item(work_item_id)
    assert row["github_issue_number"] == 257
    assert row["status"] == "declared"  # unchanged


def test_relink_issue_unknown_work_item_returns_error(temp_db):
    _db, wi = temp_db
    result = wi.relink_issue("wi_doesnotexist", issue_number=1)
    assert "error" in result
