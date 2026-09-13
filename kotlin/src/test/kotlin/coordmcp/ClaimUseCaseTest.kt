package coordmcp

import coordmcp.adapter.outbound.gh.GhIssueTrackerAdapter
import coordmcp.application.ClaimExistingCommand
import coordmcp.application.ClaimNewCommand
import coordmcp.application.ClaimResult
import coordmcp.application.ClaimUseCase
import coordmcp.application.port.BodyOutcome
import coordmcp.application.port.IssueSearchOutcome
import coordmcp.application.port.IssueOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewIssueRequest
import coordmcp.application.port.PullRequestOutcome
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemDraft
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import coordmcp.domain.WriteOutcome
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Le cas d'usage de rattachement, testé SANS base de données.
 *
 * C'est le bénéfice concret de l'hexagonal : le lecteur, l'écrivain et le suivi
 * d'issues sont des faux en mémoire, donc l'ORDRE des opérations se vérifie en
 * quelques millisecondes. Cet ordre est le contrat : on ne lie la base qu'après
 * confirmation du suivi d'issues.
 */
class ClaimUseCaseTest {

    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    private class FakeTracker(var outcome: IssueOutcome = IssueOutcome.Ok(42)) : IssueTrackerPort {
        var assigned: Int? = null
        var created: NewIssueRequest? = null
        override fun assignToMe(repoPath: String, issueNumber: Int): IssueOutcome {
            assigned = issueNumber
            return outcome
        }
        override fun createIssue(request: NewIssueRequest): IssueOutcome {
            created = request
            return outcome
        }
        override fun issueBody(repoPath: String, issueNumber: Int) = BodyOutcome.Fetched("")
        override fun openPullRequests(repoPath: String) = PullRequestOutcome.Fetched(emptyList())
        override fun searchOpenIssues(repoPath: String, query: String, limit: Int) =
            IssueSearchOutcome.Found(emptyList())
    }

    private class FakeWriter : WorkItemWriter {
        var linked: Pair<WorkItemId, Int>? = null
        var outcome: WriteOutcome = WriteOutcome.Applied((Revision.of(2) as DomainResult.Ok).value)
        override fun applyTransition(
            id: WorkItemId,
            next: WorkStatus,
            outcome: String?,
            at: Instant,
            expected: Revision?,
        ): WriteOutcome = this.outcome
        override fun linkIssue(id: WorkItemId, issueNumber: Int, at: Instant, expected: Revision?): WriteOutcome {
            linked = id to issueNumber
            return outcome
        }
        override fun create(item: coordmcp.application.port.NewWorkItem) = Unit
    }

    private class FakeReader(private val item: WorkItem?) : WorkItemReader {
        override fun findById(id: WorkItemId) = item
        override fun count() = if (item == null) 0L else 1L
        override fun active() = listOfNotNull(item)
        override fun countCiActive() = 0
    }

    private fun item(id: String = "wi_test000001"): WorkItem = WorkItem.of(
        id = (WorkItemId.of(id) as DomainResult.Ok).value,
        status = WorkStatus.DECLARED,
        outcome = null,
        draft = WorkItemDraft(
            (Repo.of("/tmp/repo") as DomainResult.Ok).value,
            "titre",
            (ScopeFiles.parse(null) as DomainResult.Ok).value,
            at.minusSeconds(3600),
            at.minusSeconds(3600),
        ),
        revision = (Revision.of(1) as DomainResult.Ok).value,
    )

    private fun useCase(tracker: FakeTracker, writer: FakeWriter, present: Boolean = true) =
        ClaimUseCase(FakeReader(if (present) item() else null), writer, tracker, clock = { at })

    @Test
    fun `claim_issue rattache l'issue apres confirmation du suivi`() {
        val tracker = FakeTracker(IssueOutcome.Ok(1578))
        val writer = FakeWriter()

        val result = useCase(tracker, writer).claimExisting(
            ClaimExistingCommand("wi_test000001", "/tmp/repo", 1578, null),
        )

        assertEquals(1578, assertIs<ClaimResult.Bound>(result).issueNumber)
        assertEquals(1578, tracker.assigned)
        assertEquals(1578, writer.linked?.second, "le lien doit porter le numéro CONFIRMÉ")
    }

    @Test
    fun `si le suivi echoue, la base n'est PAS liee`() {
        val tracker = FakeTracker(IssueOutcome.Failed("gh non authentifié"))
        val writer = FakeWriter()

        val result = useCase(tracker, writer).claimExisting(
            ClaimExistingCommand("wi_test000001", "/tmp/repo", 1578, null),
        )

        assertIs<ClaimResult.TrackerFailed>(result)
        // L'ORDRE est le contrat : un lien mort est pire que pas de lien, il a
        // l'air valide.
        assertNull(writer.linked, "aucune écriture ne doit avoir eu lieu")
    }

    @Test
    fun `un numero d'issue invalide est refuse avant tout appel`() {
        val tracker = FakeTracker()
        val writer = FakeWriter()

        val result = useCase(tracker, writer).claimExisting(
            ClaimExistingCommand("wi_test000001", "/tmp/repo", 0, null),
        )

        assertIs<ClaimResult.Invalid>(result)
        assertNull(tracker.assigned, "on n'appelle pas le suivi pour un numéro invalide")
        assertNull(writer.linked)
    }

    @Test
    fun `claim_new transmet le jalon et les labels au suivi`() {
        val tracker = FakeTracker(IssueOutcome.Ok(900))
        val writer = FakeWriter()

        val result = useCase(tracker, writer).claimNew(
            ClaimNewCommand("wi_test000001", "/tmp/repo", "titre", "corps", listOf("bug"), 12, null),
        )

        assertEquals(900, assertIs<ClaimResult.Bound>(result).issueNumber)
        assertEquals(12, tracker.created?.milestoneNumber)
        assertEquals(listOf("bug"), tracker.created?.labels)
    }

    @Test
    fun `claim_new sans titre est refuse`() {
        val tracker = FakeTracker()
        val writer = FakeWriter()

        val result = useCase(tracker, writer).claimNew(
            ClaimNewCommand("wi_test000001", "/tmp/repo", "   ", "", emptyList(), null, null),
        )

        assertIs<ClaimResult.Invalid>(result)
        assertNull(tracker.created)
    }

    @Test
    fun `une revision perimee remonte telle quelle`() {
        val tracker = FakeTracker()
        val writer = FakeWriter().apply {
            outcome = WriteOutcome.StaleRevision(
                (Revision.of(1) as DomainResult.Ok).value,
                (Revision.of(5) as DomainResult.Ok).value,
            )
        }

        val result = useCase(tracker, writer).claimExisting(
            ClaimExistingCommand("wi_test000001", "/tmp/repo", 1578, null),
        )

        assertIs<ClaimResult.Stale>(result)
    }

    // ── Résolution du slug de remote : deux conventions coexistent ---

    @Test
    fun `le slug est extrait des deux formes de remote`() {
        val adapter = GhIssueTrackerAdapter()
        assertEquals("benjamin/alert-immo", adapter.slugOfRemote("git@git.delpech.dev:benjamin/alert-immo.git"))
        assertEquals("benjamin/alert-immo", adapter.slugOfRemote("https://git.delpech.dev/benjamin/alert-immo.git"))
        assertEquals("owner/repo", adapter.slugOfRemote("https://github.com/owner/repo"))
        assertTrue(adapter.slugOfRemote("pas-un-remote") == null)
    }
}
