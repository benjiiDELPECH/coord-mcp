package coordmcp.application.port

/**
 * Port vers la mémoire durable (Graphiti) — décisions déjà prises sur un sujet.
 *
 * C'est ce qui permet à `checkin` de dire « lis ce qui existe AVANT d'envisager
 * de créer », plutôt que de laisser un agent reconcevoir ce qui est déjà tranché.
 *
 * `Unavailable` n'est PAS « aucune décision » : le service rend `null` au lieu de
 * `0`, ce qui empêche la branche `REVIEW_PRIOR_DECISIONS` de se taire en laissant
 * croire qu'on a regardé.
 */
public interface PriorDecisionsPort {
    /** `null` si le dépôt n'est mappé à aucun groupe : on ne devine pas, on saute. */
    public fun groupFor(repoPath: String): String?

    public fun search(repoPath: String, topic: String): PriorDecisionsOutcome
}

public sealed interface PriorDecisionsOutcome {
    public data class Found(public val count: Int) : PriorDecisionsOutcome

    public data class Unavailable(public val reason: String) : PriorDecisionsOutcome
}
