<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **coord-mcp** (170 symbols, 318 relationships, 20 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/coord-mcp/context` | Codebase overview, check index freshness |
| `gitnexus://repo/coord-mcp/clusters` | All functional areas |
| `gitnexus://repo/coord-mcp/processes` | All execution flows |
| `gitnexus://repo/coord-mcp/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

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