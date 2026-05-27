"""Tests for checkout — auto-detection cascade, worktree matching, fallbacks.

Covers coord-mcp#4 (HEAD ambigu in multi-worktree) and coord-mcp#2
(silent empty diff when HEAD == origin/main).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ────────────────────────────────────────────────────────────────────
# Mock helpers
# ────────────────────────────────────────────────────────────────────


def _make_mock_run(responses_by_cwd: dict):
    """Build a fake subprocess.run.

    responses_by_cwd: {cwd_path: {cmd_substring: (returncode, stdout)}}.
    Lookup falls back to cwd "" if a specific cwd is not in the map.
    Default response: (0, "").
    """

    def _run(cmd, **kwargs):
        cwd = kwargs.get("cwd", "")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        responses = responses_by_cwd.get(cwd, responses_by_cwd.get("", {}))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        for pattern, (rc, stdout) in responses.items():
            if pattern in cmd_str:
                m.returncode = rc
                m.stdout = stdout
                return m
        return m

    return _run


# ────────────────────────────────────────────────────────────────────
# _list_worktrees
# ────────────────────────────────────────────────────────────────────


def test_list_worktrees_parses_porcelain_correctly():
    from src.checkout import _list_worktrees

    porcelain = (
        "worktree /a\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /b\n"
        "HEAD def\n"
        "branch refs/heads/feat/x\n"
        "\n"
        "worktree /c\n"
        "HEAD ghi\n"
        "detached\n"
        "\n"
    )
    with patch("src.checkout.subprocess.run") as m:
        m.return_value.returncode = 0
        m.return_value.stdout = porcelain
        result = _list_worktrees("/a")

    assert len(result) == 3
    assert result[0] == {"path": "/a", "head": "abc", "branch": "main"}
    assert result[1] == {"path": "/b", "head": "def", "branch": "feat/x"}
    assert result[2] == {"path": "/c", "head": "ghi", "branch": None}


def test_list_worktrees_returns_empty_on_git_error():
    from src.checkout import _list_worktrees

    with patch("src.checkout.subprocess.run") as m:
        m.return_value.returncode = 128
        m.return_value.stdout = ""
        m.return_value.stderr = "fatal: not a git repository"
        result = _list_worktrees("/not-a-repo")

    assert result == []


# ────────────────────────────────────────────────────────────────────
# _find_worktree_matching_scope
# ────────────────────────────────────────────────────────────────────


def test_find_worktree_matching_scope_picks_best_overlap():
    from src.checkout import _find_worktree_matching_scope

    porcelain = (
        "worktree /a\nHEAD aaa\nbranch refs/heads/m\n\n"
        "worktree /b\nHEAD bbb\nbranch refs/heads/x\n\n"
        "worktree /c\nHEAD ccc\nbranch refs/heads/y\n\n"
    )
    mock = _make_mock_run({
        "/a": {
            "worktree list --porcelain": (0, porcelain),
            "diff --name-only origin/main...HEAD": (0, "x.py\n"),
        },
        "/b": {"diff --name-only origin/main...HEAD": (0, "a.py\nb.py\n")},
        "/c": {"diff --name-only origin/main...HEAD": (0, "a.py\n")},
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        best_path, overlap = _find_worktree_matching_scope(
            repo_path="/a",
            declared_files={"a.py", "b.py", "c.py"},
        )

    assert best_path == "/b"
    assert overlap == 2


def test_find_worktree_matching_scope_returns_none_if_no_overlap():
    from src.checkout import _find_worktree_matching_scope

    porcelain = "worktree /a\nHEAD aaa\nbranch refs/heads/m\n\n"
    mock = _make_mock_run({
        "/a": {
            "worktree list --porcelain": (0, porcelain),
            "diff --name-only origin/main...HEAD": (0, "docs/x.md\n"),
        },
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        best_path, overlap = _find_worktree_matching_scope(
            repo_path="/a",
            declared_files={"src/a.py"},
        )

    assert best_path is None
    assert overlap == 0


def test_find_worktree_matching_scope_empty_declared_returns_none():
    from src.checkout import _find_worktree_matching_scope

    with patch("src.checkout.subprocess.run") as m:
        m.return_value.returncode = 0
        m.return_value.stdout = ""
        best_path, overlap = _find_worktree_matching_scope(
            repo_path="/a",
            declared_files=set(),
        )

    assert best_path is None
    assert overlap == 0


# ────────────────────────────────────────────────────────────────────
# _head_is_origin_main
# ────────────────────────────────────────────────────────────────────


def test_head_is_origin_main_true_when_zero_commits():
    from src.checkout import _head_is_origin_main

    with patch("src.checkout.subprocess.run") as m:
        m.return_value.returncode = 0
        m.return_value.stdout = "0\n"
        assert _head_is_origin_main("/repo") is True


def test_head_is_origin_main_false_when_ahead():
    from src.checkout import _head_is_origin_main

    with patch("src.checkout.subprocess.run") as m:
        m.return_value.returncode = 0
        m.return_value.stdout = "3\n"
        assert _head_is_origin_main("/repo") is False


# ────────────────────────────────────────────────────────────────────
# _auto_detect_diff_files — cascade behaviour
# ────────────────────────────────────────────────────────────────────


def test_explicit_worktree_override_uses_it_directly():
    """Param worktree_path → utilisé en priorité, pas de scan worktree."""
    from src.checkout import _auto_detect_diff_files

    mock = _make_mock_run({
        "/my/custom/wt": {
            "diff --name-only origin/main...HEAD": (0, "src/a.py\nsrc/b.py\n"),
        },
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        files, source, warnings = _auto_detect_diff_files(
            repo_path="/main",
            declared_files={"src/a.py"},
            worktree_path_override="/my/custom/wt",
        )

    assert source == "worktree-override"
    assert "src/a.py" in files
    assert "src/b.py" in files
    assert warnings == []


def test_scope_matching_worktree_is_picked_over_main_repo():
    """Bug #4 résolu : repo principal sur branche A, worktree B matching scope → pick B."""
    from src.checkout import _auto_detect_diff_files

    porcelain = (
        "worktree /repo-main\n"
        "HEAD aaa\n"
        "branch refs/heads/feat/other\n"
        "\n"
        "worktree /repo-fix-4\n"
        "HEAD bbb\n"
        "branch refs/heads/fix/checkout-work\n"
        "\n"
    )
    mock = _make_mock_run({
        "/repo-main": {
            "worktree list --porcelain": (0, porcelain),
            "diff --name-only origin/main...HEAD": (0, "docs/other.md\n"),
        },
        "/repo-fix-4": {
            "diff --name-only origin/main...HEAD": (0, "src/checkout.py\ntests/test_checkout.py\n"),
        },
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        files, source, warnings = _auto_detect_diff_files(
            repo_path="/repo-main",
            declared_files={"src/checkout.py"},
        )

    assert source == "scope-match"
    assert "src/checkout.py" in files
    assert "docs/other.md" not in files


def test_no_matching_worktree_falls_back_to_repo_with_defensive_warning():
    """Aucun worktree n'overlap → fallback HEAD + warning défensif sur le mismatch."""
    from src.checkout import _auto_detect_diff_files

    porcelain = (
        "worktree /repo-main\n"
        "HEAD aaa\n"
        "branch refs/heads/feat/A\n"
        "\n"
        "worktree /repo-other\n"
        "HEAD bbb\n"
        "branch refs/heads/feat/B\n"
        "\n"
    )
    mock = _make_mock_run({
        "/repo-main": {
            "worktree list --porcelain": (0, porcelain),
            "diff --name-only origin/main...HEAD": (0, "docs/unrelated.md\n"),
            "rev-list --count": (0, "5\n"),
        },
        "/repo-other": {
            "diff --name-only origin/main...HEAD": (0, "src/foo.py\n"),
        },
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        files, source, warnings = _auto_detect_diff_files(
            repo_path="/repo-main",
            declared_files={"src/checkout.py"},
        )

    assert source == "repo-fallback"
    assert "docs/unrelated.md" in files
    assert any("worktree" in w.lower() for w in warnings), \
        f"Expected worktree warning, got: {warnings}"


def test_head_equals_origin_main_warns_about_empty_diff():
    """Bug #2 résolu : HEAD == origin/main + diff vide → warning explicite."""
    from src.checkout import _auto_detect_diff_files

    mock = _make_mock_run({
        "/repo": {
            "worktree list --porcelain": (
                0,
                "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n\n",
            ),
            "diff --name-only": (0, ""),
            "rev-list --count": (0, "0\n"),
        },
    })
    with patch("src.checkout.subprocess.run", side_effect=mock):
        files, source, warnings = _auto_detect_diff_files(
            repo_path="/repo",
            declared_files={"src/a.py"},
        )

    assert source == "repo-fallback"
    assert files == []
    assert any("origin/main" in w for w in warnings), \
        f"Expected origin/main warning, got: {warnings}"


# ────────────────────────────────────────────────────────────────────
# Integration : checkout() with explicit diff_files bypasses cascade
# ────────────────────────────────────────────────────────────────────


def test_checkout_with_explicit_diff_files_bypasses_detection(temp_db, tmp_path):
    """When diff_files is passed, _auto_detect_diff_files is NOT called."""
    _db, _wi = temp_db
    import importlib

    import src.checkout as co
    importlib.reload(co)

    # Use a real directory so checkin's subprocess.run(cwd=...) succeeds.
    # Not a real git repo → _detect_repo_slug returns None — acceptable for this test.
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()

    item = _wi.checkin(
        repo_path=str(repo_dir),
        title="Test",
        scope_files=["src/foo.py"],
    )
    wi_id = item["work_item_id"]

    with patch("src.checkout._auto_detect_diff_files") as mock_detect, \
            patch("src.checkout._find_open_prs_on_files", return_value=[]):
        result = co.checkout(wi_id, diff_files=["src/foo.py", "src/bar.py"])

    mock_detect.assert_not_called()
    assert result["diff_source"] == "explicit"
    assert "src/foo.py" in result["actual_diff_files"]
    assert "src/bar.py" in result["actual_diff_files"]
