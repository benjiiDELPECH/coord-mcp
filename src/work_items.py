"""
Work items — checkin / claim / list / checkout / release.

Each work item models a unit of agent activity. Lifecycle:

    declared → claimed → in_progress → checked_out → released
                                                     └─→ abandoned

Conflict detection on checkin = overlap on `scope_files` with other active items.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

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
    """Return active work items in the same repo with overlapping scope_files."""
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
        other_files = json.loads(row["scope_files"] or "[]")
        overlap = scope_set & set(other_files)
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
    scope_adr_topic: str | None = None,
    milestone_number: int | None = None,
    eta_hours: float | None = None,
    agent_id: str | None = None,
) -> dict:
    """Declare intent to work on something. Returns conflicts + suggestions.

    Does NOT mutate GitHub yet. The caller (or the user) then chooses to:
    - claim an existing issue (call `claim_issue`)
    - create a new issue (call `claim_new`)
    - abort (call `abandon`)
    """
    repo_path_abs = str(Path(repo_path).resolve())
    repo_slug = _detect_repo_slug(repo_path_abs)
    scope_files = scope_files or []

    conflicts = _find_conflicts(repo_path_abs, scope_files)
    similar_issues = _find_similar_issues(repo_slug, title) if repo_slug else []

    wi_id = _generate_id()
    with connection() as conn:
        conn.execute(
            "INSERT INTO work_items "
            "(id, repo, title, scope_files, scope_adr_topic, milestone_number, "
            " agent_id, status, eta_hours, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'declared', ?, ?, ?)",
            (wi_id, repo_path_abs, title, json.dumps(scope_files),
             scope_adr_topic, milestone_number, agent_id, eta_hours,
             now_iso(), now_iso()),
        )

    result = {
        "work_item_id": wi_id,
        "repo_path": repo_path_abs,
        "repo_slug": repo_slug,
        "title": title,
        "status": "declared",
        "conflicts": conflicts,
        "similar_existing_issues": similar_issues,
        "suggested_action": _suggest_action(conflicts, similar_issues),
        "next_call": {
            "to_claim_existing": "claim_issue(work_item_id, github_issue_number)",
            "to_create_new": "claim_new(work_item_id, title, body)",
            "to_abort": "abandon(work_item_id, reason)",
        },
    }
    log_audit("checkin", args={"repo_path": repo_path_abs, "title": title},
              result=result, agent_id=agent_id, work_item_id=wi_id)
    return result


def _suggest_action(conflicts: list[dict], similar: list[dict]) -> str:
    if conflicts:
        return "REVIEW_CONFLICTS — overlapping scope detected, decide whether to abort or coordinate"
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
            args += ["--milestone", str(row["milestone_number"])]
        if labels:
            for lbl in labels:
                args += ["--label", lbl]

        out = _gh(args, json_out=False)
        if not out:
            return {"error": "gh issue create failed"}
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
