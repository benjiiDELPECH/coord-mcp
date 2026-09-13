package coordmcp

import coordmcp.domain.CandidateScope
import coordmcp.domain.CiGate
import coordmcp.domain.CiGateDecision
import coordmcp.domain.CiTrigger
import coordmcp.domain.ConflictDetector
import coordmcp.domain.ConflictReason
import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemDraft
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

/** Règles de coordination — PURES, donc testables sans base ni port. */
class CoordinationTest {

    private val at: Instant = Instant.parse("2026-09-13T12:00:00Z")

    private fun item(
        id: String,
        paths: List<String> = emptyList(),
        symbols: List<String> = emptyList(),
        expanded: List<String> = emptyList(),
    ): WorkItem = WorkItem.of(
        id = (WorkItemId.of(id) as DomainResult.Ok).value,
        status = WorkStatus.DECLARED,
        outcome = null,
        draft = WorkItemDraft(
            (Repo.of("/tmp/repo") as DomainResult.Ok).value,
            "titre",
            (ScopeFiles.parse(paths.joinToString(",", "[", "]") { "\"$it\"" }) as DomainResult.Ok).value,
            createdAt = at,
            updatedAt = at,
            symbols = symbols,
            expandedFiles = expanded,
        ),
        revision = (Revision.of(1) as DomainResult.Ok).value,
    )

    private fun candidate(paths: List<String> = emptyList(), symbols: List<String> = emptyList()) =
        CandidateScope(paths.toSet(), symbols.toSet())

    // ── Conflits de chemins ───────────────────────────────────────────────

    @Test
    fun `aucun recouvrement ne produit aucun conflit`() {
        val conflicts = ConflictDetector.detect(
            candidate(paths = listOf("src/a.kt")),
            listOf(item("wi_b", paths = listOf("src/b.kt"))),
            ConflictReason.SHARED_PATH,
        )
        assertEquals(emptyList(), conflicts)
    }

    @Test
    fun `un chemin partage nomme le travail ET les fichiers en cause`() {
        val conflicts = ConflictDetector.detect(
            candidate(paths = listOf("src/commun.kt", "src/autre.kt")),
            listOf(item("wi_b", paths = listOf("src/commun.kt"))),
            ConflictReason.SHARED_PATH,
        )
        val conflict = conflicts.single()
        assertEquals("wi_b", conflict.workItemId.value)
        assertEquals(listOf("src/commun.kt"), conflict.shared, "le fichier partagé doit être nommé")
    }

    @Test
    fun `un perimetre candidat vide ne peut entrer en conflit`() {
        val conflicts = ConflictDetector.detect(
            candidate(),
            listOf(item("wi_b", paths = listOf("src/a.kt"))),
            ConflictReason.SHARED_PATH,
        )
        assertEquals(emptyList(), conflicts, "sans périmètre, aucun conflit n'est démontrable")
    }

    @Test
    fun `les fichiers ETENDUS d'un item actif comptent dans le recouvrement`() {
        // L'item actif a déclaré un symbole dont le résolveur a déduit un fichier :
        // ce fichier est réellement touché, même s'il n'est pas déclaré.
        val conflicts = ConflictDetector.detect(
            candidate(paths = listOf("src/deduit.kt")),
            listOf(item("wi_b", paths = listOf("src/declare.kt"), expanded = listOf("src/deduit.kt"))),
            ConflictReason.SHARED_PATH,
        )
        assertEquals(1, conflicts.size, "le blast-radius résolu doit compter, pas seulement le déclaré")
    }

    @Test
    fun `l'ordre des conflits est stable`() {
        val actifs = listOf(
            item("wi_z", paths = listOf("a.kt")),
            item("wi_a", paths = listOf("a.kt")),
            item("wi_m", paths = listOf("a.kt")),
        )
        val scope = candidate(paths = listOf("a.kt"))
        val premier = ConflictDetector.detect(scope, actifs, ConflictReason.SHARED_PATH)
            .map { it.workItemId.value }
        val second = ConflictDetector.detect(scope, actifs.reversed(), ConflictReason.SHARED_PATH)
            .map { it.workItemId.value }
        assertEquals(premier, second, "un diagnostic qui change d'ordre ne se compare pas")
        assertEquals(listOf("wi_a", "wi_m", "wi_z"), premier)
    }

    // ── Conflits de SYMBOLES : invisibles aux seuls chemins ───────────────

    @Test
    fun `un symbole partage est detecte MEME SI les fichiers diffèrent`() {
        val conflicts = ConflictDetector.detect(
            candidate(symbols = listOf("TaxSimulationService.compute")),
            listOf(
                item(
                    "wi_b",
                    paths = listOf("src/tout-autre-fichier.kt"),
                    symbols = listOf("TaxSimulationService.compute"),
                ),
            ),
            ConflictReason.SHARED_SYMBOL,
        )
        assertEquals(1, conflicts.size, "c'est LE conflit qu'un recoupement de chemins ne voit pas")
        assertEquals(ConflictReason.SHARED_SYMBOL, conflicts.single().reason)
    }

    @Test
    fun `detectAll rend les deux natures de conflit`() {
        val conflicts = ConflictDetector.detectAll(
            candidate(paths = listOf("src/commun.kt"), symbols = listOf("A.b")),
            listOf(item("wi_b", paths = listOf("src/commun.kt"), symbols = listOf("A.b"))),
        )
        assertEquals(2, conflicts.size)
        assertTrue(conflicts.any { it.reason == ConflictReason.SHARED_PATH })
        assertTrue(conflicts.any { it.reason == ConflictReason.SHARED_SYMBOL })
    }

    // ── Barrière CI ───────────────────────────────────────────────────────

    @Test
    fun `sous le seuil la barriere laisse passer`() {
        assertIs<CiGateDecision.Proceed>(CiGate.evaluate(activeCi = 1, limit = 2))
    }

    @Test
    fun `au seuil la barriere demande d'attendre`() {
        assertIs<CiGateDecision.Wait>(CiGate.evaluate(activeCi = 2, limit = 2))
    }

    @Test
    fun `un seuil invalide retombe sur le defaut documente, jamais sur zero`() {
        val decision = CiGate.evaluate(activeCi = 1, limit = 0)
        assertEquals(CiGate.DEFAULT_LIMIT, decision.limit)
        assertIs<CiGateDecision.Proceed>(decision)
    }

    // ── Auto-détection du déclenchement CI ────────────────────────────────

    @Test
    fun `un workflow ou du code compile declenche la CI`() {
        assertTrue(CiTrigger.detect(listOf(".github/workflows/ci.yml")))
        assertTrue(CiTrigger.detect(listOf("src/Main.kt")))
        assertTrue(CiTrigger.detect(listOf("migrations/V1__init.sql")))
    }

    @Test
    fun `de la documentation ne declenche pas de CI`() {
        assertFalse(CiTrigger.detect(listOf("docs/README.md", "docs/adr/ADR-001-x.md")))
    }

    @Test
    fun `sous-estimer le declenchement est impossible par oubli`() {
        // L'auto-détection existe parce qu'un agent qui oublie de déclarer une CI
        // lourde fait tomber le lanceur partagé : incident OOM du 26.08.2026.
        assertTrue(
            CiTrigger.detect(listOf("README.md", "src/quelque/chose.java")),
            "UN seul fichier compilé suffit, même au milieu de documentation",
        )
    }
}
