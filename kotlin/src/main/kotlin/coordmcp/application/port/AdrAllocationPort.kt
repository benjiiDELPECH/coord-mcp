package coordmcp.application.port

import java.time.Instant

/**
 * Port d'ALLOCATION — la réservation d'un numéro d'ADR.
 *
 * C'est la garantie centrale de coord-mcp : deux agents ne doivent jamais
 * obtenir le même numéro. Elle repose sur `UNIQUE (repo_path, adr_number)` en
 * base, pas sur une lecture-puis-écriture applicative — qui laisserait
 * exactement la fenêtre de course qu'on veut fermer.
 */
public interface AdrAllocationPort {
    /**
     * Réserve le prochain numéro libre pour un dépôt.
     *
     * @param topicSlug sujet de l'ADR, utilisé pour construire le nom de fichier.
     * @param allocatedTo identifiant de l'agent demandeur, pour l'audit.
     */
    public fun claim(
        repoPath: String,
        topicSlug: String,
        allocatedTo: String?,
        at: Instant,
    ): AdrClaim
}

public sealed interface AdrClaim {
    public data class Allocated(
        public val number: Int,
        public val filename: String,
    ) : AdrClaim

    /**
     * Échec APRÈS épuisement des tentatives. Nommé, jamais silencieux : ne pas
     * savoir si un numéro a été réservé est pire que ne pas en obtenir.
     */
    public data class Contended(public val attempts: Int) : AdrClaim

    /**
     * Slug refusé. Le port exige un slug VALIDE (`[a-z0-9-]`), pas un sujet brut :
     * écrire `ADR-001-Capex Monte Carlo.md` produirait un fichier hors convention
     * que le scan suivant ne reconnaîtrait pas — donc un numéro réalloué plus tard.
     */
    public data class Rejected(public val detail: String) : AdrClaim
}
