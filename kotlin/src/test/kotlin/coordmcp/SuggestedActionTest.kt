package coordmcp

import coordmcp.domain.SuggestedAction
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * La recommandation de `checkin` — règle PURE.
 *
 * Les libellés sont comparés AU CARACTÈRE PRÈS à ceux du service Python : des
 * agents peuvent les lire, et un remplacement à l'identique se joue là.
 */
class SuggestedActionTest {

    @Test
    fun `un conflit prime sur tout le reste`() {
        val action = SuggestedAction.of(conflicts = 2, similarOpenIssues = 3, priorDecisions = 5)
        assertEquals(
            "REVIEW_CONFLICTS — overlapping scope detected, decide whether to abort or coordinate",
            action,
        )
    }

    @Test
    fun `une decision Graphiti prime sur une issue qui ressemble`() {
        val action = SuggestedAction.of(conflicts = 0, similarOpenIssues = 3, priorDecisions = 2)
        assertTrue(
            action.startsWith("REVIEW_PRIOR_DECISIONS — 2 Graphiti node(s)"),
            "on veut lire ce qui existe AVANT d'envisager de créer : $action",
        )
    }

    @Test
    fun `des issues ressemblantes invitent a regarder avant de creer`() {
        val action = SuggestedAction.of(conflicts = 0, similarOpenIssues = 3, priorDecisions = null)
        assertEquals("CONSIDER_CLAIMING — 3 similar open issue(s) might already cover this", action)
    }

    @Test
    fun `rien a signaler vaut creation`() {
        assertEquals(
            "CREATE_NEW — no conflicts, no similar work, safe to create new issue",
            SuggestedAction.of(conflicts = 0, similarOpenIssues = 0, priorDecisions = null),
        )
    }

    @Test
    fun `Graphiti non consulte ne fait PAS taire la branche, il l'empeche seulement`() {
        // `null` = non vérifié. Rendre 0 ferait croire qu'on a regardé et rien
        // trouvé : la recommandation serait fausse, pas seulement incomplète.
        val nonVerifie = SuggestedAction.of(conflicts = 0, similarOpenIssues = 0, priorDecisions = null)
        val verifieVide = SuggestedAction.of(conflicts = 0, similarOpenIssues = 0, priorDecisions = 0)
        assertEquals(nonVerifie, verifieVide, "les deux mènent à CREATE_NEW — mais le JSON les distingue")
    }
}
