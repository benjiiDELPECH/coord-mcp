"""
Work items — checkin / claim / list / checkout / release.

Each work item models a unit of agent activity. Lifecycle:

    declared → claimed → in_progress → checked_out → released
                                                     └─→ abandoned

Conflict detection on checkin = overlap on `scope_files` with other active items,
unioned with the GitNexus blast-radius of any declared `scope_symbols` (best-effort —
falls back to file-path-only matching if GitNexus is unavailable or the repo isn't indexed).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from . import gitnexus_bridge, graphiti_bridge
from .db import connection, log_audit, now_iso


ACTIVE_STATUSES = ("declared", "claimed", "in_progress", "checked_out")


# ── Helpers ──────────────────────────────────────────────────────────


def _generate_id() -> str:
    return f"wi_{uuid.uuid4().hex[:12]}"


def _gh(args: list[str], json_out: bool = True) -> dict | list | str | None:
    """Run `gh` CLI and return parsed JSON (or text). Returns None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        if json_out and result.stdout.strip():
            return json.loads(result.stdout)
        return result.stdout
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _gh_with_stderr(args: list[str]) -> tuple[int, str, str]:
    """Run `gh` CLI and return (returncode, stdout, stderr). For error reporting."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "gh command timed out after 20s"
    except FileNotFoundError:
        return 127, "", "gh CLI not found in PATH"


def _resolve_milestone_title(repo_slug: str, milestone_number: int) -> str | None:
    """Resolve a milestone number to its title via the GitHub API.

    The `gh issue create --milestone <X>` flag expects the milestone **title**, not
    the number. We must resolve number → title before invoking `gh issue create`.
    Returns None if the milestone doesn't exist or the API call fails.
    """
    title = _gh(
        ["api", f"repos/{repo_slug}/milestones/{milestone_number}", "--jq", ".title"],
        json_out=False,
    )
    if not isinstance(title, str):
        return None
    title = title.strip()
    return title or None


def _detect_repo_slug(repo_path: str) -> str | None:
    """Get owner/repo from `gh repo view` for the given dir."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        capture_output=True,
        text=True,
        cwd=repo_path,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("nameWithOwner")
    except json.JSONDecodeError:
        return None


def _find_conflicts(repo: str, scope_files: list[str]) -> list[dict]:
    """Return active work items in the same repo with overlapping scope.

    `scope_files` here is already the caller's declared files UNIONED with the
    GitNexus-expanded impact of their declared symbols (see `checkin`). Each
    candidate's own scope is expanded the same way (scope_files ∪ scope_symbols_expanded)
    before comparing, so a semantic conflict is caught on either side.
    """
    if not scope_files:
        return []
    scope_set = set(scope_files)
    conflicts = []
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM work_items WHERE repo = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            (repo, *ACTIVE_STATUSES),
        ).fetchall()
    for row in rows:
        other_files = set(json.loads(row["scope_files"] or "[]"))
        other_files |= set(json.loads(row["scope_symbols_expanded"] or "[]"))
        overlap = scope_set & other_files
        if overlap:
            conflicts.append({
                "work_item_id": row["id"],
                "agent_id": row["agent_id"],
                "title": row["title"],
                "github_issue_number": row["github_issue_number"],
                "status": row["status"],
                "overlapping_files": sorted(overlap),
            })
    return conflicts


def _find_similar_issues(repo_slug: str, query: str, limit: int = 5) -> list[dict]:
    """Search open GH issues for a textual match. Best-effort."""
    if not repo_slug:
        return []
    data = _gh(["issue", "list", "--repo", repo_slug, "--state", "open",
                "--search", query, "--limit", str(limit),
                "--json", "number,title,url,labels"])
    if isinstance(data, list):
        return [{"number": d["number"], "title": d["title"], "url": d["url"],
                 "labels": [l.get("name") for l in d.get("labels", [])]} for d in data]
    return []


# ── Public API ───────────────────────────────────────────────────────


