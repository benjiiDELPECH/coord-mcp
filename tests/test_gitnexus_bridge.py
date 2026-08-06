"""Tests for src/gitnexus_bridge.py — the CLI stays mocked, never invoked for real."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import src.gitnexus_bridge as gb


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["gitnexus"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_expand_scope_empty_symbols_short_circuits():
    """No symbols declared → no subprocess call at all."""
    with patch.object(subprocess, "run") as mock_run:
        result = gb.expand_scope("alert-immo", [])
    mock_run.assert_not_called()
    assert result == {"files": [], "warnings": []}


def test_expand_scope_gitnexus_not_installed():
    """CLI absent → warning, empty files, checkin must never crash on this."""
    with patch.object(gb, "gitnexus_available", return_value=False):
        with patch.object(subprocess, "run") as mock_run:
            result = gb.expand_scope("alert-immo", ["SomeSymbol"])
    mock_run.assert_not_called()
    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "not found" in result["warnings"][0]


def test_expand_scope_success_extracts_target_and_depth_files():
    """Real shape observed from `gitnexus impact`: target.filePath + byDepth[*][*].filePath."""
    payload = {
        "target": {"name": "HqController", "filePath": "hq/HqController.kt"},
        "direction": "downstream",
        "impactedCount": 1,
        "byDepth": {
            "1": [{"depth": 1, "filePath": "hq/model/HqSitrep.kt", "relationType": "IMPORTS"}]
        },
    }
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout=json.dumps(payload))):
            result = gb.expand_scope("alert-immo", ["HqController"])

    assert result["warnings"] == []
    assert result["files"] == ["hq/HqController.kt", "hq/model/HqSitrep.kt"]


def test_expand_scope_unknown_symbol_degrades_to_warning():
    """gitnexus returns {"error": ...} for an unresolved symbol — must not raise."""
    payload = {"error": "Target 'Ghost' not found", "impactedCount": 0}
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout=json.dumps(payload))):
            result = gb.expand_scope("alert-immo", ["Ghost"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "Ghost" in result["warnings"][0]
    assert "not found" in result["warnings"][0]


def test_expand_scope_timeout_degrades_to_warning():
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="gitnexus", timeout=10)):
            result = gb.expand_scope("alert-immo", ["SlowSymbol"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "timed out" in result["warnings"][0]


def test_expand_scope_invalid_json_degrades_to_warning():
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout="not json {{{")):
            result = gb.expand_scope("alert-immo", ["Weird"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "invalid JSON" in result["warnings"][0]


def test_expand_scope_multiple_symbols_union_files_and_collect_all_warnings():
    """One symbol resolves, one doesn't — both outcomes must surface, not just the last."""
    good_payload = {"target": {"filePath": "a.kt"}, "byDepth": {}}
    bad_payload = {"error": "Target 'Ghost' not found"}

    def fake_run(cmd, **kwargs):
        symbol = cmd[2]
        if symbol == "Known":
            return _fake_completed(stdout=json.dumps(good_payload))
        return _fake_completed(stdout=json.dumps(bad_payload))

    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            result = gb.expand_scope("alert-immo", ["Known", "Ghost"])

    assert result["files"] == ["a.kt"]
    assert len(result["warnings"]) == 1
    assert "Ghost" in result["warnings"][0]
