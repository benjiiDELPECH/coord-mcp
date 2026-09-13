package coordmcp.application.port

import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import coordmcp.domain.WriteOutcome
import java.time.Instant

// ════════════════════════════════════════════════════════════════════════════
// PORTS SORTANTS — ce que les cas d'usage EXIGENT de la persistance.
//
// Ces interfaces appartiennent à l'application, pas à l'infrastructure. C'est ce
// qui rend l'architecture hexagonale : le sens de dépendance va de l'extérieur
// vers l'intérieur, jamais l'inverse. Concrètement, `CoordMcpServer` ne connaît
// plus `SqlDelightWorkItemRepository` — il connaît `WorkItemReader`.
//
// Bénéfice immédiat, pas théorique : la migration SQLite -> PostgreSQL devient
// l'ajout d'un adaptateur, pas la réécriture du cœur. Les deux peuvent coexister
// derrière le même port, ce qui rend la bascule réversible.
// ════════════════════════════════════════════════════════════════════════════

/** Lecture. Les requêtes simples passent par le port directement : en faire un cas d'usage serait de la cérémonie. */
public interface WorkItemReader {
    public fun findById(id: WorkItemId): WorkItem?

    public fun count(): Long

    /** Items non terminaux : `declared`, `claimed`, `in_progress`, `checked_out`. */
    public fun active(): List<WorkItem>

    /**
     * Nombre d'items `checked_out` qui DÉCLENCHENT une CI.
     *
     * Compté globalement, tous dépôts confondus : l'incident d'OOM du 26.08.2026
     * n'a pas respecté les frontières de dépôt, la barrière non plus.
     */
    public fun countCiActive(): Int
}

/** Données de création d'un travail. L'identifiant est généré, pas fourni par l'appelant. */
public data class NewWorkItem(
    public val id: WorkItemId,
    public val repo: Repo,
    public val title: String,
    public val scope: ScopeFiles,
    public val issueNumber: Int? = null,
    public val symbols: List<String> = emptyList(),
    public val expandedFiles: List<String> = emptyList(),
    /** Ressources d'infra occupées : troisième axe de périmètre, cf. CandidateScope. */
    public val resources: List<String> = emptyList(),
    public val agentId: String?,
    public val triggersCi: Boolean,
    public val at: Instant,
)

/** Écriture. Toute mutation passe par une garde de révision. */
public interface WorkItemWriter {
    public fun applyTransition(
        id: WorkItemId,
        next: WorkStatus,
        outcome: String?,
        at: Instant,
        expected: Revision?,
    ): WriteOutcome

    /**
     * Rattache un numéro d'issue à un travail déjà déclaré.
     *
     * Mutation DISTINCTE de la transition : elle ne change pas l'état du travail,
     * seulement son rattachement. La fondre dans `applyTransition` obligerait à
     * forcer un statut pour écrire un simple lien.
     */
    public fun linkIssue(
        id: WorkItemId,
        issueNumber: Int,
        at: Instant,
        expected: Revision?,
    ): WriteOutcome

    /**
     * Crée un travail en état `declared`.
     *
     * Pas de garde de révision : il n'y a rien à comparer sur un item qui n'existe
     * pas encore. L'unicité de l'identifiant est garantie par la clé primaire.
     */
    public fun create(item: NewWorkItem)
}
