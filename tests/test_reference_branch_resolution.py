"""Reproduces and pins the fix for the hardcoded-`origin/main` bug.

Observed symptom (alert-immo, 2x): `checkout_work` reported hundreds of
phantom "parallel scope conflicts" / a huge out-of-scope diff on a change
that was actually 2 files. Root cause: the diff baseline was hardcoded to
`origin/main`. On repos whose canonical remote is NOT `origin` (e.g.
alert-immo's canonical is `forgejo`, with `origin` left behind as a stale
GitHub mirror), diffing HEAD against the stale mirror produces a diff full
of commits that are already on the real canonical branch — reported as
"conflicts" that don't exist. Manually confirmed via
`git diff forgejo/main --stat` (2 files, correct) vs `git diff origin/main
--stat` (hundreds of files, false positive).

These tests use REAL git repos (not mocked subprocess) so the reproduction
is not an artifact of test mocking — it exercises actual git plumbing the
way `checkout_work` does in production.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env_args = [
        "git",
        "-c", "user.name=coord-mcp-test",
        "-c", "user.email=coord-mcp-test@example.com",
        "-c", "init.defaultBranch=main",
        "-c", "commit.gpgsign=false",
        *args,
    ]
    result = subprocess.run(env_args, cwd=cwd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo_with_commit(path: Path, filename: str, content: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], cwd=path)
    (path / filename).write_text(content)
    _run_git(["add", "."], cwd=path)
    _run_git(["commit", "-m", f"init: {filename}"], cwd=path)


@pytest.fixture
def two_remotes_repo(tmp_path):
    """Build the exact topology that triggered the bug:

    - `origin_bare`: STALE remote. Frozen at an old commit — stands in for
      a GitHub mirror nobody pushes to anymore.
    - `forgejo_bare`: CANONICAL remote. Has the stale commit PLUS 300 more
      commits landed since — stands in for the real Forgejo history.
    - `work`: the working repo, with both remotes configured, sitting on a
      feature branch that made exactly 2 real file changes on top of the
      canonical (forgejo) history.

    Returns the `work` repo path.
    """
    # 1. Canonical history lives in a "seed" repo we grow, then mirror the
    #    stale state into origin_bare BEFORE the 300 extra commits land.
    seed = tmp_path / "seed"
    _init_repo_with_commit(seed, "base.txt", "base\n")

    origin_bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin_bare)], check=True, capture_output=True)
    _run_git(["push", str(origin_bare), "main"], cwd=seed)

    # 2. Grow the canonical history far beyond what origin has (simulates
    #    months of merged work landing on Forgejo after the GitHub mirror
    #    went stale). Kept small (not literally hundreds) to keep the test
    #    suite fast — the mechanism reproduced is identical regardless of N.
    for i in range(20):
        (seed / f"file_{i}.txt").write_text(f"content {i}\n")
        _run_git(["add", "."], cwd=seed)
        _run_git(["commit", "-m", f"canonical work {i}"], cwd=seed)

    forgejo_bare = tmp_path / "forgejo.git"
    subprocess.run(["git", "init", "--bare", str(forgejo_bare)], check=True, capture_output=True)
    _run_git(["push", str(forgejo_bare), "main"], cwd=seed)

    # 3. Working repo: clone from the CANONICAL (forgejo) remote — this is
    #    what a real agent does (`git worktree add ... origin/forgejo main`).
    work = tmp_path / "work"
    _run_git(["clone", str(forgejo_bare), str(work)], cwd=tmp_path)
    _run_git(["remote", "rename", "origin", "forgejo"], cwd=work)
    _run_git(["remote", "add", "origin", str(origin_bare)], cwd=work)
    _run_git(["fetch", "origin"], cwd=work)
    _run_git(["fetch", "forgejo"], cwd=work)

    # 4. Make the REAL change under test: exactly 2 files, on top of the
    #    canonical (forgejo) history.
    (work / "real_change_1.py").write_text("real change 1\n")
    (work / "real_change_2.py").write_text("real change 2\n")
    _run_git(["add", "."], cwd=work)
    _run_git(["commit", "-m", "feat: the actual 2-file change"], cwd=work)

    return work


def test_diff_against_stale_origin_produces_phantom_conflicts(two_remotes_repo):
    """Sanity check that the bug is real: diffing vs the stale mirror is huge."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=two_remotes_repo, capture_output=True, text=True,
    )
    stale_diff = [f for f in result.stdout.strip().splitlines() if f]
    # 20 canonical commits + 2 real files = 22 files "different" from the
    # stale mirror — same MECHANISM as the "351 files / 8 conflicts" false
    # positive observed in production (only the scale differs).
    assert len(stale_diff) == 22


