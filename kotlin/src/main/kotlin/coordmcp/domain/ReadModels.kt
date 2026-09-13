package coordmcp.domain

import java.time.Instant

/**
 * Modèles de LECTURE — volontairement distincts des agrégats.
 *
 * `AuditEntry` n'est pas une entité : c'est un fait passé, immuable, qu'on
 * affiche. Le modeler comme un agrégat mutable serait un contresens — et
 * l'exposer tel quel dans l'API MCP ferait fuiter la structure de la table.
 */

public data class AuditEntry(
    public val id: Long,
    public val timestamp: Instant,
    public val tool: String,
    public val agentId: String?,
    public val workItemId: String?,
    public val argsJson: String?,
) {
    /** Un appel d'outil sans agent identifié n'est pas exploitable en audit : on le dit. */
    public val hasProvenance: Boolean get() = !agentId.isNullOrBlank()
}

public data class AdrAllocation(
    public val adrNumber: Int,
    public val repoPath: String,
    public val topicSlug: String,
    public val filename: String,
    public val allocatedTo: String?,
    public val allocatedAt: Instant,
)
