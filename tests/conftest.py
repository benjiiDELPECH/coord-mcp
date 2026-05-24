"""Pytest fixtures shared across coord-mcp tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make `src/` importable as a top-level package `coord_mcp` for the tests.
# Since the production code uses relative imports (`from .db import ...`), we
# import it via its parent path so `src` itself becomes a package.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point coord-mcp at a fresh SQLite DB per test (isolated state)."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("COORD_MCP_DB", str(db_path))

    # The db module reads COORD_MCP_DB at import time, so we must reload it
    # (and its dependents) after setting the env var.
    import importlib

    import src.db as _db
    importlib.reload(_db)
    import src.work_items as _wi
    importlib.reload(_wi)

    _db.init_db()

    return _db, _wi
