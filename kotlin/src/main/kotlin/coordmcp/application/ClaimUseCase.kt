package coordmcp.application

import coordmcp.application.port.IssueOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewIssueRequest
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.domain.DomainResult
import coordmcp.domain.Revision
import coordmcp.domain.WorkItemId
import coordmcp.domain.WriteOutcome
import java.time.Instant

public data class ClaimExistingCommand(
    val workItemId: String,
    val repoPath: String,
    val issueNumber: Int,
    val expectedRevision: Revision?,
)

public data class ClaimNewCommand(
    val workItemId: String,
    val repoPath: String,
    val title: String,
    val body: String,
    val labels: List<String>,
    val milestoneNumber: Int?,
    val expectedRevision: Revision?,
)

public sealed interface ClaimResult {
    public data class Bound(val issueNumber: Int, val revision: Revision) : ClaimResult

    /** Le suivi d'issues a échoué — rien n'a été rattaché en base. */
    public data class TrackerFailed(val detail: String) : ClaimResult

    public data class Invalid(val detail: String) : ClaimResult

    public data object NotFound : ClaimResult

    public data class Stale(val expected: Revision, val current: Revision) : ClaimResult
}

/**
 * CAS D'USAGE — rattacher un travail à une issue.
 *
 * L'ORDRE est le contrat : on ne lie la base qu'APRÈS confirmation du suivi
 * d'issues. L'inverse laisserait un travail pointant vers une issue inexistante —
 * le lien mort est pire que l'absence de lien, parce qu'il a l'air valide.
 */
public class ClaimUseCase(
    private val reader: WorkItemReader,
    private val writer: WorkItemWriter,
    private val tracker: IssueTrackerPort,
    private val clock: () -> Instant = Instant::now,
) {

    public fun claimExisting(command: ClaimExistingCommand): ClaimResult {
        if (command.issueNumber <= 0) {
            return ClaimResult.Invalid("issue_number doit être strictement positif")
        }
        val id = parseId(command.workItemId) ?: return ClaimResult.Invalid("work_item_id invalide")
        reader.findById(id) ?: return ClaimResult.NotFound

        return when (val outcome = tracker.assignToMe(command.repoPath, command.issueNumber)) {
            is IssueOutcome.Failed -> ClaimResult.TrackerFailed(outcome.detail)
            is IssueOutcome.Ok -> bind(id, outcome.issueNumber, command.expectedRevision)
        }
    }

    public fun claimNew(command: ClaimNewCommand): ClaimResult {
        if (command.title.isBlank()) return ClaimResult.Invalid("title est requis")
        val id = parseId(command.workItemId) ?: return ClaimResult.Invalid("work_item_id invalide")
        reader.findById(id) ?: return ClaimResult.NotFound

        val request = NewIssueRequest(
            repoPath = command.repoPath,
            title = command.title,
            body = command.body,
            labels = command.labels,
            milestoneNumber = command.milestoneNumber,
        )

        return when (val outcome = tracker.createIssue(request)) {
            is IssueOutcome.Failed -> ClaimResult.TrackerFailed(outcome.detail)
            is IssueOutcome.Ok -> bind(id, outcome.issueNumber, command.expectedRevision)
        }
    }

    private fun bind(id: WorkItemId, issueNumber: Int, expected: Revision?): ClaimResult =
        when (val outcome = writer.linkIssue(id, issueNumber, clock(), expected)) {
            is WriteOutcome.Applied -> ClaimResult.Bound(issueNumber, outcome.revision)
            is WriteOutcome.StaleRevision -> ClaimResult.Stale(outcome.expected, outcome.current)
            WriteOutcome.NotFound -> ClaimResult.NotFound
        }

    private fun parseId(raw: String): WorkItemId? = (WorkItemId.of(raw) as? DomainResult.Ok)?.value
}