def checkin(
    repo_path: str,
    title: str,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    scope_adr_topic: str | None = None,
    milestone_number: int | None = None,
    eta_hours: float | None = None,
    agent_id: str | None = None,
) -> dict:
    """Declare intent to work on something. Returns conflicts + suggestions.

    `scope_symbols` (optional): code symbol/concept names the agent intends to touch.
    If the repo is indexed by GitNexus, each symbol's downstream blast radius is
    resolved via `gitnexus impact` and unioned into the conflict-detection scope —
    catches semantic collisions (two agents on different files, same dependent code)
    that pure `scope_files` overlap misses. Best-effort: silently degrades to
    file-only matching if GitNexus is absent or the repo isn't indexed.

    Does NOT mutate GitHub yet. The caller (or the user) then chooses to:
    - claim an existing issue (call `claim_issue`)
    - create a new issue (call `claim_new`)
    - abort (call `abandon`)
    """
    repo_path_abs = str(Path(repo_path).resolve())
    repo_slug = _detect_repo_slug(repo_path_abs)
    scope_files = scope_files or []
    scope_symbols = scope_symbols or []

    expansion = gitnexus_bridge.expand_scope(Path(repo_path_abs).name, scope_symbols)
    expanded_files = expansion["files"]

    conflicts = _find_conflicts(repo_path_abs, sorted(set(scope_files) | set(expanded_files)))
    similar_issues = _find_similar_issues(repo_slug, title) if repo_slug else []

    graphiti_group_id = graphiti_bridge.infer_group_id(repo_path_abs)
    if graphiti_group_id:
        graphiti_result = graphiti_bridge.search_prior_decisions(title, graphiti_group_id)
    else:
        graphiti_result = {"nodes": [], "warnings": ["repo has no known Graphiti group_id — skipped"]}

    wi_id = _generate_id()
    with connection() as conn:
        conn.execute(
            "INSERT INTO work_items "
            "(id, repo, title, scope_files, scope_symbols, scope_symbols_expanded, "
            " scope_adr_topic, milestone_number, agent_id, status, eta_hours, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'declared', ?, ?, ?)",
            (wi_id, repo_path_abs, title, json.dumps(scope_files), json.dumps(scope_symbols),
             json.dumps(expanded_files), scope_adr_topic, milestone_number, agent_id,
             eta_hours, now_iso(), now_iso()),
        )

    result = {
        "work_item_id": wi_id,
        "repo_path": repo_path_abs,
        "repo_slug": repo_slug,
        "title": title,
        "status": "declared",
        "scope_symbols_expanded": expanded_files,
        "gitnexus_warnings": expansion["warnings"],
        "conflicts": conflicts,
        "similar_existing_issues": similar_issues,
        "graphiti_prior_decisions": [
            {"name": n.get("name"), "summary": n.get("summary")} for n in graphiti_result["nodes"]
        ],
        "graphiti_warnings": graphiti_result["warnings"],
        "suggested_action": _suggest_action(conflicts, similar_issues, graphiti_result["nodes"]),
        "next_call": {
            "to_claim_existing": "claim_issue(work_item_id, github_issue_number)",
            "to_create_new": "claim_new(work_item_id, title, body)",
            "to_abort": "abandon(work_item_id, reason)",
        },
    }
    log_audit("checkin", args={"repo_path": repo_path_abs, "title": title},
              result=result, agent_id=agent_id, work_item_id=wi_id)
    return result


def _suggest_action(conflicts: list[dict], similar: list[dict], graphiti_nodes: list[dict] | None = None) -> str:
    if conflicts:
        return "REVIEW_CONFLICTS — overlapping scope detected, decide whether to abort or coordinate"
    if graphiti_nodes:
        return (
            f"REVIEW_PRIOR_DECISIONS — {len(graphiti_nodes)} Graphiti node(s) already exist on this "
            "topic, possibly on an unmerged branch — read them before designing an approach"
        )
    if similar:
        return f"CONSIDER_CLAIMING — {len(similar)} similar open issue(s) might already cover this"
    return "CREATE_NEW — no conflicts, no similar work, safe to create new issue"


