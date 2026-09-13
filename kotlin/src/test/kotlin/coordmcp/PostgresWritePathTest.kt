package coordmcp

import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.adapter.outbound.postgres.JooqWorkItemAdapter
import coordmcp.domain.DomainResult
import coordmcp.domain.Revision
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import coordmcp.domain.WriteOutcome
import java.sql.DriverManager
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

/**
 * Le compare-and-swap, sur POSTGRESQL.
 *
 * La version SQLite le testait déjà ; ce n'était pas transposable, et c'est
 * précisément la pièce dont l'échec silencieux coûte un outcome. Une garantie de
 * sûreté non testée sur le moteur réellement déployé n'est pas une garantie.
 *
 * Base DÉDIÉE `coord_mcp_test`, jamais la production : elle est vidée avant
 * chaque test. La base est créée hors des tests :
 *
 *   createdb coord_mcp_test
 *   psql -d coord_mcp_test -f migrations/pg/001_init.sql
 */
class PostgresWritePathTest {

    private val url = System.getenv("COORD_MCP_PG_TEST_URL")
        ?: "jdbc:postgresql://127.0.0.1:5432/coord_mcp_test"
    private val user = System.getenv("COORD_MCP_DB_USER") ?: System.getProperty("user.name")

    private lateinit var database: JooqDatabase
    private lateinit var adapter: JooqWorkItemAdapter
    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    @BeforeTest
    fun setUp() {
        // Table vide = test reproductible. L'ordre respecte la clé étrangère.
        execute("DELETE FROM consumer_inbox")
        execute("DELETE FROM outbox_events")
        execute("DELETE FROM work_items")
        database = JooqDatabase(url, user)
        adapter = JooqWorkItemAdapter(database)
        insert("wi_test0001", "checked_out")
    }

    @AfterTest
    fun tearDown() {
        database.close()
    }

    @Test
    fun `revision correcte applique et incremente`() {
        val outcome = adapter.applyTransition(
            id("wi_test0001"), WorkStatus.RELEASED, "livré", at, rev(1),
        )

        assertEquals(2L, assertIs<WriteOutcome.Applied>(outcome).revision.value)
        assertEquals("released", stored("status"))
        assertEquals("livré", stored("outcome"))
    }

    @Test
    fun `revision perimee rejette sans rien appliquer`() {
        adapter.applyTransition(id("wi_test0001"), WorkStatus.RELEASED, "premier", at, rev(1))

        val conflict = adapter.applyTransition(
            id("wi_test0001"), WorkStatus.RELEASED, "second", at, rev(1),
        )

        val stale = assertIs<WriteOutcome.StaleRevision>(conflict)
        assertEquals(1L, stale.expected.value)
        assertEquals(2L, stale.current.value)
        assertEquals("premier", stored("outcome"), "l'outcome du premier n'a pas été écrasé")
        assertEquals("2", stored("revision"), "un rejet n'incrémente pas la révision")
    }

    @Test
    fun `deux transitions concurrentes, un seul gagnant`() {
        val lue = rev(stored("revision")!!.toLong())

        val a = adapter.applyTransition(id("wi_test0001"), WorkStatus.RELEASED, "A", at, lue)
        val b = adapter.applyTransition(id("wi_test0001"), WorkStatus.RELEASED, "B", at, lue)

        assertEquals(1, listOf(a, b).count { it is WriteOutcome.Applied })
        assertEquals(1, listOf(a, b).count { it is WriteOutcome.StaleRevision })
        assertEquals("A", stored("outcome"))
    }

    @Test
    fun `item inconnu donne NotFound`() {
        assertIs<WriteOutcome.NotFound>(
            adapter.applyTransition(id("wi_absent"), WorkStatus.RELEASED, "x", at, rev(1)),
        )
    }

    @Test
    fun `la contrainte CHECK refuse un released sans outcome, meme en SQL direct`() {
        // L'invariant est porté par la BASE : ce test vérifie qu'un écrivain
        // hors application — script d'administration, psql — ne peut pas le
        // contourner.
        val directWrite = "UPDATE work_items SET status='released', outcome=NULL WHERE id='wi_test0001'"
        val failure = runCatching { execute(directWrite) }.exceptionOrNull()

        assertEquals(
            true,
            failure?.message?.contains("terminal_implique_outcome") == true,
            "attendu un refus nommant la contrainte, obtenu : ${failure?.message}",
        )
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private fun id(v: String): WorkItemId = (WorkItemId.of(v) as DomainResult.Ok).value

    private fun rev(v: Long): Revision = (Revision.of(v) as DomainResult.Ok).value

    /**
     * Fixture ANTÉRIEURE à `at` : la contrainte `updated_apres_created` refuse
     * qu'une transition écrive un `updated_at` antérieur au `created_at`. C'est
     * un contrat réel — un appelant qui passerait un horodatage périmé se ferait
     * refuser par la base, pas par une convention.
     */
    private fun insert(id: String, status: String) {
        execute(
            "INSERT INTO work_items " +
                "(id, repo, title, status, created_at, updated_at, revision) VALUES " +
                "('$id', '/tmp/x', 'un titre', '$status', " +
                "'2026-09-13T10:00:00Z', '2026-09-13T10:00:00Z', 1)",
        )
    }

    private fun stored(column: String): String? = query { s ->
        s.executeQuery("SELECT $column FROM work_items WHERE id='wi_test0001'").use { rs ->
            if (rs.next()) rs.getString(column) else null
        }
    }

    /** Un seul point d'accès JDBC : les `use` imbriqués finissaient par masquer la logique du test. */
    private fun <T> query(block: (java.sql.Statement) -> T): T =
        DriverManager.getConnection(url, user, "").use { c -> c.createStatement().use(block) }

    private fun execute(sql: String) {
        query { s -> s.execute(sql) }
    }
}
