package coordmcp

import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.adapter.outbound.postgres.JooqWorkItemAdapter
import coordmcp.application.CheckoutCommand
import coordmcp.application.CheckoutResult
import coordmcp.application.CheckoutUseCase
import coordmcp.application.port.BodyOutcome
import coordmcp.application.port.IssueSearchOutcome
import coordmcp.application.port.DiffPort
import coordmcp.application.port.IssueOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewIssueRequest
import coordmcp.application.port.PullRequestOutcome
import coordmcp.application.port.DiffRequest
import coordmcp.application.port.DiffResult
import coordmcp.application.port.DiffSource
import coordmcp.domain.DomainResult
import coordmcp.domain.Revision
import java.sql.DriverManager
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * `checkout_work` — la barrière de concurrence CI, APPLIQUÉE.
 *
 * L'assertion centrale de ce fichier est celle qui vérifie que le STATUT N'A PAS
 * BOUGÉ quand la barrière est saturée. Signaler une contrainte ne suffit pas :
 * le 26.08.2026, la règle existait et Forgejo est tombé en OOM parce que rien ne
 * l'imposait. Un `WAIT` que l'appelant peut ignorer n'est pas une barrière.
 */
class PostgresCheckoutTest {

    private val url = System.getenv("COORD_MCP_PG_TEST_URL")
        ?: "jdbc:postgresql://127.0.0.1:5432/coord_mcp_test"
    private val user = System.getenv("COORD_MCP_DB_USER") ?: System.getProperty("user.name")
    private val repo = "/tmp/coord-checkout-test"

    private lateinit var database: JooqDatabase
    private lateinit var adapter: JooqWorkItemAdapter
    private lateinit var checkout: CheckoutUseCase

    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    @BeforeTest
    fun setUp() {
        execute("DELETE FROM work_items")
        database = JooqDatabase(url, user)
        adapter = JooqWorkItemAdapter(database)
        checkout = CheckoutUseCase(adapter, adapter, FakeDiff(), FakeIssues(), ciLimit = 2, clock = { at })
    }

    @AfterTest
    fun tearDown() {
        execute("DELETE FROM work_items")
        database.close()
    }

    @Test
    fun `barriere saturee — le statut N'EST PAS modifie`() {
        insert("wi_cible000001", status = "declared", triggersCi = true)
        insert("wi_occupant0001", status = "checked_out", triggersCi = true)
        insert("wi_occupant0002", status = "checked_out", triggersCi = true)

        val result = checkout.checkout(CheckoutCommand("wi_cible000001", rev(1)))

        val blocked = assertIs<CheckoutResult.Blocked>(result)
        assertEquals(2, blocked.ciGate.activeCi)
        assertEquals(2, blocked.ciGate.limit)

        // L'ASSERTION QUI COMPTE : la barrière n'a pas seulement parlé, elle a agi.
        assertEquals(
            "declared",
            statusOf("wi_cible000001"),
            "la transition a eu lieu malgré la barrière — c'est exactement l'incident du 26.08",
        )
        assertEquals("1", revisionOf("wi_cible000001"), "un blocage n'incrémente pas la révision")
    }

    @Test
    fun `sous la barriere la transition a lieu`() {
        insert("wi_cible000001", status = "declared", triggersCi = true)

        val result = checkout.checkout(CheckoutCommand("wi_cible000001", rev(1)))

        assertIs<CheckoutResult.CheckedOut>(result)
        assertEquals("checked_out", statusOf("wi_cible000001"))
        assertEquals("2", revisionOf("wi_cible000001"))
    }

    @Test
    fun `un travail sans CI passe meme si la barriere est saturee`() {
        insert("wi_cible000001", status = "declared", triggersCi = false)
        insert("wi_occupant0001", status = "checked_out", triggersCi = true)
        insert("wi_occupant0002", status = "checked_out", triggersCi = true)

        val result = checkout.checkout(CheckoutCommand("wi_cible000001", rev(1)))

        assertIs<CheckoutResult.CheckedOut>(result)
        assertEquals("checked_out", statusOf("wi_cible000001"))
    }

    @Test
    fun `une revision perimee rejette sans modifier le statut`() {
        insert("wi_cible000001", status = "declared", triggersCi = false)

        val result = checkout.checkout(CheckoutCommand("wi_cible000001", rev(99)))

        assertIs<CheckoutResult.Stale>(result)
        assertEquals("declared", statusOf("wi_cible000001"))
    }

