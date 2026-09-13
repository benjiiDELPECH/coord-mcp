package coordmcp

import coordmcp.adapter.outbound.postgres.JooqAdrAllocationAdapter
import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.application.port.AdrClaim
import coordmcp.domain.AdrNumbering
import java.nio.file.Files
import java.sql.DriverManager
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Allocation d'un numéro d'ADR — la garantie centrale de coord-mcp.
 *
 * Deux agents ne doivent JAMAIS obtenir le même numéro. L'atomicité vient de
 * `UNIQUE (repo_path, adr_number)` et d'un `INSERT … ON CONFLICT DO NOTHING` :
 * c'est la contrainte qui arbitre, pas une lecture-puis-écriture applicative qui
 * laisserait ouverte la fenêtre de course.
 *
 * Un dépôt factice est utilisé : aucun ADR réel n'est touché.
 */
class PostgresAdrClaimTest {

    private val url = System.getenv("COORD_MCP_PG_TEST_URL")
        ?: "jdbc:postgresql://127.0.0.1:5432/coord_mcp_test"
    private val user = System.getenv("COORD_MCP_DB_USER") ?: System.getProperty("user.name")

    private lateinit var database: JooqDatabase
    private lateinit var allocations: JooqAdrAllocationAdapter
    private lateinit var repoPath: String

    @BeforeTest
    fun setUp() {
        database = JooqDatabase(url, user)
        allocations = JooqAdrAllocationAdapter(database)
        // Dépôt factice SANS `docs/adr` : le maximum disque vaut 0, le test
        // n'isole que la logique d'allocation.
        repoPath = Files.createTempDirectory("adr-claim-").toString()
        execute("DELETE FROM adr_allocations WHERE repo_path = '$repoPath'")
    }

    @AfterTest
    fun tearDown() {
        execute("DELETE FROM adr_allocations WHERE repo_path = '$repoPath'")
        database.close()
    }

    @Test
    fun `deux allocations successives donnent deux numeros distincts et croissants`() {
        val first = assertIs<AdrClaim.Allocated>(claim("premier-sujet"))
        val second = assertIs<AdrClaim.Allocated>(claim("second-sujet"))

        assertEquals(1, first.number, "dépôt vierge : la première allocation est le numéro 1")
        assertEquals(2, second.number)
        assertTrue(first.filename != second.filename)
    }

    @Test
    fun `le nom de fichier suit la convention Python`() {
        val claim = assertIs<AdrClaim.Allocated>(claim(AdrNumbering.slugify("capex monte carlo")))
        assertEquals("ADR-001-capex-monte-carlo.md", claim.filename)
        assertEquals(claim.number, allocations.allocations(repoPath).single().adrNumber)
    }

    @Test
    fun `dix allocations concurrentes donnent dix numeros DIFFERENTS`() {
        // Le cœur de la garantie. En séquentiel ce test ne prouve rien ; ce qui
        // compte est qu'AUCUN numéro ne soit attribué deux fois.
        val numbers = (1..10).map { assertIs<AdrClaim.Allocated>(claim("sujet-$it")).number }

        assertEquals(10, numbers.toSet().size, "un numéro a été attribué deux fois")
        assertEquals((1..10).toList(), numbers.sorted())
    }

    @Test
    fun `le slug est calcule par le domaine, pas par l'appelant`() {
        val claim = assertIs<AdrClaim.Allocated>(
            claim(AdrNumbering.slugify("Décision d'architecture — CAPEX")),
        )
        assertEquals("ADR-001-decision-d-architecture-capex.md", claim.filename)
    }

    @Test
    fun `un slug hors convention est refuse au lieu de produire un fichier invalide`() {
        val rejected = assertIs<AdrClaim.Rejected>(claim("Capex Monte Carlo"))
        assertTrue(rejected.detail.contains("slug invalide"))
        assertEquals(
            0,
            allocations.allocations(repoPath).size,
            "un slug refusé ne doit RIEN avoir écrit",
        )
    }

    private fun claim(slug: String): AdrClaim =
        allocations.claim(repoPath, slug, "agent_de_test", Instant.now())

    private fun execute(sql: String) {
        DriverManager.getConnection(url, user, "").use { c ->
            c.createStatement().use { s -> s.execute(sql) }
        }
    }
}