def test_diff_against_canonical_forgejo_is_the_real_2_file_change(two_remotes_repo):
    """The ground truth: vs the canonical branch, only the real edit shows up."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "forgejo/main...HEAD"],
        cwd=two_remotes_repo, capture_output=True, text=True,
    )
    real_diff = sorted(f for f in result.stdout.strip().splitlines() if f)
    assert real_diff == ["real_change_1.py", "real_change_2.py"]


def test_resolve_reference_ref_autodetects_the_fresher_canonical_remote(two_remotes_repo):
    """The fix: with no config, auto-detect picks forgejo/main (freshest), not origin/main."""
    from src.checkout import resolve_reference_ref

    ref = resolve_reference_ref(str(two_remotes_repo))
    assert ref == "forgejo/main"


def test_git_diff_files_uses_the_fixed_resolution_not_stale_origin(two_remotes_repo):
    """End-to-end: `_git_diff_files` — what `checkout_work` actually calls —
    returns the real 2-file diff, not the 302-file phantom diff."""
    from src.checkout import _git_diff_files

    files = sorted(_git_diff_files(str(two_remotes_repo)))
    assert files == ["real_change_1.py", "real_change_2.py"]


def test_repo_git_config_override_wins_over_autodetect(two_remotes_repo):
    """Explicit `git config coord-mcp.canonical-remote origin` is honoured even
    though forgejo is fresher — the escape hatch must be authoritative."""
    from src.checkout import resolve_reference_ref

    _run_git(["config", "coord-mcp.canonical-remote", "origin"], cwd=two_remotes_repo)
    ref = resolve_reference_ref(str(two_remotes_repo))
    assert ref == "origin/main"


def test_env_var_override_wins_over_autodetect_but_not_repo_config(two_remotes_repo, monkeypatch):
    """COORD_MCP_REFERENCE_REMOTE is honoured when no repo-level config exists."""
    from src.checkout import resolve_reference_ref

    monkeypatch.setenv("COORD_MCP_REFERENCE_REMOTE", "origin")
    ref = resolve_reference_ref(str(two_remotes_repo))
    assert ref == "origin/main"


def test_single_remote_repo_falls_back_to_origin_main_unchanged(tmp_path):
    """Repos with only `origin` configured keep the original behaviour exactly."""
    from src.checkout import resolve_reference_ref

    repo = tmp_path / "single"
    _init_repo_with_commit(repo, "a.txt", "a\n")
    bare = tmp_path / "single.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _run_git(["remote", "add", "origin", str(bare)], cwd=repo)
    _run_git(["push", "origin", "main"], cwd=repo)

    ref = resolve_reference_ref(str(repo))
    assert ref == "origin/main"


def test_checkout_end_to_end_no_longer_reports_phantom_out_of_scope(two_remotes_repo, temp_db):
    """The full `checkout()` call: with the fix, out-of-scope count reflects
    the real 2-file change, not hundreds of files from the stale mirror."""
    import importlib

    import src.checkout as co
    importlib.reload(co)

    _db, wi = temp_db
    item = wi.checkin(
        repo_path=str(two_remotes_repo),
        title="Real 2-file change",
        scope_files=["real_change_1.py", "real_change_2.py"],
    )
    work_item_id = item["work_item_id"]

    from unittest.mock import patch
    with patch("src.checkout._find_open_prs_on_files", return_value=[]):
        result = co.checkout(work_item_id)

    assert result["diff_source"] in ("scope-match", "repo-fallback")
    assert sorted(result["actual_diff_files"]) == ["real_change_1.py", "real_change_2.py"]
    assert result["out_of_scope_count"] == 0
    assert result["untouched_declared_count"] == 0
