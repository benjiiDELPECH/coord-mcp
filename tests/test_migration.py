"""Tests for src/migration.py — atomic Flyway migration version allocation.

Mirrors tests/test_adr.py exactly: same atomicity claim, same race, same fix.
The one test that matters is the real multi-threaded contention test — it
proves "no duplicate VNNN versions under concurrency" instead of just
asserting it in a docstring, which is the exact failure mode (V144, V154
each collided twice on alert-immo main, 07.09.2026) this module exists to close.
"""

from __future__ import annotations

import sqlite3
import threading
from unittest.mock import patch

import pytest

import src.db as db_mod
import src.migration as migration_mod


@pytest.fixture
def migration_repo(tmp_path, monkeypatch):
    """A throwaway repo dir + isolated SQLite DB, both scoped to this test."""
    monkeypatch.setenv("COORD_MCP_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db_mod)
    importlib.reload(migration_mod)
    db_mod.init_db()

    repo_dir = tmp_path / "repo"
    (repo_dir / migration_mod.DEFAULT_MIGRATIONS_DIR).mkdir(parents=True)
    return repo_dir


# ── Backoff delay shape ──────────────────────────────────────────────


def test_backoff_delay_is_bounded_by_cap():
    for attempt in range(20):
        delay = migration_mod._backoff_delay(attempt)
        assert 0 <= delay <= migration_mod.BACKOFF_MAX_S


def test_backoff_delay_grows_with_attempt_on_average():
    """Full jitter means any single sample can be anywhere in [0, cap] — so we
    compare average delay across many samples, not individual draws."""
    def avg_delay(attempt, n=200):
        return sum(migration_mod._backoff_delay(attempt) for _ in range(n)) / n

    avg_attempt_0 = avg_delay(0)
    avg_attempt_3 = avg_delay(3)
    assert avg_attempt_3 > avg_attempt_0


class _FlakyConnProxy:
    """Wraps a real sqlite3.Connection; fails the first migration INSERT once,
    then delegates everything (including that same call, on retry) to the real
    thing. See test_adr.py._FlakyConnProxy for why this proxies at connection()
    level rather than patching sqlite3.Connection directly."""

    def __init__(self, real_conn, armed: dict):
        self._real = real_conn
        self._armed = armed

    def execute(self, sql, params=()):
        if sql.strip().startswith("INSERT INTO migration_allocations") and self._armed["on"]:
            self._armed["on"] = False
            raise sqlite3.IntegrityError("UNIQUE constraint failed (simulated)")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_backoff_is_actually_invoked_on_integrity_error(migration_repo):
    """claim_migration must sleep (not busy-loop) when it hits the UNIQUE constraint."""
    from contextlib import contextmanager

    armed = {"on": True}

    @contextmanager
    def flaky_connection(db_path=None):
        with db_mod.connection() as real_conn:
            yield _FlakyConnProxy(real_conn, armed)

    with patch.object(migration_mod, "connection", flaky_connection):
        with patch.object(migration_mod.time, "sleep") as mock_sleep:
            result = migration_mod.claim_migration(repo_path=str(migration_repo), description="test migration")

    assert result["migration_version"] == 1
    mock_sleep.assert_called_once()
    assert 0 <= mock_sleep.call_args[0][0] <= migration_mod.BACKOFF_MAX_S


# ── Correctness under real concurrency ───────────────────────────────


def test_claim_migration_no_duplicates_under_real_thread_contention(migration_repo):
    """The actual regression test for the atomicity claim this module makes — and
    the direct reproduction of the incident it was built to prevent: N threads
    racing claim_migration on the SAME repo must produce N distinct, gap-free
    VNNN versions, proven against real SQLite + real filesystem, not reasoning.
    """
    N = 12
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(i):
        try:
            r = migration_mod.claim_migration(
                repo_path=str(migration_repo), description=f"concurrent migration {i}",
                create_skeleton=True,
            )
            with lock:
                results.append(r)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"claim_migration raised under contention: {errors}"
    assert len(results) == N

    versions = sorted(r["migration_version"] for r in results)
    assert versions == list(range(1, N + 1)), (
        f"expected exactly {{1..{N}}} with no duplicates or gaps, got {versions}"
    )

    # Every skeleton file must exist and match its own claimed version — proves
    # the filename and the DB row never diverged under the race.
    for r in results:
        from pathlib import Path
        assert Path(r["file_path"]).exists()
        assert Path(r["file_path"]).name.startswith(f"V{r['migration_version']}__")


def test_claim_migration_respects_existing_filesystem_max(migration_repo):
    """A migration file dropped directly on disk (not through coord-mcp) must
    still be seen — the whole point is scanning the filesystem, not trusting
    the DB alone (an agent could always bypass coord-mcp and write V150 by hand)."""
    mdir = migration_repo / migration_mod.DEFAULT_MIGRATIONS_DIR
    (mdir / "V150__hand_written.sql").write_text("-- hand written\n")

    result = migration_mod.claim_migration(repo_path=str(migration_repo), description="next one")
    assert result["migration_version"] == 151
