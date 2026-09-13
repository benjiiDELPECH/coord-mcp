package coordmcp.mcp

import coordmcp.application.CheckinUseCase
import coordmcp.application.ClaimUseCase
import coordmcp.application.CheckoutUseCase
import coordmcp.application.WorkLifecycleUseCase
import coordmcp.application.port.AdrAllocationPort
import coordmcp.application.port.AdrAllocationQueryPort
import coordmcp.application.port.AuditQueryPort
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.server.ServerOptions
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import io.modelcontextprotocol.kotlin.sdk.types.ServerCapabilities

/**
 * Ce dont l'adaptateur MCP a besoin, groupé.
 *
 * Sept ports en arguments positionnels, c'était sept occasions d'inverser deux
 * paramètres du même type sans que le compilateur s'en aperçoive. Le
 * regroupement rend l'appel lisible et le remplacement d'un port explicite.
 */
public data class CoordPorts(
    public val reader: WorkItemReader,
    public val audit: AuditQueryPort,
    public val allocations: AdrAllocationQueryPort,
    public val adr: AdrAllocationPort,
    public val lifecycle: WorkLifecycleUseCase,
    public val checkin: CheckinUseCase,
    public val checkout: CheckoutUseCase,
    public val claims: ClaimUseCase,
    public val writer: WorkItemWriter,
    public val resources: AutoCloseable? = null,
)

/**
 * ADAPTATEUR CONDUISANT — il ne connaît QUE des ports.
 *
 * Aucune mention de SQL, de jOOQ, de PostgreSQL ni de SQLite. Avant, cette classe
 * instanciait `SqlDelightWorkItemRepository` : le sens de dépendance allait de
 * l'extérieur vers un détail d'infrastructure.
 *
 * Cette classe ne fait qu'ASSEMBLER : créer le serveur, câbler les groupes
 * d'outils, fermer. Chaque groupe vit dans sa propre classe, par responsabilité.
 *
 * Qui choisit l'adaptateur ? `Main.kt`, la racine de composition — le seul
 * endroit autorisé à connaître les deux côtés.
 */
public class CoordMcpServer(private val ports: CoordPorts) {

    public fun server(): Server {
        val mcp = Server(
            serverInfo = Implementation(name = "coord-mcp-kotlin", version = "0.1.0"),
            options = ServerOptions(
                capabilities = ServerCapabilities(
                    tools = ServerCapabilities.Tools(listChanged = false),
                ),
            ),
        )
        WorkQueryTools(ports.reader).register(mcp)
        RegistryTools(ports.audit, ports.allocations, ports.adr).register(mcp)
        CheckinTools(ports.checkin, ports.checkout).register(mcp)
        WorkCommandTools(ports.lifecycle, ports.writer).register(mcp)
        ClaimTools(ports.claims).register(mcp)
        return mcp
    }

    public fun close() {
        ports.resources?.close()
    }
}
