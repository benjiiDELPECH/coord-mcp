package coordmcp.mcp

import coordmcp.application.port.AdrAllocationPort
import coordmcp.application.port.AdrAllocationQueryPort
import coordmcp.application.port.AdrClaim
import coordmcp.application.port.AuditQueryPort
import io.modelcontextprotocol.kotlin.sdk.server.Server
import coordmcp.domain.AdrNumbering
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import java.time.Instant
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Outils des registres ANNEXES : piste d'audit et allocation des numéros d'ADR.
 *
 * Ces deux registres ne concernent pas l'état d'un travail mais la mémoire de ce
 * qui a été fait et la réservation de ressources partagées. Les ranger avec les
 * outils de travail aurait brouillé la frontière.
 */
internal class RegistryTools(
    private val audit: AuditQueryPort,
    private val allocations: AdrAllocationQueryPort,
    private val adr: AdrAllocationPort,
) {

    fun register(mcp: Server) {
        registerAuditTail(mcp)
        registerAdrAllocations(mcp)
        registerClaimAdrNumber(mcp)
    }

    /**
     * Allocation d'un numéro d'ADR.
     *
     * Le SLUG est calculé ICI, par le domaine (`AdrNumbering.slugify`) : c'est une
     * règle de nommage, pas un détail d'adaptateur. Deux implémentations qui
     * slugifient différemment écriraient deux fichiers pour un même numéro.
     */
    private fun registerClaimAdrNumber(mcp: Server) {
        mcp.addTool(
            name = "claim_adr_number",
            description = "Réserve le prochain numéro d'ADR libre pour un dépôt. " +
                "Atomique : deux agents ne peuvent pas obtenir le même numéro.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("repo_path") {
                        put("type", "string")
                        put("description", "Chemin absolu du dépôt.")
                    }
                    putJsonObject("topic") {
                        put("type", "string")
                        put("description", "Sujet de l'ADR, slugifié pour le nom de fichier.")
                    }
                    putJsonObject("allocated_to") {
                        put("type", "string")
                        put("description", "Identifiant de l'agent demandeur, pour l'audit.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val repoPath = McpTooling.arg(args, "repo_path")
            val topic = McpTooling.arg(args, "topic")
            if (repoPath.isNullOrBlank() || topic.isNullOrBlank()) {
                return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("repo_path et topic sont requis"),
                )
            }
            val allocatedTo = McpTooling.arg(args, "allocated_to")

            val claim = adr.claim(repoPath, AdrNumbering.slugify(topic), allocatedTo, Instant.now())
            McpTooling.text(
                when (claim) {
                    is AdrClaim.Allocated -> RegistryToolJson.adrAllocated(claim.number, claim.filename)
                    is AdrClaim.Contended -> RegistryToolJson.adrContended(claim.attempts)
                    is AdrClaim.Rejected -> WorkToolJson.invalidArgument(claim.detail)
                },
            )
        }
    }

    private fun registerAuditTail(mcp: Server) {
        mcp.addTool(
            name = "audit_tail",
            description = "Dernières entrées d'audit, de la plus récente à la plus ancienne " +
                "(borné à 500 pour ne pas noyer le contexte).",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("limit") {
                        put("type", "integer")
                        put("description", "Nombre d'entrées souhaité. Défaut 50, maximum 500.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val requested = McpTooling.arg(request.arguments, "limit")
            val limit = when {
                requested == null -> DEFAULT_TAIL
                else -> requested.toIntOrNull()
                    ?: return@addTool McpTooling.text(
                        WorkToolJson.invalidArgument("limit doit être un entier, reçu '$requested'"),
                    )
            }
            McpTooling.text(RegistryToolJson.auditEntries(audit.tail(limit)))
        }
    }

    private fun registerAdrAllocations(mcp: Server) {
        mcp.addTool(
            name = "list_adr_allocations",
            description = "Numéros d'ADR déjà alloués, éventuellement filtrés par dépôt. " +
                "Certifie qu'un numéro n'est pas déjà pris avant d'en réclamer un.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("repo_path") {
                        put("type", "string")
                        put("description", "Chemin absolu du dépôt. Omis => tous les dépôts.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val repoPath = McpTooling.arg(request.arguments, "repo_path")
            McpTooling.text(RegistryToolJson.allocations(allocations.allocations(repoPath)))
        }
    }

    private companion object {
        const val DEFAULT_TAIL = 50
    }
}
