package coordmcp

import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.adapter.outbound.postgres.JooqWorkItemAdapter
import coordmcp.application.CheckinCommand
import coordmcp.application.CheckinResult
import coordmcp.application.ScopeResolution
import coordmcp.application.CheckinKnowledge
import coordmcp.application.CheckinUseCase
import coordmcp.application.port.ScopeExpansion
import coordmcp.application.port.IssueOutcome
import coordmcp.application.port.IssueSearchOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewIssueRequest
import coordmcp.application.port.PriorDecisionsOutcome
import coordmcp.application.port.PriorDecisionsPort
import coordmcp.application.port.PullRequestOutcome
import coordmcp.application.port.ScopeResolverPort
import coordmcp.domain.CiGateDecision
import java.sql.DriverManager
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * `checkin` en intégration : conflits détectés sur des données réelles, et
 * barrière CI qui MORD.
 *
 * La barrière est le mécanisme qui a manqué le 26.08.2026 : elle existait comme
 * règle, rien ne l'imposait. Ici on vérifie qu'elle répond WAIT quand le seuil
 * est atteint — pas qu'elle existe.
 */
class PostgresCheckinTest {

    private val url = System.getenv("COORD_MCP_PG_TEST_URL")
        ?: "jdbc:postgresql://127.0.0.1:5432/coord_mcp_test"
    private val user = System.getenv("COORD_MCP_DB_USER") ?: System.getProperty("user.name")
    private val repo = "/tmp/coord-checkin-test"

    private lateinit var database: JooqDatabase
    private lateinit var adapter: JooqWorkItemAdapter
    private lateinit var checkin: CheckinUseCase

    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    @BeforeTest
    fun setUp() {
        execute("DELETE FROM work_items")
        database = JooqDatabase(url, user)
        adapter = JooqWorkItemAdapter(database)
        checkin = CheckinUseCase(
            adapter,
            adapter,
            CheckinKnowledge(FakeScopeResolver(), FakeIssues(), FakePriorDecisions()),
            ciLimit = 2,
            clock = { at },
        )
    }

    @AfterTest
    fun tearDown() {
        execute("DELETE FROM work_items")
        database.close()
    }

    @Test
    fun `declarer un travail le cree en etat declared`() {
        val created = assertIs<CheckinResult.Created>(
            checkin.checkin(command("premier travail", listOf("src/a.kt"))),
        )

        val stored = adapter.findById(created.workItemId)
        assertEquals("premier travail", stored?.title)
        assertEquals("declared", stored?.status?.storageValue)
        assertTrue(created.suggestedAction.startsWith("CREATE_NEW"), created.suggestedAction)
    }

    @Test
    fun `un perimetre recouvert est SIGNALE, et le travail est quand meme cree`() {
        checkin.checkin(command("premier", listOf("src/commun.kt")))

        val second = assertIs<CheckinResult.Created>(
            checkin.checkin(command("second", listOf("src/commun.kt"))),
        )

        assertTrue(second.suggestedAction.startsWith("REVIEW_CONFLICTS"), second.suggestedAction)
        assertEquals(1, second.conflicts.size)
        assertEquals(listOf("src/commun.kt"), second.conflicts.single().shared)
        // Signalé, pas tranché : le travail existe, c'est à l'agent de décider.
        assertTrue(adapter.findById(second.workItemId) != null)
    }

    @Test
    fun `au seuil CI la barriere repond WAIT`() {
        // Deux travaux DÉJÀ checked_out et déclencheurs de CI : le seuil est 2.
        insertCheckedOut("wi_ci000000001")
        insertCheckedOut("wi_ci000000002")

        val created = assertIs<CheckinResult.Created>(
            checkin.checkin(command("troisieme", listOf("src/c.kt"), triggersCi = true)),
        )

        val gate = assertIs<CiGateDecision.Wait>(created.ciGate)
        assertEquals(2, gate.activeCi)
        assertEquals(2, gate.limit)
    }

