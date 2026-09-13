package coordmcp.application.port

/**
 * Port vers le diff git : quels fichiers ont RÉELLEMENT changé.
 *
 * C'est ce qui distingue « ce que j'ai déclaré toucher » de « ce que j'ai
 * touché ». Un agent qui déborde de son périmètre sans le dire crée une
 * collision que personne n'a vue venir.
 *
 * `Unavailable` n'est jamais « aucun changement » : un dépôt sans git, une
 * référence de comparaison absente, un délai dépassé — trois cas où RIEN n'a été
 * vérifié, et qu'il serait malhonnête de rendre comme une liste vide.
 */
public interface DiffPort {
    public fun changedFiles(request: DiffRequest): DiffResult
}

public data class DiffRequest(
    val repoPath: String,
    /** Fichiers fournis explicitement : court-circuite toute détection. */
    val explicitFiles: List<String>? = null,
    /** Worktree à interroger à la place de `repoPath`. */
    val worktreePath: String? = null,
)

public enum class DiffSource { EXPLICIT, WORKTREE_OVERRIDE, REPO_HEAD }

public sealed interface DiffResult {
    public data class Resolved(
        public val files: Set<String>,
        public val source: DiffSource,
        /** Un diff vide sur la branche de référence est un piège : on l'explique. */
        public val warnings: List<String> = emptyList(),
    ) : DiffResult

    public data class Unavailable(public val reason: String) : DiffResult
}
