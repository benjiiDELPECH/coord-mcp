package coordmcp.application

import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.domain.DomainResult
import coordmcp.domain.Refusal
import coordmcp.domain.Revision
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemId
import coordmcp.domain.WriteOutcome
import java.time.Instant

/** Issue d'une commande, avant toute sérialisation. */
public sealed interface WorkActionResult {
    /**
     * Mutation appliquée. Porte le STATUT RÉSULTANT, pas seulement la révision.
     *
     * Sans lui, la couche MCP devait deviner — et devinait `released` en dur,
     * pour toute écriture. `abandon_work` annonçait donc `released` : le domaine
     * distingue pourtant RELEASED et ABANDONED, deux états terminaux qui ne
     * veulent pas dire la même chose. L'information existait ici, à deux lignes
     * de la sortie, et était jetée.
     */
    public data class Done(
        public val outcome: WriteOutcome,
        public val status: coordmcp.domain.WorkStatus,
    ) : WorkActionResult

    /** Le domaine a refusé — la base n'a pas été touchée. */
    public data class Refused(public val refusals: List<Refusal>) : WorkActionResult

    public data object NotFound : WorkActionResult
}

/**
 * CAS D'USAGE — les transitions de vie d'un travail.
 *
 * Il ne connaît que des ports. Aucune mention de SQL, de jOOQ, de PostgreSQL ou
 * de SQLite : changer de moteur ne le touche pas. Ce n'est pas une figure de
 * style — on est en train de changer de moteur.
 *
 * `release` et `abandon` partagent la même forme : lire, laisser le DOMAINE
 * trancher, écrire seulement s'il accepte. Les dupliquer en deux classes
 * reviendrait à laisser dériver deux fois le même enchaînement.
 */
public class WorkLifecycleUseCase(
    private val reader: WorkItemReader,
    private val writer: WorkItemWriter,
) {

    public fun release(
        id: WorkItemId,
        lesson: String,
        at: Instant,
        expected: Revision?,
    ): WorkActionResult = apply(id, expected) { it.release(lesson, at) }

    /** Le motif est obligatoire : un abandonné muet est indistinguable d'un perdu. */
    public fun abandon(
        id: WorkItemId,
        reason: String,
        at: Instant,
        expected: Revision?,
    ): WorkActionResult = apply(id, expected) { it.abandon(reason, at) }

    /**
     * Enchaînement commun. Le domaine produit l'item SUIVANT ; l'adaptateur
     * n'écrit que le champ correspondant, sous la même garde de révision.
     */
    private fun apply(
        id: WorkItemId,
        expected: Revision?,
        decide: (WorkItem) -> DomainResult<WorkItem>,
    ): WorkActionResult {
        val item = reader.findById(id) ?: return WorkActionResult.NotFound

        return when (val decision = decide(item)) {
            is DomainResult.Rejected -> WorkActionResult.Refused(decision.refusals)
            is DomainResult.Ok -> {
                val next = decision.value
                WorkActionResult.Done(
                    outcome = writer.applyTransition(
                        id = next.id,
                        next = next.status,
                        outcome = next.outcome,
                        at = next.updatedAt,
                        expected = expected,
                    ),
                    status = next.status,
                )
            }
        }
    }

}
