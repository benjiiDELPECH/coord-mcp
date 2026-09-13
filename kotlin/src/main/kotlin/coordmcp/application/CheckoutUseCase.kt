package coordmcp.application

import coordmcp.application.port.BodyOutcome
import coordmcp.application.port.DiffPort
import coordmcp.application.port.DiffRequest
import coordmcp.application.port.DiffResult
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.PullRequestOutcome
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.domain.CandidateScope
import coordmcp.domain.CiGate
import coordmcp.domain.CiGateDecision
import coordmcp.domain.AcceptanceCriteria
import coordmcp.domain.Conflict
import coordmcp.domain.ConflictDetector
import coordmcp.domain.PullRequestCollision
import coordmcp.domain.PullRequestOverlap
import coordmcp.domain.DomainResult
import coordmcp.domain.Refusal
import coordmcp.domain.Revision
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemId
import coordmcp.domain.WriteOutcome
import java.time.Instant

public data class CheckoutCommand(
    val workItemId: String,
    val expectedRevision: Revision?,
    /** Fichiers modifiés fournis explicitement : court-circuite la détection. */
    val diffFiles: List<String>? = null,
    val worktreePath: String? = null,
)

public sealed interface CheckoutResult {
    /** Transition effectuée. */
    public data class CheckedOut(
        val revision: Revision,
        val ciGate: CiGateDecision,
        /** Collisions entre le diff RÉEL et les autres travaux actifs. */
        val scopeConflicts: List<Conflict>,
        /** `null` si le diff n'a pas pu être résolu : on le dit. */
        val diffUnavailable: String?,
        val acceptance: AcceptanceCriteria?,
        /** `null` si le corps d'issue n'a pas pu être lu : les critères ne sont PAS « satisfaits ». */
        val acceptanceUnavailable: String?,
        val pullRequestCollisions: List<PullRequestCollision>,
        val pullRequestsUnavailable: String?,
    ) : CheckoutResult

    /**
     * Transition REFUSÉE par la barrière. Le statut n'a pas bougé : c'est la
     * différence entre signaler une contrainte et l'appliquer.
     */
    public data class Blocked(
        val blockers: List<String>,
        val ciGate: CiGateDecision,
    ) : CheckoutResult

    /** Le domaine a refusé la transition (état incompatible). */
    public data class Refused(val refusals: List<Refusal>) : CheckoutResult

    public data object NotFound : CheckoutResult

    public data class Stale(val expected: Revision, val current: Revision) : CheckoutResult
}

/**
 * CAS D'USAGE — `checkout_work`, la PORTE D'ARRIVÉE.
 *
 * C'est ici que la barrière de concurrence devient **mécanique** plutôt que
 * consultative. Avant, elle produisait un `WAIT` que l'appelant devait vouloir
 * respecter : ~35 travaux simultanés ont saturé Forgejo le 26.08.2026, parce
 * qu'une règle que rien n'impose n'est pas une règle.
 *
 * Ici, `WAIT` signifie que la transition N'A PAS LIEU. Le statut ne change pas.
 * Contourner n'est pas possible par indiscipline de l'appelant — il n'y a pas
 * d'appel qui le permette.
 */
