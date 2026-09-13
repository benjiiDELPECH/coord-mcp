package coordmcp.adapter.outbound.gh

import coordmcp.application.port.IssueRef
import coordmcp.domain.PullRequestRef

/**
 * Lecture minimale du JSON de `gh` — séparée de l'exécution des commandes.
 *
 * Le JSON de `gh` est plat et connu ; tirer une dépendance de parsing complet
 * pour quatre champs serait disproportionné, et mélanger les deux
 * responsabilités faisait dépasser la taille utile de l'adaptateur.
 */
internal object GhJson {

    /** Extraction d'une chaîne de premier niveau. `\n` et `\"` sont déséchappés. */
    fun string(json: String, key: String): String? {
        val match = Regex("\"$key\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"").find(json) ?: return null
        return match.groupValues[1].replace("\\n", "\n").replace("\\\"", "\"")
    }

    /**
     * `gh pr list --json …` rend `[{number,title,url,files:[{path}]}]`.
     *
     * Un élément illisible est IGNORÉ — mais l'appel reste `Fetched` : on ne
     * prétend pas que la liste est vide par nature.
     */
    /** `gh issue list --json number,title,url` : meme forme plate que les PR. */
    fun issues(json: String): List<IssueRef> =
        Regex("\\{[^{}]*\"number\"\\s*:\\s*(\\d+)[^{}]*\\}").findAll(json).mapNotNull { match ->
            val block = match.value
            val number = match.groupValues[1].toIntOrNull() ?: return@mapNotNull null
            IssueRef(
                number = number,
                title = string(block, "title").orEmpty(),
                url = string(block, "url").orEmpty(),
            )
        }.toList()

    fun pullRequests(json: String): List<PullRequestRef> =
        Regex("\\{[^{}]*\"number\"\\s*:\\s*(\\d+)[^{}]*\\}").findAll(json).mapNotNull { match ->
            val block = match.value
            val number = match.groupValues[1].toIntOrNull() ?: return@mapNotNull null
            val files = Regex("\"path\"\\s*:\\s*\"([^\"]+)\"").findAll(block)
                .map { it.groupValues[1] }.toSet()
            PullRequestRef(
                number = number,
                title = string(block, "title").orEmpty(),
                url = string(block, "url").orEmpty(),
                files = files,
            )
        }.toList()
}
