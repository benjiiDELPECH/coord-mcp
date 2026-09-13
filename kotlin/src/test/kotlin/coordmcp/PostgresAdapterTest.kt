package coordmcp

import coordmcp.adapter.outbound.postgres.JooqAdrAllocationAdapter
import coordmcp.adapter.outbound.postgres.JooqAuditAdapter
import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.adapter.outbound.postgres.JooqWorkItemAdapter
import coordmcp.domain.WorkStatus
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/**
 * L'adaptateur PostgreSQL, contre la VRAIE base `coord_mcp`.
 *
 * Ce test est la preuve que l'architecture est réellement hexagonale : le même
 * port `WorkItemReader` est servi par deux adaptateurs, et celui-ci lit les 2090
 * items migrés. Si un jour l'adaptateur SQLite disparaît, ce test reste.
 *
 * LECTURE SEULE : aucun test ne doit muter le registre de production. Le chemin
 * d'écriture PostgreSQL (compare-and-swap) demande une base de test dédiée —
 * c'est une dette assumée, pas un oubli.
 */
class PostgresAdapterTest {

    private fun database(): JooqDatabase {
        val url = System.getenv("COORD_MCP_PG_URL")
            ?: "jdbc:postgresql://127.0.0.1:5432/coord_mcp"
        val user = System.getenv("COORD_MCP_DB_USER") ?: System.getProperty("user.name")
        return JooqDatabase(url, user)
    }

    @Test
    fun `lit les items migres depuis PostgreSQL`() {
        database().use { db ->
            val pg = JooqWorkItemAdapter(db)
            assertTrue(pg.count() >= 2000, "attendu les 2090 items migrés, obtenu ${pg.count()}")

            val active = pg.active()
            assertTrue(active.isNotEmpty(), "le registre doit contenir des items non terminaux")
            active.forEach {
                assertTrue(
                    it.status in ACTIVE,
                    "active() ne doit renvoyer que des statuts non terminaux, vu ${it.status}",
                )
            }
        }
    }

    @Test
    fun `un item relu est identique a lui-meme et porte sa revision`() {
        database().use { db ->
            val pg = JooqWorkItemAdapter(db)
            val known = pg.active().first()
            val reread = pg.findById(known.id)

            assertNotNull(reread)
            assertEquals(known.id, reread.id)
            assertEquals(known.status, reread.status)
            assertEquals(known.revision, reread.revision)
            assertTrue(reread.revision.value >= 1, "la contrainte CHECK garantit revision >= 1")
        }
    }

    @Test
    fun `la piste d'audit est lue du plus recent au plus ancien et bornee`() {
        database().use { db ->
            val pg = JooqWorkItemAdapter(db)
            val tail = JooqAuditAdapter(db).tail(10)
            assertTrue(tail.isNotEmpty(), "la piste d'audit migrée doit contenir des entrées")

            val timestamps = tail.map { it.timestamp }
            assertEquals(
                timestamps.sortedDescending(),
                timestamps,
                "l'audit se lit du plus récent au plus ancien",
            )

            // La borne est un invariant d'interface : un agent ne doit pas
            // pouvoir tirer 5662 lignes dans le contexte d'un modèle.
            assertTrue(
                JooqAuditAdapter(db).tail(100_000).size <= 500,
                "un limit arbitraire doit être clampé, pas obéi",
            )
        }
    }

    @Test
    fun `les allocations d'ADR sont lues et filtrables par depot`() {
        database().use { db ->
            val pg = JooqWorkItemAdapter(db)
            val toutes = JooqAdrAllocationAdapter(db).allocations()
            assertTrue(toutes.size >= 100, "attendu les 147 allocations migrées, obtenu ${toutes.size}")

            // Aucun numéro d'ADR ne peut être alloué deux fois DANS UN MÊME dépôt :
            // c'est la contrainte UNIQUE(repo_path, adr_number) qui le garantit.
            val doublons = toutes
                .groupBy { it.repoPath to it.adrNumber }
                .filterValues { it.size > 1 }
            assertEquals(emptyList(), doublons.keys.toList(), "deux allocations identiques en base")

            val unDepot = toutes.first().repoPath
            val filtrees = JooqAdrAllocationAdapter(db).allocations(unDepot)
            assertTrue(filtrees.isNotEmpty())
            assertTrue(filtrees.all { it.repoPath == unDepot }, "le filtre par dépôt doit être strict")
        }
    }

    /**
     * Contrat d'ORDRE, verrouillé par un test.
     *
     * Le double-run du 2026-09-13 a montré que le service Python rend les actifs
     * du PLUS RÉCENT au plus ancien, alors que l'adaptateur Kotlin les rendait
     * dans l'ordre inverse. Le contenu était identique — un comptage ne l'aurait
     * jamais vu. Un agent qui lit « le premier » prendrait le mauvais item.
     */
    @Test
    fun `les actifs sont ordonnes du plus recent au plus ancien, comme le service Python`() {
        database().use { db ->
            val items = JooqWorkItemAdapter(db).active()
            val dates = items.map { it.createdAt }
            assertEquals(
                dates.sortedDescending(),
                dates,
                "l'ordre doit correspondre au service Python, sinon les agents divergent",
            )
        }
    }

    private companion object {
        val ACTIVE = setOf(
            WorkStatus.DECLARED,
            WorkStatus.CLAIMED,
            WorkStatus.CHECKED_OUT,
        )
    }
}