public class CheckoutUseCase(
    private val reader: WorkItemReader,
    private val writer: WorkItemWriter,
    private val diffs: DiffPort,
    private val issues: IssueTrackerPort,
    private val ciLimit: Int = CiGate.DEFAULT_LIMIT,
    private val clock: () -> Instant = Instant::now,
) {

    public fun checkout(command: CheckoutCommand): CheckoutResult {
        val id = when (val r = WorkItemId.of(command.workItemId)) {
            is DomainResult.Ok -> r.value
            is DomainResult.Rejected -> return CheckoutResult.NotFound
        }

        val item = reader.findById(id) ?: return CheckoutResult.NotFound

        // La barrière, AVANT toute mutation.
        val gate = evaluateGate(item)
        if (gate is CiGateDecision.Wait) {
            return CheckoutResult.Blocked(
                listOf(BLOCKED_BY_CI.format(gate.activeCi, gate.limit)),
                gate,
            )
        }
        return applyTransition(item, gate, command)
    }

    /** Le travail consomme-t-il une place ? Si non, la barrière ne le concerne pas. */
    private fun evaluateGate(item: WorkItem): CiGateDecision {
        val activeCi = reader.countCiActive()
        return if (item.draft.triggersCi) {
            CiGate.evaluate(activeCi, ciLimit)
        } else {
            CiGateDecision.Proceed(activeCi, ciLimit)
        }
    }

    /**
     * Collisions entre ce qui a RÉELLEMENT changé et les autres travaux actifs.
     *
     * Différent de `checkin`, qui compare un périmètre DÉCLARÉ. Ici on compare le
     * diff observé : un agent qui déborde de son périmètre produit une collision
     * que la déclaration seule n'aurait jamais montrée.
     */
    private fun scopeConflicts(item: WorkItem, command: CheckoutCommand): Pair<List<Conflict>, String?> {
        val request = DiffRequest(
            repoPath = item.repo.raw,
            explicitFiles = command.diffFiles,
            worktreePath = command.worktreePath,
        )
        val diff = diffs.changedFiles(request)

        return when (diff) {
            is DiffResult.Unavailable -> emptyList<Conflict>() to diff.reason
            is DiffResult.Resolved -> {
                val others = reader.active().filter { it.id != item.id }
                val candidate = CandidateScope(
                    paths = diff.files + item.scope.paths,
                    symbols = item.draft.symbols.toSet(),
                )
                ConflictDetector.detectAll(candidate, others) to null
            }
        }
    }

    private data class ReviewSignals(
        val acceptance: AcceptanceCriteria?,
        val acceptanceUnavailable: String?,
        val pullRequests: List<PullRequestCollision>,
        val pullRequestsUnavailable: String?,
    )

    /**
     * Critères d'acceptance et PR concurrentes.
     *
     * Sans numéro d'issue rattaché, il n'y a rien à consulter — et ce n'est PAS
     * une indisponibilité : `null` des deux côtés dit « non applicable ».
     */
    private fun reviewSignals(item: WorkItem): ReviewSignals {
        val issueNumber = item.draft.issueNumber
            ?: return ReviewSignals(null, null, emptyList(), null)

        // UN appel par signal : les appeler deux fois doublerait les appels `gh`.
        val body = issues.issueBody(item.repo.raw, issueNumber)
        val scan = issues.openPullRequests(item.repo.raw)

        val acceptance = (body as? BodyOutcome.Fetched)?.let { AcceptanceCriteria.parse(it.body) }
        val pullRequests = (scan as? PullRequestOutcome.Fetched)?.let {
            PullRequestOverlap.detect(it.pullRequests, item.scope.paths.toSet() + item.draft.expandedFiles)
        }.orEmpty()

        return ReviewSignals(
            acceptance = acceptance,
            acceptanceUnavailable = (body as? BodyOutcome.Unavailable)?.reason,
            pullRequests = pullRequests,
            pullRequestsUnavailable = (scan as? PullRequestOutcome.Unavailable)?.reason,
        )
    }

    private fun applyTransition(
        item: WorkItem,
        gate: CiGateDecision,
        command: CheckoutCommand,
    ): CheckoutResult {
        val (conflicts, diffUnavailable) = scopeConflicts(item, command)
        val review = reviewSignals(item)
        val transition = when (val decision = item.checkOut(clock())) {
            is DomainResult.Rejected -> return CheckoutResult.Refused(decision.refusals)
            is DomainResult.Ok -> decision.value
        }

        return when (
            val outcome = writer.applyTransition(
                id = transition.id,
                next = transition.status,
                outcome = transition.outcome,
                at = transition.updatedAt,
                expected = command.expectedRevision,
            )
        ) {
            is WriteOutcome.Applied ->
                CheckoutResult.CheckedOut(
                    outcome.revision,
                    gate,
                    conflicts,
                    diffUnavailable,
                    review.acceptance,
                    review.acceptanceUnavailable,
                    review.pullRequests,
                    review.pullRequestsUnavailable,
                )
            is WriteOutcome.StaleRevision -> CheckoutResult.Stale(outcome.expected, outcome.current)
            WriteOutcome.NotFound -> CheckoutResult.NotFound
        }
    }

    private companion object {
        /** Message explicite : l'appelant doit comprendre que RIEN n'a bougé. */
        const val BLOCKED_BY_CI =
            "barrière CI saturée : %d travaux déjà en cours pour un seuil de %d. " +
                "Le statut n'a PAS été modifié."
    }
}
