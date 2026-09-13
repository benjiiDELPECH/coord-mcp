package coordmcp

import coordmcp.domain.AcceptanceCriteria
import coordmcp.domain.PullRequestOverlap
import coordmcp.domain.PullRequestRef
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Règles de revue — PURES. L'adaptateur rapporte le TEXTE et les PR ; le domaine
 * les JUGE. Ces tests tournent sans `gh`, sans réseau, sans base.
 */
class ReviewRulesTest {

    // ── Critères d'acceptance ─────────────────────────────────────────────

    @Test
    fun `les cases cochees et non cochees sont comptees`() {
        val criteria = AcceptanceCriteria.parse(
            """
            ## Critères
            - [x] le endpoint répond
            - [X] le test passe
            - [ ] la doc est à jour
            """.trimIndent(),
        )
        assertEquals(3, criteria.total)
        assertEquals(2, criteria.checked)
        assertEquals(1, criteria.unchecked)
        assertFalse(criteria.allChecked)
    }

    @Test
    fun `une issue SANS critere n'a pas ses criteres satisfaits`() {
        // Le piège : `unchecked == 0` est vrai sur une issue vide. Confondre
        // « aucun critère » et « tous satisfaits » ferait passer une absence de
        // vérification pour une vérification réussie.
        val criteria = AcceptanceCriteria.parse("Juste du texte, aucun critère.")
        assertEquals(0, criteria.total)
        assertFalse(criteria.allChecked, "sans critère, rien n'est satisfait")
    }

    @Test
    fun `toutes cochees vaut satisfait`() {
        val criteria = AcceptanceCriteria.parse("- [x] a\n- [x] b")
        assertEquals(2, criteria.total)
        assertTrue(criteria.allChecked)
    }

    @Test
    fun `une case non cochee suffit a invalider`() {
        assertFalse(AcceptanceCriteria.parse("- [x] a\n- [ ] b").allChecked)
    }

    @Test
    fun `une case au milieu d'une ligne n'est pas comptee`() {
        // `- [x]` doit être en début de ligne : sinon du texte citant la syntaxe
        // fausserait le compte.
        assertFalse(AcceptanceCriteria.parse("voici un exemple : - [x] pas un critère").allChecked)
    }

    // ── PR concurrentes ───────────────────────────────────────────────────

    private fun pr(number: Int, vararg files: String) =
        PullRequestRef(number, "PR $number", "https://exemple/$number", files.toSet())

    @Test
    fun `une PR qui recouvre un fichier est signalee avec ce fichier`() {
        val collisions = PullRequestOverlap.detect(
            listOf(pr(42, "src/commun.kt", "src/autre.kt")),
            listOf("src/commun.kt"),
        )
        val collision = collisions.single()
        assertEquals(42, collision.number)
        assertEquals(listOf("src/commun.kt"), collision.overlappingFiles)
    }

    @Test
    fun `une PR sans recouvrement est ignoree`() {
        assertEquals(
            emptyList(),
            PullRequestOverlap.detect(listOf(pr(1, "src/a.kt")), listOf("src/b.kt")),
        )
    }

    @Test
    fun `l'ordre des collisions est stable par numero`() {
        val prs = listOf(pr(9, "a.kt"), pr(3, "a.kt"), pr(7, "a.kt"))
        assertEquals(listOf(3, 7, 9), PullRequestOverlap.detect(prs, listOf("a.kt")).map { it.number })
        assertEquals(
            listOf(3, 7, 9),
            PullRequestOverlap.detect(prs.reversed(), listOf("a.kt")).map { it.number },
        )
    }

    @Test
    fun `sans fichier touche, aucune PR n'est en collision`() {
        assertEquals(emptyList(), PullRequestOverlap.detect(listOf(pr(1, "a.kt")), emptyList()))
    }
}