    @Test
    fun `un etat incompatible est refuse par le domaine, pas par un cas particulier`() {
        // `released` est terminal : aucune transition n'en sort.
        insert("wi_cible000001", status = "released", triggersCi = false)

        val result = checkout.checkout(CheckoutCommand("wi_cible000001", rev(1)))

        assertIs<CheckoutResult.Refused>(result)
        assertEquals("released", statusOf("wi_cible000001"))
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private fun rev(v: Long): Revision = (Revision.of(v) as DomainResult.Ok).value

    /** `released` exige un outcome : la contrainte de table le rappelle à l'ordre. */
    private fun insert(id: String, status: String, triggersCi: Boolean) {
        val released = status == "released"
        val outcomeColumn = if (released) ", outcome" else ""
        val outcomeValue = if (released) ", 'livré'" else ""

        // Horodatage ANTÉRIEUR à l'horloge du test (12:00Z) : le domaine refuse un
        // `updated_at` antérieur au `created_at`, et il a raison — c'est le même
        // contrat que la contrainte `updated_apres_created` en base.
        execute(
            "INSERT INTO work_items " +
                "(id, repo, title, status, triggers_ci$outcomeColumn, created_at, updated_at, revision) " +
                "VALUES ('$id', '$repo', 'titre', '$status', $triggersCi$outcomeValue, " +
                "'2026-09-13T10:00:00Z', '2026-09-13T10:00:00Z', 1)",
        )
    }

    private fun statusOf(id: String): String? = column(id, "status")

    private fun revisionOf(id: String): String? = column(id, "revision")

    private fun column(id: String, name: String): String? = query { s ->
        s.executeQuery("SELECT $name FROM work_items WHERE id='$id'").use { rs ->
            if (rs.next()) rs.getString(name) else null
        }
    }

    /** Un seul point d'accès JDBC : les `use` imbriqués finissaient par masquer la logique. */
    private fun <T> query(block: (java.sql.Statement) -> T): T =
        DriverManager.getConnection(url, user, "").use { c -> c.createStatement().use(block) }

    private fun execute(sql: String) {
        query { s -> s.execute(sql) }
    }

    /** Suivi d'issues factice : aucune PR, aucun corps — donc rien à consulter. */
    private class FakeIssues : IssueTrackerPort {
        override fun assignToMe(repoPath: String, issueNumber: Int) = IssueOutcome.Ok(issueNumber)
        override fun createIssue(request: NewIssueRequest) = IssueOutcome.Ok(1)
        override fun issueBody(repoPath: String, issueNumber: Int) = BodyOutcome.Fetched("")
        override fun openPullRequests(repoPath: String) = PullRequestOutcome.Fetched(emptyList())
        override fun searchOpenIssues(repoPath: String, query: String, limit: Int) =
            IssueSearchOutcome.Found(emptyList())
    }

    /** Diff factice : par défaut aucune modification, donc aucune collision. */
    private class FakeDiff(
        private val result: DiffResult = DiffResult.Resolved(emptySet(), DiffSource.REPO_HEAD),
    ) : DiffPort {
        override fun changedFiles(request: DiffRequest): DiffResult = result
    }

    @Test
    fun `un diff indisponible est SIGNALE, jamais confondu avec aucun changement`() {
        insert("wi_cible000001", status = "declared", triggersCi = false)
        val degraded = CheckoutUseCase(
            adapter,
            adapter,
            FakeDiff(DiffResult.Unavailable("dépôt sans remote (test)")),
            FakeIssues(),
            ciLimit = 2,
            clock = { at },
        )

        val result = assertIs<CheckoutResult.CheckedOut>(
            degraded.checkout(CheckoutCommand("wi_cible000001", rev(1))),
        )
        assertTrue(
            result.diffUnavailable?.contains("sans remote") == true,
            "une absence de vérification doit être visible dans la réponse",
        )
    }

    @Test
    fun `des fichiers explicites court-circuitent la detection`() {
        insert("wi_cible000001", status = "declared", triggersCi = false)

        val result = assertIs<CheckoutResult.CheckedOut>(
            checkout.checkout(
                CheckoutCommand("wi_cible000001", rev(1), diffFiles = listOf("src/explicite.kt")),
            ),
        )
        assertEquals(null, result.diffUnavailable, "un diff explicite ne peut pas être indisponible")
    }
}
