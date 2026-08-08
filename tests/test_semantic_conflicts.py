"""Tests for scope_symbols → semantic conflict detection in checkin()/_find_conflicts().

The scope resolver is injected via `work_items.set_scope_resolver` (see
scope_resolver.py) — work_items.py never imports gitnexus_bridge directly, so
these tests fake a resolver conforming to the ScopeResolver contract instead of
mocking gitnexus_bridge's internals (that's test_gitnexus_bridge.py's job).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch


def _fake_gh(cmd, **kwargs):
    """Minimal `gh` stub: no repo slug, no similar issues — keeps checkin() side-effect-free."""
    if cmd[:3] == ["gh", "repo", "view"]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not a repo")
    raise AssertionError(f"unexpected gh call: {cmd}")


def _resolver(files: list[str]):
    """Build a ScopeResolver-conforming fake that returns a fixed file set."""
    return lambda repo_alias, symbols, depth=2: {"files": files, "warnings": []}


def test_checkin_persists_scope_symbols_and_expanded_files(temp_db):
    _db, wi = temp_db
    wi.set_scope_resolver(_resolver(["hq/HqController.kt", "hq/model/HqSitrep.kt"]))

    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(
            repo_path=".",
            title="Touch HqController",
            scope_symbols=["HqController"],
            agent_id="agent-a",
        )

    assert result["scope_symbols_expanded"] == ["hq/HqController.kt", "hq/model/HqSitrep.kt"]
    assert result["gitnexus_warnings"] == []

    with _db.connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (result["work_item_id"],)).fetchone()
    assert json.loads(row["scope_symbols"]) == ["HqController"]
    assert json.loads(row["scope_symbols_expanded"]) == ["hq/HqController.kt", "hq/model/HqSitrep.kt"]


def test_symbol_expansion_catches_conflict_invisible_to_scope_files_alone(temp_db):
    """Agent A declares scope_symbols that expand to a file Agent B declared directly —
    scope_files themselves never overlap, only the expansion does."""
    _db, wi = temp_db

    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver([]))
        wi.checkin(
            repo_path=".",
            title="Agent B — direct file edit",
            scope_files=["hq/model/HqSitrep.kt"],
            agent_id="agent-b",
        )

        wi.set_scope_resolver(_resolver(["hq/model/HqSitrep.kt"]))
        result_a = wi.checkin(
            repo_path=".",
            title="Agent A — touches HqController, which imports HqSitrep",
            scope_symbols=["HqController"],
            agent_id="agent-a",
        )

    assert len(result_a["conflicts"]) == 1
    assert result_a["conflicts"][0]["agent_id"] == "agent-b"
    assert "hq/model/HqSitrep.kt" in result_a["conflicts"][0]["overlapping_files"]


def test_symbol_expansion_conflict_detected_symmetrically(temp_db):
    """Same scenario, declared in the opposite order: the ALREADY-ACTIVE item is the
    one with the symbol expansion, the NEW arrival has a plain scope_files hit.
    Regression guard for the `other_files |= scope_symbols_expanded` line in
    _find_conflicts — without it, only one direction of this comparison is caught."""
    _db, wi = temp_db

    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["hq/model/HqSitrep.kt"]))
        wi.checkin(
            repo_path=".",
            title="Agent A — touches HqController first",
            scope_symbols=["HqController"],
            agent_id="agent-a",
        )

        wi.set_scope_resolver(_resolver([]))
        result_b = wi.checkin(
            repo_path=".",
            title="Agent B — direct file edit, arrives second",
            scope_files=["hq/model/HqSitrep.kt"],
            agent_id="agent-b",
        )

    assert len(result_b["conflicts"]) == 1
    assert result_b["conflicts"][0]["agent_id"] == "agent-a"


def test_no_symbols_behaves_exactly_like_before(temp_db):
    """Backward compatibility: with no resolver wired at all (module default,
    null_resolver), conflict detection stays pure file-path overlap."""
    _db, wi = temp_db

    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.checkin(repo_path=".", title="Plain A", scope_files=["a.py"], agent_id="agent-a")
        result_b = wi.checkin(repo_path=".", title="Plain B", scope_files=["b.py"], agent_id="agent-b")

    assert result_b["conflicts"] == []


def test_default_resolver_is_null_until_wired(temp_db):
    """work_items.py must never touch a concrete provider on its own — the module
    default is null_resolver, proven here by declaring scope_symbols with NOTHING
    wired and checking the warning names the missing configuration, not a CLI error."""
    _db, wi = temp_db

    with patch.object(subprocess, "run", side_effect=_fake_gh):
        result = wi.checkin(
            repo_path=".",
            title="No resolver wired",
            scope_symbols=["AnySymbol"],
            agent_id="agent-a",
        )

    assert result["scope_symbols_expanded"] == []
    assert result["gitnexus_warnings"] == ["no scope resolver configured — semantic scope expansion skipped"]
