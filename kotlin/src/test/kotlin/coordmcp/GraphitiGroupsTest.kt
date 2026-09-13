package coordmcp

import coordmcp.domain.GraphitiGroups
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

/**
 * Mapping dépôt → groupe Graphiti — PUR.
 *
 * L'invariant qui compte : les identifiants utilisent des UNDERSCORES. FalkorDB
 * FTS lit le tiret comme une négation, donc un `group_id` construit
 * mécaniquement depuis un nom de dépôt (`alert-immo`) produirait une recherche
 * silencieusement vide — qu'on prendrait pour « aucune décision existante ».
 */
class GraphitiGroupsTest {

    @Test
    fun `un nom de depot avec tiret devient un groupe avec underscore`() {
        val group = GraphitiGroups.of("/Users/bdelpech/dev/github/alert-immo")
        assertEquals("alert_immo", group)
        assertEquals(false, group?.contains('-'), "aucun tiret : FalkorDB le lit comme une négation")
    }

    @Test
    fun `les deux depots connus sont mappes`() {
        assertEquals("alert_immo", GraphitiGroups.of("/Users/x/dev/github/alert-immo"))
        assertEquals("delpech_infra", GraphitiGroups.of("/Users/x/dev/github/delpech-infra"))
    }

    @Test
    fun `un depot inconnu rend null, et l'appelant SAUTE l'appel`() {
        // Deviner un groupe produirait soit une erreur, soit une réponse vide
        // prise pour « aucune décision existante ». On ne devine pas.
        assertNull(GraphitiGroups.of("/Users/x/dev/github/mon-projet-neuf"))
    }
}