def claim_issue(work_item_id: str, github_issue_number: int) -> dict:
    """Bind a work item to an existing GitHub issue (assignee = current user)."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if not row:
            return {"error": f"work_item {work_item_id} not found"}
        repo_slug = _detect_repo_slug(row["repo"])
        if repo_slug:
            _gh(["issue", "edit", str(github_issue_number), "--repo", repo_slug,
                 "--add-assignee", "@me"], json_out=False)
        conn.execute(
            "UPDATE work_items SET github_issue_number=?, status='claimed', updated_at=? WHERE id=?",
            (github_issue_number, now_iso(), work_item_id),
        )
    result = {"work_item_id": work_item_id, "github_issue_number": github_issue_number,
              "status": "claimed"}
    log_audit("claim_issue",
              args={"work_item_id": work_item_id, "github_issue_number": github_issue_number},
              result=result, agent_id=row["agent_id"], work_item_id=work_item_id)
    return result


def claim_new(work_item_id: str, body: str = "", labels: list[str] | None = None) -> dict:
    """Create a new GitHub issue and bind it to the work item."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if not row:
            return {"error": f"work_item {work_item_id} not found"}
        repo_slug = _detect_repo_slug(row["repo"])
        if not repo_slug:
            return {"error": "could not detect GitHub repo slug"}

        args = ["issue", "create", "--repo", repo_slug,
                "--title", row["title"], "--body", body or _default_body(row)]
        if row["milestone_number"]:
            # `gh issue create --milestone` expects the milestone TITLE, not its number.
            # Resolve it lazily here so we don't need a SQLite migration. If the milestone
            # doesn't exist on the remote, surface an explicit error instead of letting
            # `gh issue create` fail opaquely.
            milestone_title = _resolve_milestone_title(repo_slug, row["milestone_number"])
            if milestone_title is None:
                return {
                    "error": (
                        f"milestone #{row['milestone_number']} not found in {repo_slug} "
                        f"(checkin recorded a milestone number that doesn't resolve to a "
                        f"title via GitHub API)"
                    )
                }
            args += ["--milestone", milestone_title]
        if labels:
            for lbl in labels:
                args += ["--label", lbl]

        rc, stdout, stderr = _gh_with_stderr(args)
        if rc != 0:
            return {
                "error": "gh issue create failed",
                "stderr": (stderr or "").strip()[:500],
                "returncode": rc,
            }
        out = stdout
        # `gh issue create` returns the URL on stdout
        url = out.strip().splitlines()[-1] if out else ""
        try:
            issue_number = int(url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            return {"error": f"could not parse issue number from: {url}"}

        conn.execute(
            "UPDATE work_items SET github_issue_number=?, status='claimed', updated_at=? WHERE id=?",
            (issue_number, now_iso(), work_item_id),
        )
    result = {"work_item_id": work_item_id, "github_issue_number": issue_number,
              "url": url, "status": "claimed"}
    log_audit("claim_new",
              args={"work_item_id": work_item_id, "labels": labels, "body_preview": (body or "")[:80]},
              result=result, agent_id=row["agent_id"], work_item_id=work_item_id)
    return result


def _default_body(row) -> str:
    scope_files = json.loads(row["scope_files"] or "[]")
    parts = [f"_Work item created by coord-mcp ({row['id']})_", ""]
    if scope_files:
        parts.append("**Scope files** :")
        parts += [f"- `{f}`" for f in scope_files]
        parts.append("")
    if row["scope_adr_topic"]:
        parts.append(f"**ADR topic** : {row['scope_adr_topic']}")
    if row["eta_hours"]:
        parts.append(f"**ETA** : {row['eta_hours']}h")
    return "\n".join(parts)


def list_active_work(repo_path: str | None = None) -> list[dict]:
    """List all non-released work items. Optionally filter by repo."""
    with connection() as conn:
        if repo_path:
            repo_abs = str(Path(repo_path).resolve())
            rows = conn.execute(
                f"SELECT * FROM work_items WHERE repo = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                "ORDER BY created_at DESC",
                (repo_abs, *ACTIVE_STATUSES),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM work_items WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                "ORDER BY created_at DESC",
                ACTIVE_STATUSES,
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("scope_files"):
        try:
            d["scope_files"] = json.loads(d["scope_files"])
        except json.JSONDecodeError:
            pass
    return d


def get_work_item(work_item_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
    return _row_to_dict(row) if row else None
