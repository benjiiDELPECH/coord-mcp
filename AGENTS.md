<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **coord-mcp** (1807 symbols, 3454 relationships, 92 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/coord-mcp/context` | Codebase overview, check index freshness |
| `gitnexus://repo/coord-mcp/clusters` | All functional areas |
| `gitnexus://repo/coord-mcp/processes` | All execution flows |
| `gitnexus://repo/coord-mcp/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

---

# 🚦 CI-concurrency gate — MUST-RESPECT, not advisory (établi 2026-08-27)

> **Contexte** : Forgejo (le serveur Git+CI+registre partagé entre alert-immo ET
> delpech-infra) est tombé en `OOMKilled` répété le 2026-08-26. Cause racine :
> ~35 work items coord-mcp actifs simultanément, chacun avec son propre
> PR/run CI/polling runner, ont saturé la mémoire du process Forgejo — alors
> que le node K8s lui-même n'était qu'à 38% de charge. La doctrine existait
> déjà (mémoire Graphiti `alert_immo`
> `feedback_parallelisme_agents_vs_parallelisme_ci` : "4-5 agents OK, CI
> lourdes simultanées max 1-2") mais n'était qu'une règle que l'orchestrateur
> devait se rappeler d'appliquer — rien ne l'imposait mécaniquement.

## Mécanisme

- `checkin(..., triggers_ci: bool | None)` : si omis, auto-détecté depuis
  `scope_files` (`work_items._auto_detect_triggers_ci` — fichiers
  `.github/workflows/`, extensions de code compilé/testé). Le résultat porte
  un champ **`ci_gate`** distinct du champ `resolution` (qui ne couvre que
  les conflits de scope) : `{strategy, you_should: "PROCEED"|"WAIT",
  wait_on, active_ci_count, limit}`.
- `checkout_work` **applique** la barrière : si `ci_gate.you_should ==
  "WAIT"`, la transition de statut vers `checked_out` **n'a PAS lieu** — le
  work item reste à son statut précédent, `blockers` explique pourquoi,
  `ready_to_merge=False`. Réessayer `checkout_work` après qu'un des
  `ci_gate.wait_on` ait appelé `release_work`.
- Le compteur est **GLOBAL au serveur** (tous repos confondus, pas de filtre
  par `repo`) : compte les work items `status='checked_out'` avec
  `triggers_ci=1`. Global par construction — l'OOM du 26.08 n'a pas respecté
  les frontières de repo, la barrière non plus.
- Seuil configurable : `$COORD_MCP_CI_CONCURRENCY_LIMIT`, défaut **2**.
- Le signal ne bloque JAMAIS le process serveur (pas de sleep/poll côté
  coord-mcp) — `checkin`/`checkout_work` répondent immédiatement. C'est
  **l'appelant** (agent/orchestrateur) qui doit respecter `WAIT` et ne pas
  pousser de commit / ouvrir de PR tant qu'un slot ne s'est pas libéré.

## Always Do

- **MUST traiter `ci_gate.you_should == "WAIT"` comme un blocage réel**, pas
  un bruit consultatif — c'est exactement la classe d'incident (barrière
  mécanique contournable par indiscipline) que ce mécanisme existe pour
  fermer, dans l'esprit `feedback_doctrine_must_be_mechanical`.
- **MUST attendre qu'un des work items listés dans `ci_gate.wait_on` appelle
  `release_work`** avant de relancer `checkout_work`, plutôt que de pousser
  un commit / ouvrir une PR pendant l'attente.

## Never Do

- **NEVER** contourner un `WAIT` en appelant `checkout_work` avec des
  `diff_files` ou un `worktree_path` différents dans l'espoir de forcer le
  passage — le gate est calculé sur l'état global de la table
  `work_items`, pas sur les arguments de l'appel.
- **NEVER** régresser `triggers_ci` à `False` juste pour éviter la barrière
  sur un work item qui déclenche réellement une CI lourde — ça reproduit
  exactement l'incident du 26.08.

Voir tests : `kotlin/src/test/kotlin/coordmcp/PostgresCheckoutTest.kt`.
