package coordmcp

import io.ktor.client.HttpClient
import io.ktor.client.plugins.sse.SSE
import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.kotlin.sdk.client.StreamableHttpClientTransport
import io.modelcontextprotocol.kotlin.sdk.types.CallToolRequest
import io.modelcontextprotocol.kotlin.sdk.types.CallToolRequestParams
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue

/**
 * Contrat de PROTOCOLE, avec un VRAI client MCP.
 *
 * Cette suite existe à cause d'un échec réel : la bascule du 2026-09-13 a laissé
 * le service debout et les appels d'outils REFUSÉS, parce que le SDK Kotlin
 * déclarait un schéma de sortie par défaut (`{"result": [...]}`) que nos charges
 * utiles — des objets — ne respectaient pas.
 *
 * Un double-run sur les DONNÉES (85 items identiques des deux côtés) ne pouvait
 * pas le voir : il faut un client qui valide l'enveloppe JSON-RPC.
 *
 * Nécessite le service sur 8015. Lancement :
 *   COORD_MCP_LIVE_URL=http://127.0.0.1:8015/mcp gradle test --tests '*ProtocolContractTest*'
 */
class ProtocolContractTest {

    private val url = System.getenv("COORD_MCP_LIVE_URL") ?: "http://127.0.0.1:8015/mcp"

    /** Sans service en face, ce test ne prouve rien : on l'ignore au lieu de le faire échouer. */
    private fun serviceIsUp(): Boolean = try {
        java.net.Socket("127.0.0.1", java.net.URI(url).port).use { true }
    } catch (_: java.io.IOException) {
        false
    }

    @Test
    fun `un client MCP reel peut lister ET appeler un outil sans erreur de protocole`() = runBlocking {
        assumeTrue(serviceIsUp(), "service MCP absent sur $url — test de protocole ignoré")
        val http = HttpClient { install(SSE) }
        val client = Client(clientInfo = Implementation(name = "protocol-test", version = "1"))

        try {
            client.connect(StreamableHttpClientTransport(client = http, url = url))

            val tools = client.listTools().tools
            assertTrue(tools.size >= 13, "attendu au moins 13 outils, obtenu ${tools.size}")

            // L'appel est ce qui échouait : le client valide la réponse contre le
            // schéma de sortie déclaré, s'il y en a un.
            val result = client.callTool(
                CallToolRequest(CallToolRequestParams(name = "list_active_work")),
            )
            val text = result.content.filterIsInstance<TextContent>().firstOrNull()?.text.orEmpty()
            assertTrue(text.isNotBlank(), "l'outil doit rendre un contenu")
            assertTrue(text.contains("\"count\""), "la charge utile doit être le JSON attendu : $text")
        } finally {
            client.close()
            http.close()
        }
    }
}
