# coord-mcp

Central coordination MCP server for multi-agent / multi-session work : atomic ADR number allocation, work item declaration with conflict detection, checkin/checkout lifecycle, audit trail.

## Why

When several Claude Code sessions (or other LLM agents) work in parallel on the same set of repos, they collide on shared named resources : ADR numbers, branch names, GitHub issues. Filesystem scans and ad-hoc scripts can't guarantee atomicity. `coord-mcp` is a single broker process exposing typed MCP tools backed by SQLite with `UNIQUE` constraints and retry-on-conflict loops.

Born out of a real incident: three collisions in a single day between two parallel Claude Code sessions sharing a working tree — one session's checkout stashed the other's uncommitted work, and an unresolved merge conflict broke a shared dev server. Git worktree isolation prevents *file*-level collisions, but not collisions on shared resources that live outside any single worktree — ports, caches, sequential ID allocation (ADR numbers), ratchet baselines. `coord-mcp` addresses that gap with explicit scope declaration and conflict detection *before* the collision, rather than isolation after the fact.

## Stack

- Python 3.13 + [FastMCP](https://github.com/modelcontextprotocol/python-sdk) (MCP SDK)
- SQLite (WAL mode) at `~/.coord-mcp/state.db`
- launchd auto-respawn (`deploy/com.example.coord-mcp.plist.template`)
- HTTP transport on `127.0.0.1:8015`

## Tools exposed (11)

| Tool | Phase | Purpose |
|---|---|---|
| `checkin` | departure | Declare scope, detect conflicts via overlap on `scope_files`, suggest existing issues, resolve `triggers_ci` |
| `claim_issue` | departure | Bind work item to existing GitHub issue (assigns @me) |
| `claim_new` | departure | Create new GitHub issue + bind |
| `claim_adr_number` | atomic | Allocate next free ADR number (UNIQUE + retry) |
| `checkout_work` | arrival | Validate diff vs scope, detect parallel PRs, parse AC, enforce global CI-concurrency gate |
| `release_work` | arrival | Close work item with outcome, optionally close GH issue, frees a CI-concurrency slot |
| `abandon_work` | arrival | Mark declared item as abandoned |
| `list_active_work` | visibility | All non-terminal work items, optionally per-repo |
| `get_work` | visibility | Inspect single work item by id |
| `list_adr_allocations` | registry | Cross-repo ADR number registry |
| `audit_tail` | debug | Last N audit log entries |

## Install (macOS)

```bash
# Clone (replace with your own fork/remote if you forked it)
git clone https://github.com/benjiiDELPECH/coord-mcp.git ~/dev/github/coord-mcp
cd ~/dev/github/coord-mcp

# Venv + deps
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Register with Claude Code (~/.claude.json)
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.claude.json'
d = json.loads(p.read_text())
d.setdefault('mcpServers', {})['coord-mcp'] = {
    'type': 'http',
    'url': 'http://localhost:8015/mcp'
}
p.write_text(json.dumps(d, indent=2))
"

# Install launchd plist (persistent, auto-start at login)
# Substitute the placeholders for your own paths, then install:
sed -e "s|__COORD_MCP_HOME__|$HOME/dev/github/coord-mcp|g" \
    -e "s|__HOME__|$HOME|g" \
    deploy/com.example.coord-mcp.plist.template > ~/Library/LaunchAgents/com.you.coord-mcp.plist
launchctl load ~/Library/LaunchAgents/com.you.coord-mcp.plist
launchctl list | grep coord-mcp   # verify

# Verify
curl -sS -X POST http://127.0.0.1:8015/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
```

Restart Claude Code after registration so the session discovers the `mcp__coord-mcp__*` tools.

## Usage

### Allocate an ADR number atomically

```python
from coord_mcp.adr import claim_adr
result = claim_adr(
    repo_path="/Users/you/dev/your-repo",
    topic="my new decision title",
)
# {'adr_number': 42, 'filename': 'ADR-042-my-new-decision-title.md', ...}
```

Or via MCP from Claude :

```
mcp__coord-mcp__claim_adr_number(
    repo_path="/Users/you/dev/your-repo",
    topic="my new decision title",
)
```

### Full coordination cycle

1. **checkin** — declare intent : `checkin(repo_path, title, scope_files=[...])` → returns conflicts + suggested action.
2. **claim_new** or **claim_issue** — bind to GitHub.
3. … do the work …
4. **checkout_work** — gate before merge : validates diff vs scope, finds parallel PRs.
5. **release_work** — close cycle with outcome summary.

## CI-concurrency gate

Established 2026-08-27 after Forgejo (the shared Git+CI+registry server across alert-immo AND delpech-infra) went OOMKilled repeatedly — root cause: ~35 coord-mcp work items active simultaneously, each driving its own PR/CI-run/runner-poll cycle. A mechanical cap, not a rule agents have to remember:

- `checkin(..., triggers_ci: bool | None = None)` — auto-detected from `scope_files` if omitted (workflow files, compiled/tested source extensions), overridable explicitly. Result carries `ci_gate: {strategy, you_should: "PROCEED"|"WAIT", wait_on, active_ci_count, limit}` — separate from `resolution` (scope conflicts).
- `checkout_work` enforces it: if `ci_gate.you_should == "WAIT"`, the status transition to `checked_out` is withheld (no CI slot consumed), `ready_to_merge=False`, `blockers` names which work items to wait on.
- The counter is **global across every repo** — counts `status='checked_out'` rows with `triggers_ci=1`, no `repo` filter. The 2026-08-26 OOM did not respect repo boundaries; neither does this cap.
- Threshold: `$COORD_MCP_CI_CONCURRENCY_LIMIT`, default **2**.
- Non-blocking by design: coord-mcp never sleeps/polls waiting for a slot — it returns the `WAIT` signal immediately. The caller (agent/orchestrator) is responsible for respecting it before pushing commits / opening a PR. `release_work` frees a slot for the next queued `checkout_work`.

See `tests/test_ci_concurrency.py` and the doctrine block in `CLAUDE.md`/`AGENTS.md`.

## Data layout

```
~/.coord-mcp/state.db   SQLite WAL
├── work_items          declared/claimed/in_progress/checked_out/released/abandoned
├── adr_allocations     UNIQUE(repo_path, adr_number) — the atomic guarantee
└── audit_log           every tool call with args, result, timestamp
```

## Roadmap

- Graphiti integration on `release_work` (push outcome to long-term memory)
- Pre-commit / pre-push hooks for hard enforcement
- Grafana dashboard for active conflicts visibility
- Linux systemd-user equivalent of the launchd plist

## License

MIT — see [LICENSE](./LICENSE).
