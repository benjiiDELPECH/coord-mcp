"""Tests for src/adr.py — atomic ADR number allocation + exponential backoff.

Previously this module (the most concurrency-critical piece of coord-mcp — the
one making the actual atomicity claim) had zero test coverage. This file closes
that gap, including a real multi-threaded contention test: the one test that
actually proves "no duplicate ADR numbers under concurrency" rather than just
asserting it in a docstring.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

import src.adr as adr_mod
import src.db as db_mod


@pytest.fixture
def adr_repo(tmp_path, monkeypatch):
    """A throwaway repo dir + isolated SQLite DB, both scoped to this test."""
    monkeypatch.setenv("COORD_MCP_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db_mod)
    importlib.reload(adr_mod)
    db_mod.init_db()

    repo_dir = tmp_path / "repo"
    (repo_dir / "docs" / "adr").mkdir(parents=True)
    return repo_dir


# ── Backoff delay shape ──────────────────────────────────────────────


def test_backoff_delay_is_bounded_by_cap():
    for attempt in range(20):
        delay = adr_mod._backoff_delay(attempt)
        assert 0 <= delay <= adr_mod.BACKOFF_MAX_S


def test_backoff_delay_grows_with_attempt_on_average():
    """Full jitter means any single sample can be anywhere in [0, cap] — so we
    compare average delay across many samples, not individual draws."""
    def avg_delay(attempt, n=200):
        return sum(adr_mod._backoff_delay(attempt) for _ in range(n)) / n

    avg_attempt_0 = avg_delay(0)
    avg_attempt_3 = avg_delay(3)
    assert avg_attempt_3 > avg_attempt_0


class _FlakyConnProxy:
    """Wraps a real sqlite3.Connection; fails the first ADR INSERT once, then
    delegates everything (including that same call, on retry) to the real thing.
    sqlite3.Connection is a C-level immutable type — can't be patched directly —
    so we proxy at the level adr.py actually calls through (`connection()`)."""

    def __init__(self, real_conn, armed: dict):
        self._real = real_conn
        self._armed = armed

    def execute(self, sql, params=()):
        if sql.strip().startswith("INSERT INTO adr_allocations") and self._armed["on"]:
            self._armed["on"] = False
            raise sqlite3.IntegrityError("UNIQUE constraint failed (simulated)")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_backoff_is_actually_invoked_on_integrity_error(adr_repo):
    """claim_adr must sleep (not busy-loop) when it hits the UNIQUE constraint."""
    from contextlib import contextmanager

    armed = {"on": True}

    @contextmanager
    def flaky_connection(db_path=None):
        with db_mod.connection() as real_conn:
            yield _FlakyConnProxy(real_conn, armed)

    with patch.object(adr_mod, "connection", flaky_connection):
        with patch.object(adr_mod.time, "sleep") as mock_sleep:
            result = adr_mod.claim_adr(repo_path=str(adr_repo), topic="test topic")

    assert result["adr_number"] == 1
    mock_sleep.assert_called_once()
    assert 0 <= mock_sleep.call_args[0][0] <= adr_mod.BACKOFF_MAX_S


# ── Correctness under real concurrency ───────────────────────────────


def test_claim_adr_no_duplicates_under_real_thread_contention(adr_repo):
    """The actual regression test for the atomicity claim this whole module makes:
    N threads racing claim_adr on the SAME repo must produce N distinct, gap-free
    ADR numbers — proven by running real threads against the real SQLite file,
    not by reasoning about the code in a docstring.
    """
    N = 12
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(i):
        try:
            r = adr_mod.claim_adr(repo_path=str(adr_repo), topic=f"concurrent topic {i}",
                                   create_skeleton=True)
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

    assert errors == [], f"claim_adr raised under contention: {errors}"
    assert len(results) == N

    numbers = sorted(r["adr_number"] for r in results)
    assert numbers == list(range(1, N + 1)), (
        f"expected exactly {{1..{N}}} with no duplicates or gaps, got {numbers}"
    )

    # Every skeleton file must exist and match its own claimed number — proves
    # the filename and the DB row never diverged under the race.
    for r in results:
        from pathlib import Path
        assert Path(r["file_path"]).exists()
        assert f"ADR-{r['adr_number']:03d}-" in Path(r["file_path"]).name
