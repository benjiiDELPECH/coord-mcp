"""
Best-effort bridge to the GitNexus CLI for semantic scope expansion.

coord-mcp's default conflict detection compares declared `scope_files` by exact
path overlap. Two agents touching different files that both derive from the same
symbol (e.g. a Controller and a model it imports) are invisible to that check.
If the target repo is indexed by GitNexus, `expand_scope` resolves declared
`scope_symbols` to their blast-radius file set via `gitnexus impact`, so
conflict detection can compare on the richer set instead.

Never raises: any failure (CLI absent, repo not indexed, symbol not found,
timeout) degrades to an empty expansion with a warning. Semantic expansion is
a pure enhancement on top of file-path matching, never a hard dependency —
checkin must always succeed even without GitNexus installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess

GITNEXUS_TIMEOUT_S = 10


def gitnexus_available() -> bool:
    return shutil.which("gitnexus") is not None


def _impact_files(repo_alias: str, symbol: str, depth: int) -> tuple[list[str], str | None]:
    """Run `gitnexus impact <symbol> -r <repo_alias>` and extract affected file paths.

    Returns (files, warning). warning is None on success.
    """
    try:
        result = subprocess.run(
            ["gitnexus", "impact", symbol, "-r", repo_alias,
             "--depth", str(depth), "--direction", "downstream"],
            capture_output=True,
            text=True,
            timeout=GITNEXUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [], f"{symbol}: gitnexus impact timed out after {GITNEXUS_TIMEOUT_S}s"

    if not result.stdout.strip():
        return [], f"{symbol}: gitnexus impact returned no output ({result.stderr.strip()[:200]})"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], f"{symbol}: gitnexus impact returned invalid JSON"

    if data.get("error"):
        return [], f"{symbol}: {data['error']}"

    files = set()
    target_path = data.get("target", {}).get("filePath")
    if target_path:
        files.add(target_path)
    for depth_bucket in (data.get("byDepth") or {}).values():
        for entry in depth_bucket:
            path = entry.get("filePath")
            if path:
                files.add(path)

    return sorted(files), None


def expand_scope(repo_alias: str, symbols: list[str], depth: int = 2) -> dict:
    """Resolve declared symbols to their blast-radius file set.

    Returns {"files": [...], "warnings": [...]}. Best-effort — never raises,
    empty symbols or missing GitNexus both degrade to an empty, warning-only result.
    """
    if not symbols:
        return {"files": [], "warnings": []}

    if not gitnexus_available():
        return {"files": [], "warnings": ["gitnexus CLI not found in PATH — semantic scope expansion skipped"]}

    all_files: set[str] = set()
    warnings: list[str] = []
    for symbol in symbols:
        files, warning = _impact_files(repo_alias, symbol, depth)
        all_files.update(files)
        if warning:
            warnings.append(warning)

    return {"files": sorted(all_files), "warnings": warnings}
