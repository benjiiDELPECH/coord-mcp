package coordmcp.domain

/**
 * Mapping dépôt → groupe Graphiti — règle PURE, testable sans réseau.
 *
 * Les identifiants de groupe utilisent des **underscores**, jamais des tirets :
 * FalkorDB FTS lit le tiret comme une négation et la requête échoue. Un
 * `group_id` construit depuis un nom de dépôt (`alert-immo`) produirait donc une
 * recherche silencieusement vide — d'où une table explicite plutôt qu'une
 * transformation automatique.
 *
 * Un dépôt inconnu rend `null`, et l'appelant **saute** l'appel Graphiti au lieu
 * de deviner un groupe. Deviner produirait soit une erreur, soit — pire — une
 * réponse vide qu'on prendrait pour « aucune décision existante ».
 */
public object GraphitiGroups {

    private val REPO_TO_GROUP = mapOf(
        "alert-immo" to "alert_immo",
        "delpech-infra" to "delpech_infra",
    )

    public fun of(repoPath: String): String? =
        REPO_TO_GROUP.entries.firstOrNull { repoPath.contains(it.key) }?.value
}
