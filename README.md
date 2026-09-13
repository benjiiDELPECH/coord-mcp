# coord-mcp

Central coordination MCP server for multi-agent / multi-session work : atomic ADR number allocation, work item declaration with conflict detection, checkin/checkout lifecycle, audit trail.

## Why

When several Claude Code sessions (or other LLM agents) work in parallel on the same set of repos, they collide on shared named resources : ADR numbers, branch names, GitHub issues. Filesystem scans and ad-hoc scripts can't guarantee atomicity. `coord-mcp` is a single broker process exposing typed MCP tools backed by PostgreSQL with `UNIQUE` constraints and retry-on-conflict loops.

Born out of a real incident: three collisions in a single day between two parallel Claude Code sessions sharing a working tree — one session's checkout stashed the other's uncommitted work, and an unresolved merge conflict broke a shared dev server. Git worktree isolation prevents *file*-level collisions, but not collisions on shared resources that live outside any single worktree — ports, caches, sequential ID allocation (ADR numbers), ratchet baselines. `coord-mcp` addresses that gap with explicit scope declaration and conflict detection *before* the collision, rather than isolation after the fact.

## Stack

- **Kotlin 2.4** + [MCP Kotlin SDK](https://github.com/modelcontextprotocol/kotlin-sdk)
- **PostgreSQL** — base `coord_mcp`
- **jOOQ** : les types sont générés **depuis la base réelle**. Une colonne
  renommée casse la compilation, pas la production. Le schéma n'est donc déclaré
  qu'une fois (`kotlin/migrations/pg/`), jamais dupliqué dans le code.
- `explicitApi()` + `allWarningsAsErrors` + Detekt : la rigueur est **imposée par
  le build**, pas promise par une convention.
- launchd auto-respawn (`kotlin/deploy/com.bdelpech.coord-mcp.plist`)
- HTTP transport on `127.0.0.1:8015`

> **Historique.** Ce service était en Python/SQLite. Il a été porté en
> Kotlin/PostgreSQL le 2026-09-13, avec double-run de comparaison (lectures *et*
> écritures) et bascule avec retour arrière vérifié. Le code Python reste dans
> l'historique Git, plus dans l'arbre de travail.

## Tools exposed (14)

| Tool | Phase | Purpose |
|---|---|---|
| `checkin` | departure | Declare scope, detect conflicts (paths **et** symboles), resolve `triggers_ci`, read prior decisions and similar issues |
| `claim_issue` | departure | Bind work item to existing GitHub issue (assigns @me) |
| `claim_new` | departure | Create new GitHub issue + bind |
| `claim_adr_number` | atomic | Allocate next free ADR number (UNIQUE + retry) |
| `checkout_work` | arrival | Validate diff vs scope, detect parallel PRs, parse acceptance criteria, enforce the global CI-concurrency gate |
| `release_work` | arrival | Close work item with outcome, optionally close GH issue, frees a CI-concurrency slot |
| `abandon_work` | arrival | Mark declared item as abandoned (motif obligatoire) |
| `relink_issue` | registry | Rebind an issue number without changing status |
| `list_active_work` | visibility | All non-terminal work items, most recent first |
| `get_work` | visibility | Inspect single work item by id |
| `plan_parallel_waves` | visibility | Partition active work into conflict-free waves |
| `list_adr_allocations` | registry | Cross-repo ADR number registry |
| `audit_tail` | debug | Last N audit log entries (bounded to 500) |
| `work_count` | visibility | Total work items |

## Install (macOS)

```bash
git clone <your-remote> ~/dev/github/coord-mcp
cd ~/dev/github/coord-mcp

# PostgreSQL : base + schéma
createdb coord_mcp
psql -d coord_mcp -f kotlin/migrations/pg/001_init.sql
psql -d coord_mcp -f kotlin/migrations/pg/002_audit_and_adr.sql

# Build de l'exécutable autonome
cd kotlin && gradle installDist

# Clé d'accès : un FICHIER en 0600, jamais dans le plist ni dans un .env versionné
install -d -m 700 ~/.config/coord-mcp
printf '%s' '' > ~/.config/coord-mcp/db_password
chmod 600 ~/.config/coord-mcp/db_password

# launchd
sed -e "s|__COORD_MCP_HOME__|$HOME/dev/github/coord-mcp|g" \
    deploy/com.example.coord-mcp.plist.template > ~/Library/LaunchAgents/com.you.coord-mcp.plist
launchctl load ~/Library/LaunchAgents/com.you.coord-mcp.plist
launchctl list | grep coord-mcp

# Register with Claude Code (~/.claude.json) : transport HTTP
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.claude.json'
d = json.loads(p.read_text())
d.setdefault('mcpServers', {})['coord-mcp'] = {'type': 'http', 'url': 'http://localhost:8015/mcp'}
p.write_text(json.dumps(d, indent=2))
"
```

Restart Claude Code after registration so the session discovers the `mcp__coord-mcp__*` tools.

### Configuration (environnement)

Toutes les variables sont **obligatoires** : le processus refuse de démarrer et
nomme celles qui manquent (`EX_CONFIG`, 78). Une base injoignable sort en
`EX_UNAVAILABLE` (69) — deux diagnostics distincts, parce que « corrige tes
variables » et « réveille la base » ne sont pas la même action.

| Variable | Rôle |
|---|---|
| `COORD_MCP_JDBC_URL` | `jdbc:postgresql://127.0.0.1:5432/coord_mcp` |
| `COORD_MCP_DB_USER` | rôle PostgreSQL |
| `COORD_MCP_DB_PASSWORD` **ou** `COORD_MCP_DB_PASSWORD_FILE` | exactement l'un des deux — définir les deux est une erreur |
| `COORD_MCP_CI_CONCURRENCY_LIMIT` | seuil du lanceur CI partagé, défaut **2** |
| `COORD_MCP_TRANSPORT` | `http` (défaut) ou `stdio` |
| `COORD_MCP_PORT` | défaut **8015** |
| `COORD_MCP_GRAPHITI_URL` | défaut `http://localhost:8001/mcp/` |

## Usage

### Allocate an ADR number atomically

```
mcp__coord-mcp__claim_adr_number(
    repo_path="/Users/you/dev/your-repo",
    topic="my new decision title",
)
# {"adr_number": 42, "filename": "ADR-042-my-new-decision-title.md"}
```

### Full coordination cycle

1. **checkin** — declare intent → returns conflicts, prior decisions, similar
   issues, CI-gate state and a suggested action.
2. **claim_new** or **claim_issue** — bind to GitHub.
3. … do the work …
4. **checkout_work** — gate before merge: validates the *actual git diff* against
   scope, finds parallel PRs, parses acceptance criteria.
5. **release_work** — close cycle with an outcome summary.

### Concurrency control

Every mutation increments `work_items.revision`. Passing `expectedRevision`
turns the write into a compare-and-swap: if another agent mutated the item in
between, the answer is `STALE_REVISION` and **nothing is applied**. Without it
the write is blind, and the response says so via `revision_checked: false`.

## CI-concurrency gate

Established 2026-08-27 after Forgejo (the shared Git+CI+registry server across alert-immo AND delpech-infra) went OOMKilled repeatedly — root cause: ~35 coord-mcp work items active simultaneously, each driving its own PR/CI-run/runner-poll cycle. A mechanical cap, not a rule agents have to remember:

- `checkin(..., triggers_ci)` — auto-detected from `scope_files` if omitted (workflow files, compiled/tested source extensions). The result carries a `ci_gate` field **separate** from `resolution` (scope conflicts).
- `checkout_work` **enforces** it: if the gate is saturated, the transition to `checked_out` does **not** happen — the status is unchanged, and `blockers` explains why. This is the difference between signalling a constraint and applying it.
- The counter is **global across every repo** — the 2026-08-26 OOM did not respect repo boundaries; neither does this cap.
- Threshold: `COORD_MCP_CI_CONCURRENCY_LIMIT`, default **2**.
- A work item that does **not** trigger CI is never gated: it does not consume a slot.

See `kotlin/src/test/kotlin/coordmcp/PostgresCheckoutTest.kt` and the doctrine block in `CLAUDE.md`/`AGENTS.md`.

## Data layout (PostgreSQL)

```
coord_mcp
├── work_items            declared/claimed/in_progress/checked_out/released/abandoned
│                         + revision (compare-and-swap) + CHECK constraints
├── adr_allocations       UNIQUE(repo_path, adr_number) — the atomic guarantee
├── migration_allocations UNIQUE(repo_path, version)
├── audit_log             every tool call with args, result, timestamp
├── outbox_events         événements à publier (idempotence par event_id)
└── consumer_inbox        déduplication côté consommateur
```

Deux invariants sont portés par la **base**, pas seulement par le code :
`terminal_implique_outcome` (un travail terminé porte une leçon) et
`updated_apres_created`. Aucun écrivain — même un script d'administration — ne
peut les enfreindre.

## Tests

```bash
createdb coord_mcp_test
psql -d coord_mcp_test -f kotlin/migrations/pg/001_init.sql
psql -d coord_mcp_test -f kotlin/migrations/pg/002_audit_and_adr.sql
cd kotlin && gradle test detekt
```

`ProtocolContractTest` interroge le **service en cours** avec un vrai client MCP
(SDK officiel) : lister les outils et appeler une fonction. C'est le seul test
qui attrape une incompatibilité d'enveloppe JSON-RPC — un double-run sur les
données ne la voit jamais.

## Roadmap

- `REVIEW_PRIOR_DECISIONS` branché sur Graphiti (fait) ; reste à mesurer son
  taux de faux positifs avant de le rendre bloquant.
- Préflight Context Compiler : mesurer d'abord la baseline sur les cas existants.
- Linux systemd-user equivalent of the launchd plist.

## License

MIT — see [LICENSE](./LICENSE).
