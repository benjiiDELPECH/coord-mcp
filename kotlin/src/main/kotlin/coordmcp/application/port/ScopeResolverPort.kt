package coordmcp.application.port

/**
 * Port vers le résolveur de code (GitNexus) : symboles déclarés → fichiers réellement touchés.
 *
 * C'est ce qui rend visible le conflit qu'aucun recoupement de chemins ne voit :
 * deux agents peuvent éditer des fichiers DIFFÉRENTS et se disputer le même
 * symbole.
 *
 * Le type porte la distinction que le service Python écrasait : `Unavailable`
 * n'est PAS « aucun fichier ». Rendre une liste vide quand le résolveur est
 * indisponible ferait passer une absence de vérification pour une vérification
 * réussie — ce qui est exactement la classe de défaut qu'on a passé la journée à
 * corriger.
 */
public interface ScopeResolverPort {
    public fun expand(repoPath: String, symbols: List<String>, depth: Int = 2): ScopeExpansion
}

public sealed interface ScopeExpansion {
    /** Le résolveur a répondu. `files` peut être vide si les symboles n'ont aucune dépendance. */
    public data class Resolved(
        public val files: Set<String>,
        /** Symboles que le résolveur n'a PAS su résoudre, avec la raison. Jamais tus. */
        public val unresolved: Map<String, String> = emptyMap(),
    ) : ScopeExpansion

    /** Le résolveur n'a pas pu répondre : RIEN n'a été vérifié. */
    public data class Unavailable(public val reason: String) : ScopeExpansion

    public companion object {
        /** Aucun symbole déclaré : il n'y a rien à résoudre, et ce n'est pas une indisponibilité. */
        public fun notNeeded(): ScopeExpansion = Resolved(emptySet())
    }
}
