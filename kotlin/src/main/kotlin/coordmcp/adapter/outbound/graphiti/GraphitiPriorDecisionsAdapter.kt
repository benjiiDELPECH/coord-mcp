package coordmcp.adapter.outbound.graphiti

import coordmcp.application.port.PriorDecisionsOutcome
import coordmcp.application.port.PriorDecisionsPort
import coordmcp.domain.GraphitiGroups
import io.ktor.client.HttpClient
import io.ktor.client.plugins.HttpRedirect
import io.ktor.client.plugins.sse.SSE
import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.kotlin.sdk.client.StreamableHttpClientTransport
import io.modelcontextprotocol.kotlin.sdk.types.CallToolRequest
import io.modelcontextprotocol.kotlin.sdk.types.CallToolRequestParams
import io.modelcontextprotocol.kotlin.sdk.types.Implementation
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.put

/**
 * Adaptateur Graphiti — interroge le serveur MCP de mémoire durable.
 *
 * Appel `search_nodes` avec le `group_id` du dépôt, comme le service Python de
 * référence. Trois issues distinctes, jamais confondues :
 *   - `Found(n)`       la mémoire a répondu ;
 *   - `Unavailable(…)` la mémoire n'a pas pu répondre — l'appelant rend alors
 *                      `null`, donc `prior_decisions_checked: false` ;
 *   - dépôt non mappé  `groupFor` rend `null` et on SAUTE l'appel : deviner un
 *                      groupe produirait une réponse vide qu'on prendrait pour
 *                      « aucune décision existante ».
 *
 * Délai borné : une mémoire lente ne doit pas immobiliser un agent.
 */
public class GraphitiPriorDecisionsAdapter(
    private val url: String = System.getenv("COORD_MCP_GRAPHITI_URL")
        ?: "http://localhost:8001/mcp/",
    private val timeoutMs: Long = 4_000,
) : PriorDecisionsPort {

    private val json = Json { ignoreUnknownKeys = true }

    private companion object {
        /** Assez pour diagnostiquer, assez court pour un message. */
        const val MAX_REASON_CHARS = 120
    }

    override fun groupFor(repoPath: String): String? = GraphitiGroups.of(repoPath)

    override fun search(repoPath: String, topic: String): PriorDecisionsOutcome {
        val group = groupFor(repoPath)
            ?: return PriorDecisionsOutcome.Unavailable("dépôt sans groupe Graphiti — appel sauté")

        return try {
            val text = runBlocking {
                withTimeoutOrNull(timeoutMs) { searchNodes(group, topic) }
            } ?: return PriorDecisionsOutcome.Unavailable("délai de ${timeoutMs}ms dépassé")

            PriorDecisionsOutcome.Found(countNodes(text))
        } catch (
            @Suppress("TooGenericExceptionCaught")
            // Volontairement large, et c'est le point : TOUTE panne de la mémoire
            // doit devenir une indisponibilité NOMMÉE. Laisser remonter une
            // exception ferait échouer `checkin` ; l'attraper trop étroitement
            // ferait passer une panne pour « aucune décision ».
            e: Exception,
        ) {
            // Toute panne est une INDISPONIBILITÉ nommée, jamais un zéro silencieux.
            PriorDecisionsOutcome.Unavailable("graphiti injoignable : ${e.message?.take(MAX_REASON_CHARS)}")
        }
    }

    private suspend fun searchNodes(group: String, topic: String): String {
        // HttpRedirect est INDISPENSABLE : le serveur Graphiti répond 307 sur
        // `/mcp/` et ktor ne suit PAS les redirections par défaut. Sans ce
        // plugin, chaque recherche échouait — et comme un échec se traduit par
        // `prior_decisions_checked: false`, la panne aurait été invisible.
        val http = HttpClient {
            install(SSE)
            install(HttpRedirect) { checkHttpMethod = false }
        }
        val client = Client(clientInfo = Implementation(name = "coord-mcp", version = "0.1.0"))
        return try {
            client.connect(StreamableHttpClientTransport(client = http, url = url))
            val result = client.callTool(
                CallToolRequest(
                    CallToolRequestParams(
                        name = "search_nodes",
                        arguments = kotlinx.serialization.json.buildJsonObject {
                            put("query", kotlinx.serialization.json.JsonPrimitive(topic))
                            put(
                                "group_ids",
                                JsonArray(listOf(kotlinx.serialization.json.JsonPrimitive(group))),
                            )
                        },
                    ),
                ),
            )
            result.content.filterIsInstance<TextContent>().firstOrNull()?.text.orEmpty()
        } finally {
            client.close()
            http.close()
        }
    }

    /** La mémoire rend un tableau de nœuds en JSON : on compte, sans interpréter. */
    private fun countNodes(text: String): Int = try {
        (json.parseToJsonElement(text) as? JsonArray)?.size ?: 0
    } catch (_: kotlinx.serialization.SerializationException) {
        0
    }
}