    @Test
    fun `un travail sans CI ne consomme pas de place dans la barriere`() {
        insertCheckedOut("wi_ci000000001")
        insertCheckedOut("wi_ci000000002")

        val created = assertIs<CheckinResult.Created>(
            checkin.checkin(command("doc seule", listOf("docs/x.md"), triggersCi = false)),
        )

        assertIs<CiGateDecision.Proceed>(created.ciGate)
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private fun command(
        title: String,
        paths: List<String>,
        triggersCi: Boolean? = null,
        symbols: List<String> = emptyList(),
    ) = CheckinCommand(
        repoPath = repo,
        title = title,
        scopeFiles = paths,
        scopeSymbols = symbols,
        agentId = "agent_de_test",
        triggersCi = triggersCi,
    )

    /**
     * Mémoire factice : `Unavailable` par défaut.
     *
     * C'est le cas le plus important à couvrir — une mémoire injoignable doit
     * produire `prior_decisions_checked: false`, pas un zéro rassurant.
     */
    private class FakePriorDecisions(
        private val outcome: PriorDecisionsOutcome = PriorDecisionsOutcome.Unavailable("test"),
    ) : PriorDecisionsPort {
        override fun groupFor(repoPath: String): String? = null
        override fun search(repoPath: String, topic: String): PriorDecisionsOutcome = outcome
    }

    /** Suivi d'issues factice : aucune issue ressemblante, aucune PR. */
    private class FakeIssues : IssueTrackerPort {
        override fun assignToMe(repoPath: String, issueNumber: Int) = IssueOutcome.Ok(issueNumber)
        override fun createIssue(request: NewIssueRequest) = IssueOutcome.Ok(1)
        override fun issueBody(repoPath: String, issueNumber: Int) =
            coordmcp.application.port.BodyOutcome.Fetched("")
        override fun openPullRequests(repoPath: String) = PullRequestOutcome.Fetched(emptyList())
        override fun searchOpenIssues(repoPath: String, query: String, limit: Int) =
            IssueSearchOutcome.Found(emptyList())
    }

    /** Résolveur factice : étend les symboles en `fichiers-deduits/<symbole>.kt`. */
    private class FakeScopeResolver(
        private val available: Boolean = true,
    ) : ScopeResolverPort {
        override fun expand(repoPath: String, symbols: List<String>, depth: Int): ScopeExpansion =
            if (!available) {
                ScopeExpansion.Unavailable("gitnexus non exécutable (test)")
            } else {
                ScopeExpansion.Resolved(symbols.map { "fichiers-deduits/$it.kt" }.toSet())
            }
    }

    @Test
    fun `le blast-radius resolu revele un conflit invisible aux chemins`() {
        // Le premier travail declare un SYMBOLE ; le second, un CHEMIN différent
        // que le symbole du premier recouvre réellement.
        checkin.checkin(command("premier", listOf("src/loin.kt"), symbols = listOf("A.b")))

        val second = assertIs<CheckinResult.Created>(
            checkin.checkin(command("second", listOf("fichiers-deduits/A.b.kt"))),
        )

        assertTrue(second.suggestedAction.startsWith("REVIEW_CONFLICTS"), second.suggestedAction)
        assertEquals(1, second.conflicts.size)
    }

    @Test
    fun `un resolveur indisponible est SIGNALE, jamais confondu avec aucun conflit`() {
        val degraded = CheckinUseCase(
            adapter,
            adapter,
            CheckinKnowledge(FakeScopeResolver(available = false), FakeIssues(), FakePriorDecisions()),
            ciLimit = 2,
            clock = { at },
        )

        val created = assertIs<CheckinResult.Created>(
            degraded.checkin(command("avec symbole", listOf("src/a.kt"), symbols = listOf("A.b"))),
        )

        val resolution = assertIs<ScopeResolution.Unavailable>(created.scopeResolution)
        assertTrue(resolution.reason.contains("non exécutable"))
    }

    private fun insertCheckedOut(id: String) {
        execute(
            "INSERT INTO work_items (id, repo, title, status, triggers_ci, created_at, updated_at, revision) " +
                "VALUES ('$id', '$repo', 'occupant', 'checked_out', true, now(), now(), 1)",
        )
    }

    private fun execute(sql: String) {
        DriverManager.getConnection(url, user, "").use { c ->
            c.createStatement().use { s -> s.execute(sql) }
        }
    }
}
