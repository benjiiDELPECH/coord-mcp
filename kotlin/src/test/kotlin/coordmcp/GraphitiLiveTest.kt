package coordmcp

import coordmcp.adapter.outbound.graphiti.GraphitiPriorDecisionsAdapter
import coordmcp.application.port.PriorDecisionsOutcome
import kotlin.test.Test
import kotlin.test.assertIs
import org.junit.jupiter.api.Assumptions.assumeTrue

/**
 * L'adaptateur Graphiti, contre le VRAI serveur.
 *
 * Il existe à cause d'un bug réel : le serveur répond `307` sur `/mcp/` et ktor
 * ne suit pas les redirections par défaut. Chaque recherche échouait, et comme
 * un échec se traduit par `prior_decisions_checked: false`, la panne était
 * invisible — un client curl (`-L`) réussissait pendant que le service échouait.
 */
class GraphitiLiveTest {

    private val adapter = GraphitiPriorDecisionsAdapter()

    private fun graphitiIsUp(): Boolean = try {
        java.net.Socket("localhost", 8001).use { true }
    } catch (_: java.io.IOException) {
        false
    }

    @Test
    fun `la recherche atteint reellement Graphiti pour un depot mappe`() {
        assumeTrue(graphitiIsUp(), "Graphiti absent sur 8001 — test ignoré")

        val outcome = adapter.search("/Users/bdelpech/dev/github/alert-immo", "incident secrets historique git")

        assertIs<PriorDecisionsOutcome.Found>(
            outcome,
            "l'appel doit ATTEINDRE la mémoire, pas être classé indisponible : $outcome",
        )
    }

    @Test
    fun `un depot sans groupe est saute, pas devine`() {
        val outcome = adapter.search("/tmp/depot-inconnu", "peu importe")
        assertIs<PriorDecisionsOutcome.Unavailable>(outcome)
    }
}
