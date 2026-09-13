package coordmcp.domain

/**
 * Règles de revue — PURES. L'adaptateur rapporte le TEXTE, le domaine le JUGE.
 *
 * Compter des cases à cocher et croiser des ensembles de fichiers ne sont pas
 * des appels réseau : ce sont des règles. Les mettre ici les rend testables en
 * millisecondes, et surtout indépendantes du suivi d'issues utilisé.
 */

public data class AcceptanceCriteria(
    public val total: Int,
    public val checked: Int,
    public val unchecked: Int,
) {
    /**
     * `total > 0` est essentiel : une issue SANS critère n'est pas une issue dont
     * les critères sont satisfaits. Confondre les deux ferait passer une absence
     * de vérification pour une vérification réussie.
     */
    public val allChecked: Boolean get() = total > 0 && unchecked == 0

    public companion object {
        private val CHECKED = Regex("(?m)^\\s*-\\s*\\[[xX]\\]")
        private val UNCHECKED = Regex("(?m)^\\s*-\\s*\\[\\s*\\]")

        public fun parse(body: String?): AcceptanceCriteria {
            val text = body.orEmpty()
            val checked = CHECKED.findAll(text).count()
            val unchecked = UNCHECKED.findAll(text).count()
            return AcceptanceCriteria(checked + unchecked, checked, unchecked)
        }
    }
}

public data class PullRequestRef(
    val number: Int,
    val title: String,
    val url: String,
    val files: Set<String>,
)

/** Une PR ouverte dont les fichiers recouvrent ceux qu'on s'apprête à toucher. */
public data class PullRequestCollision(
    val number: Int,
    val title: String,
    val url: String,
    val overlappingFiles: List<String>,
)

public object PullRequestOverlap {

    /**
     * PR ouvertes qui se disputent au moins un fichier.
     *
     * Ordre stable par numéro : un rapport qui change d'ordre entre deux appels
     * ne se compare pas.
     */
    public fun detect(pullRequests: List<PullRequestRef>, files: Collection<String>): List<PullRequestCollision> {
        val target = files.toSet()
        if (target.isEmpty()) return emptyList()

        return pullRequests
            .mapNotNull { pr ->
                val overlap = pr.files.intersect(target).sorted()
                if (overlap.isEmpty()) null
                else PullRequestCollision(pr.number, pr.title, pr.url, overlap)
            }
            .sortedBy { it.number }
    }
}
