"""
Contract for scope-resolution providers — the pluggable interface that keeps
checkin's semantic conflict detection decoupled from any single code indexer.

GitNexus (gitnexus_bridge.py) is the reference implementation, wired as the
default composition in server.py's startup. work_items.py never imports
gitnexus_bridge directly — it only depends on this Protocol, plus the safe
no-op default (`null_resolver`). Any other provider (ctags, a language server,
a plain import-graph grep) can be substituted by conforming to this shape and
calling `work_items.set_scope_resolver(...)`; nothing in the conflict-detection
logic needs to change.
"""

from __future__ import annotations

from typing import Protocol


class ScopeResolver(Protocol):
    """Resolve declared symbol/concept names to their blast-radius file set.

    Args:
        repo_alias: how the provider identifies the target repo (e.g. a registered
            index name, a directory basename — provider-specific, not standardized
            by this contract).
        symbols: symbol/concept names the caller intends to touch.
        depth: how many relationship hops to traverse, if the provider supports it.

    Returns:
        {"files": list[str], "warnings": list[str]}.

    Must never raise. Any failure (provider unavailable, repo unindexed, symbol
    unknown, timeout) degrades to an empty files list with an explanatory warning
    in `warnings` — checkin() must always succeed even when no resolver is
    configured, or the configured resolver fails outright.
    """

    def __call__(self, repo_alias: str, symbols: list[str], depth: int = 2) -> dict:
        ...


def null_resolver(repo_alias: str, symbols: list[str], depth: int = 2) -> dict:
    """Default resolver when nothing is wired: pure file-path matching, unchanged
    from before semantic scope expansion existed. Never a network/subprocess call."""
    if not symbols:
        return {"files": [], "warnings": []}
    return {"files": [], "warnings": ["no scope resolver configured — semantic scope expansion skipped"]}
