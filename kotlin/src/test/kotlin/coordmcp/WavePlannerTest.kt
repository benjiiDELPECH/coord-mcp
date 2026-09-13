package coordmcp

import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WavePlanner
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemDraft
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Planification de vagues — fonction PURE, donc testable sans base ni port.
 *
 * C'est le bénéfice concret de l'hexagonal : la règle la plus délicate du domaine
 * (qui peut tourner en parallèle avec qui) se teste en quelques millisecondes,
 * sans PostgreSQL, sans mock, sans fixture.
 */
class WavePlannerTest {

    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    private fun item(id: String, vararg paths: String): WorkItem = WorkItem.of(
        id = (WorkItemId.of(id) as DomainResult.Ok).value,
        status = WorkStatus.DECLARED,
        outcome = null,
        draft = WorkItemDraft(
            (Repo.of("/tmp/repo") as DomainResult.Ok).value,
            "titre",
            (ScopeFiles.parse(paths.joinToString(",", "[", "]") { "\"$it\"" }) as DomainResult.Ok).value,
            at,
            at,
        ),
        revision = (Revision.of(1) as DomainResult.Ok).value,
    )

    @Test
    fun `des perimetres disjoints tiennent dans une seule vague`() {
        val waves = WavePlanner.plan(
            listOf(item("wi_a", "src/a.kt"), item("wi_b", "src/b.kt")),
        )
        assertEquals(1, waves.size, "aucun conflit ne doit pas créer de vague inutile")
    }

    @Test
    fun `un fichier partage force deux vagues`() {
        val waves = WavePlanner.plan(
            listOf(item("wi_a", "src/commun.kt"), item("wi_b", "src/commun.kt")),
        )
        assertEquals(2, waves.size)
        assertEquals(1, waves[0].size)
        assertEquals(1, waves[1].size)
    }

    @Test
    fun `un item sans conflit rejoint la vague existante plutot que d'en creer une`() {
        val waves = WavePlanner.plan(
            listOf(
                item("wi_a", "src/commun.kt"),
                item("wi_b", "src/commun.kt"),
                item("wi_c", "src/autre.kt"),
            ),
        )
        assertEquals(2, waves.size, "wi_c n'a de conflit avec personne")
        val waveDeC = waves.first { wave -> wave.any { it.value == "wi_c" } }
        assertEquals(2, waveDeC.size, "wi_c doit REJOINDRE une vague, pas en créer une troisième")
    }

    @Test
    fun `le plan est deterministe quel que soit l'ordre d'entree`() {
        val items = listOf(
            item("wi_c", "src/c.kt"),
            item("wi_a", "src/a.kt"),
            item("wi_b", "src/a.kt"),
        )
        val premier = WavePlanner.plan(items)
        val second = WavePlanner.plan(items.reversed())

        assertEquals(
            premier.map { wave -> wave.map { it.value } },
            second.map { wave -> wave.map { it.value } },
            "un ordonnancement non reproductible ne se diagnostique pas",
        )
    }

    @Test
    fun `un perimetre vide ne peut entrer en conflit avec rien`() {
        val waves = WavePlanner.plan(
            listOf(item("wi_sans_scope"), item("wi_avec", "src/a.kt")),
        )
        assertEquals(1, waves.size, "sans périmètre, aucun conflit n'est démontrable")
        assertTrue(waves.single().size == 2)
    }
}
