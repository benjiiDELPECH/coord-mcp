package coordmcp.application.port

/**
 * Port vers le suivi d'issues (GitHub / Forgejo via `gh`).
 *
 * Le port parle en CHEMIN DE DÉPÔT, pas en `owner/repo` : c'est ce que l'appelant
 * possède. Résoudre le slug (`git remote get-url`) est un détail d'adaptateur, et
 * le faire fuiter ici obligerait chaque appelant à connaître la convention de
 * remote du dépôt.
 *
 * Aucune méthode ne lève : un échec est une VALEUR. Une création d'issue qui
 * échoue ne doit jamais être confondue avec une issue créée.
 */
public interface IssueTrackerPort {
    public fun assignToMe(repoPath: String, issueNumber: Int): IssueOutcome

    public fun createIssue(request: NewIssueRequest): IssueOutcome

    /** Le corps brut : le COMPTAGE des cases à cocher est une règle du domaine, pas d'ici. */
    public fun issueBody(repoPath: String, issueNumber: Int): BodyOutcome

    /** PR ouvertes du dépôt. Le CROISEMENT avec les fichiers touchés est une règle du domaine. */
    public fun openPullRequests(repoPath: String): PullRequestOutcome

    /** Issues ouvertes ressemblant à ce titre. Détecte un travail déjà couvert. */
    public fun searchOpenIssues(repoPath: String, query: String, limit: Int = 5): IssueSearchOutcome
}

public data class NewIssueRequest(
    val repoPath: String,
    val title: String,
    val body: String,
    val labels: List<String>,
    /** Numéro de jalon ; l'adaptateur le résout en TITRE, que `gh` attend. */
    val milestoneNumber: Int?,
)

/** Corps d'issue. `Unavailable` interdit de conclure quoi que ce soit des critères. */
public sealed interface BodyOutcome {
    public data class Fetched(public val body: String) : BodyOutcome
    public data class Unavailable(public val reason: String) : BodyOutcome
}

/** Issues ouvertes correspondant à une recherche textuelle. */
public data class IssueRef(
    public val number: Int,
    public val title: String,
    public val url: String,
)

public sealed interface IssueSearchOutcome {
    public data class Found(public val issues: List<IssueRef>) : IssueSearchOutcome
    public data class Unavailable(public val reason: String) : IssueSearchOutcome
}

public sealed interface PullRequestOutcome {
    public data class Fetched(public val pullRequests: List<coordmcp.domain.PullRequestRef>) : PullRequestOutcome
    public data class Unavailable(public val reason: String) : PullRequestOutcome
}

public sealed interface IssueOutcome {
    /** L'issue existe et son numéro est connu — c'est ce qu'on écrit en base. */
    public data class Ok(public val issueNumber: Int) : IssueOutcome

    /** Échec nommé. Jamais un numéro inventé, jamais un succès supposé. */
    public data class Failed(public val detail: String) : IssueOutcome
}
