package coordmcp.domain

/**
 * Détection de conflits et barrière de concurrence CI — PUR, testable sans base.
 *
 * Ces deux règles sont le cœur de `checkin` : « ce travail en recouvre-t-il un
 * autre ? » et « le lanceur de CI partagé est-il déjà saturé ? ». Les isoler du
 * stockage permet de les tester en millisecondes, et surtout de les faire
 * évoluer sans risquer de casser la persistance.
 */

public enum class ConflictReason { SHARED_PATH, SHARED_SYMBOL }

public data class Conflict(
    val workItemId: WorkItemId,
    val reason: ConflictReason,
    val shared: List<String>,
)

/** Périmètre candidat : chemins déclarés, symboles déclarés, fichiers étendus. */
public data class CandidateScope(
    val paths: Set<String>,
    val symbols: Set<String>,
)

public object ConflictDetector {

    /**
     * Recouvrement de périmètre avec les travaux actifs.
     *
     * DEUX comparaisons, pas une. Deux agents peuvent éditer des fichiers
     * différents et partager le MÊME symbole — le conflit est alors invisible au
     * recoupement de chemins, et c'est précisément celui qui casse en silence.
     *
     * Les fichiers étendus d'un item actif comptent aussi : c'est ce que le
     * résolveur de code a déduit de ses symboles, donc du périmètre réellement
     * touché, pas seulement déclaré.
     */
    public fun detect(candidate: CandidateScope, active: List<WorkItem>, reason: ConflictReason): List<Conflict> {
        if (reason == ConflictReason.SHARED_PATH) {
            if (candidate.paths.isEmpty()) return emptyList()
            return active
                .mapNotNull { item ->
                    val itsPaths = item.scope.paths.toSet() + item.draft.expandedFiles
                    val shared = itsPaths.intersect(candidate.paths).sorted()
                    if (shared.isEmpty()) null else Conflict(item.id, reason, shared)
                }
                .sortedBy { it.workItemId.value }
        }

        if (candidate.symbols.isEmpty()) return emptyList()
        return active
            .mapNotNull { item ->
                val shared = item.draft.symbols.toSet().intersect(candidate.symbols).sorted()
                if (shared.isEmpty()) null else Conflict(item.id, reason, shared)
            }
            .sortedBy { it.workItemId.value }
    }

    /** Conflits de chemins ET de symboles, chemins d'abord (plus lisibles). */
    public fun detectAll(candidate: CandidateScope, active: List<WorkItem>): List<Conflict> =
        detect(candidate, active, ConflictReason.SHARED_PATH) +
            detect(candidate, active, ConflictReason.SHARED_SYMBOL)
}

public sealed interface CiGateDecision {
    public val activeCi: Int
    public val limit: Int

    public data class Proceed(override val activeCi: Int, override val limit: Int) : CiGateDecision

    /**
     * La barrière est SATURÉE. Ce n'est pas un refus du travail : c'est un refus
     * de le faire entrer dans le lanceur de CI partagé maintenant. La distinction
     * compte — le travail peut commencer, seule la PR doit attendre.
     */
    public data class Wait(override val activeCi: Int, override val limit: Int) : CiGateDecision
}

/**
 * Le périmètre déclenche-t-il une CI lourde ?
 *
 * Détection par RÈGLE, pas par confiance : un agent qui oublierait de le déclarer
 * saturerait le lanceur partagé, ce qui est exactement l'incident du 26.08.2026.
 * Sous-estimer ce booléen n'est pas neutre — c'est ce qui fait tomber Forgejo.
 */
public object CiTrigger {

    private val WORKFLOW_DIRS = listOf(".github/workflows/", ".forgejo/workflows/")
    private val COMPILED_OR_TESTED = setOf(
        "kt", "kts", "java", "scala", "ts", "tsx", "js", "jsx", "vue",
        "py", "go", "rs", "rb", "gradle", "sql",
    )

    public fun detect(paths: List<String>): Boolean = paths.any { raw ->
        val path = raw.trim().lowercase()
        WORKFLOW_DIRS.any { path.contains(it) } ||
            path.substringAfterLast('.', "").takeIf { it.isNotEmpty() } in COMPILED_OR_TESTED
    }
}

public object CiGate {

    public const val DEFAULT_LIMIT: Int = 2

    /**
     * Compteur GLOBAL, tous dépôts confondus : l'incident d'OOM du 26.08.2026 n'a
     * pas respecté les frontières de dépôt, la barrière non plus.
     */
    public fun evaluate(activeCi: Int, limit: Int = DEFAULT_LIMIT): CiGateDecision {
        val effectiveLimit = if (limit < 1) DEFAULT_LIMIT else limit
        return if (activeCi >= effectiveLimit) {
            CiGateDecision.Wait(activeCi, effectiveLimit)
        } else {
            CiGateDecision.Proceed(activeCi, effectiveLimit)
        }
    }
}
