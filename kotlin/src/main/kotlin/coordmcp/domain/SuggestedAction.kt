package coordmcp.domain

/**
 * Recommandation rendue par `checkin` — règle PURE, testable sans réseau.
 *
 * Les quatre libellés sont repris **au caractère près** du service Python : des
 * agents peuvent comparer ces chaînes, et un remplacement à l'identique se joue
 * sur ce genre de détail. Le tiret est un tiret cadratin (U+2014), pas un trait
 * d'union — c'est ce que le Python écrit.
 *
 * ORDRE DE PRIORITÉ, et il est significatif : un conflit de périmètre prime sur
 * tout ; une décision déjà prise prime sur une issue qui ressemble. On veut que
 * l'agent lise ce qui existe AVANT d'envisager de créer.
 */
public object SuggestedAction {

    public fun of(
        conflicts: Int,
        similarOpenIssues: Int,
        /**
         * Décisions Graphiti déjà prises sur le sujet.
         *
         * `null` signifie **non vérifié**, et ce n'est PAS zéro : le service
         * Kotlin n'interroge pas encore Graphiti. Rendre `0` ferait taire la
         * branche `REVIEW_PRIOR_DECISIONS` en laissant croire qu'on a regardé —
         * exactement le défaut qu'on refuse partout ailleurs.
         */
        priorDecisions: Int?,
    ): String = when {
        conflicts > 0 ->
            "REVIEW_CONFLICTS — overlapping scope detected, decide whether to abort or coordinate"

        priorDecisions != null && priorDecisions > 0 ->
            "REVIEW_PRIOR_DECISIONS — $priorDecisions Graphiti node(s) already exist on this " +
                "topic, possibly on an unmerged branch — read them before designing an approach"

        similarOpenIssues > 0 ->
            "CONSIDER_CLAIMING — $similarOpenIssues similar open issue(s) might already cover this"

        else -> "CREATE_NEW — no conflicts, no similar work, safe to create new issue"
    }
}
