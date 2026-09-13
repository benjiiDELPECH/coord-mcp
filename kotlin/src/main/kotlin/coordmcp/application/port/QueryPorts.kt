package coordmcp.application.port

import coordmcp.domain.AdrAllocation
import coordmcp.domain.AuditEntry

/**
 * Ports de LECTURE des registres annexes.
 *
 * Séparés de `WorkItemReader` parce que ce sont d'autres responsabilités : le
 * registre des travaux, la piste d'audit et l'allocation des numéros d'ADR
 * évoluent pour des raisons différentes. Un seul port fourre-tout obligerait
 * chaque adaptateur à tout implémenter, y compris ce qu'il ne sert pas.
 */
public interface AuditQueryPort {
    /** Les `limit` dernières entrées, de la plus récente à la plus ancienne. */
    public fun tail(limit: Int): List<AuditEntry>
}

public interface AdrAllocationQueryPort {
    /** Sans `repoPath`, renvoie les allocations de tous les dépôts. */
    public fun allocations(repoPath: String? = null): List<AdrAllocation>
}
