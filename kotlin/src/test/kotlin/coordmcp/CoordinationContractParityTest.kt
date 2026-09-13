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
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue

/**
 * PARITÉ Python → Kotlin — le contrat des outils de coordination.
 *
 * Référence : l'implémentation Python, récupérable par
 * `git show <commit-avant-le-portage>:src/server.py`. Signatures :
 *
 *     get_work(work_item_id: str)
 *     release_work(work_item_id: str)
 *     abandon_work(work_item_id: str, reason: str = "")
 *     relink_issue(work_item_id: str, issue_number: int, note: str = "")
 *     list_active_work(repo_path: str | None = None) -> list[dict[str, Any]]
 *
 * QUATRE OUTILS, UN SEUL VOCABULAIRE : `work_item_id`.
 *
 * Cette suite est écrite pour ÉCHOUER sur le portage du 2026-09-13, qui a
 * conservé les NOMS d'outils en modifiant les contrats. Trois régressions, une
 * par niveau — syntaxique (`work_item_id` → `id`), structurel (tableau →
 * objet refusé par le client), sémantique (`repo_path` ignoré).
 *
 * L'ORDRE DES TESTS EST DÉLIBÉRÉ : un simple snapshot de `tools/list` attrape
 * deux des trois SANS aucun appel. On écrit le test le moins cher en premier,
 * parce que `outputSchema = null` était vert conceptuellement et faux à
 * l'exécution — un test unitaire vert n'aurait rien vu.
 *
 * Nécessite le service sur 8015 ; sans lui, les tests sont IGNORÉS (et non
 * verts), sinon ils ne prouveraient rien.
 */
class CoordinationContractParityTest {

    private val url = System.getenv("COORD_MCP_LIVE_URL") ?: "http://127.0.0.1:8015/mcp"

    /** Les outils qui prennent un identifiant. Le nom canonique Python est `work_item_id`. */
    private val outilsAIdentifiant = listOf("get_work", "release_work", "abandon_work", "relink_issue")

    private fun serviceIsUp(): Boolean = try {
        java.net.Socket("127.0.0.1", java.net.URI(url).port).use { true }
    } catch (_: java.io.IOException) {
        false
    }

    private fun withClient(block: suspend (Client) -> Unit): Unit = runBlocking {
        assumeTrue(serviceIsUp(), "service MCP absent sur $url — test de parité ignoré")
        val http = HttpClient { install(SSE) }
        val client = Client(clientInfo = Implementation(name = "parity-test", version = "1"))
        try {
            client.connect(StreamableHttpClientTransport(client = http, url = url))
            block(client)
        } finally {
            client.close()
            http.close()
        }
    }

    private suspend fun Client.appeler(nom: String, args: kotlinx.serialization.json.JsonObject? = null): String {
        val params = if (args == null) CallToolRequestParams(name = nom)
        else CallToolRequestParams(name = nom, arguments = args)
        val result = clientCall(params)
        return result.content.filterIsInstance<TextContent>().firstOrNull()?.text.orEmpty()
    }

    private suspend fun Client.clientCall(params: CallToolRequestParams) =
        callTool(CallToolRequest(params))

    /** Niveau 1 — snapshot de `tools/list`. Aucun appel. Attrape la régression syntaxique. */
    @Test
    fun `les quatre outils prennent work_item_id, pas id`() = withClient { client ->
        val tools = client.listTools().tools.associateBy { it.name }

        for (nom in outilsAIdentifiant) {
            val outil = tools[nom] ?: error("outil absent du serveur : $nom")
            val props = outil.inputSchema.properties?.keys.orEmpty()
            assertTrue(
                "work_item_id" in props,
                "$nom doit déclarer `work_item_id` (contrat Python). Déclaré : $props",
            )
            assertTrue(
                "id" !in props,
                "$nom déclare `id` — un SECOND vocabulaire d'identifiant a été introduit " +
                    "à côté de `work_item_id` (CheckinTools). Déclaré : $props",
            )
        }
    }

    /** Niveau 1 — toujours sans appel. Attrape la régression sémantique. */
    @Test
    fun `list_active_work declare repo_path`() = withClient { client ->
        val tools = client.listTools().tools.associateBy { it.name }
        val outil = tools["list_active_work"] ?: error("outil absent : list_active_work")
        val props = outil.inputSchema.properties?.keys.orEmpty()
        assertTrue(
            "repo_path" in props,
            "list_active_work doit déclarer `repo_path` (contrat Python). Sans lui, un " +
                "filtre DEMANDÉ disparaît en silence et rend une réponse plausible et fausse. " +
                "Déclaré : $props",
        )
    }

    /** Niveau 3 — appel réel. Attrape le refus d'enveloppe (-32602). */
    @Test
    fun `list_active_work rend un TABLEAU, pas un objet`() = withClient { client ->
        val texte = client.appeler("list_active_work")
        val parsed = Json.parseToJsonElement(texte)
        assertTrue(
            parsed is JsonArray,
            "le contrat Python rend list[dict] ; obtenu ${parsed::class.simpleName} : ${texte.take(200)}",
        )
    }

    /** Niveau 2 — validation d'arguments. Attrape le renommage du paramètre. */
    @Test
    fun `get_work lit work_item_id et ne le prend pas pour un argument absent`() = withClient { client ->
        val texte = client.appeler(
            "get_work",
            buildJsonObject { put("work_item_id", "wi_000000000000") },
        )
        assertTrue(
            "manquant ou invalide" !in texte,
            "get_work a reçu `work_item_id` mais répond « manquant ou invalide » — " +
                "le paramètre n'est pas lu sous son nom canonique. Réponse : $texte",
        )
    }

    /**
     * Niveau 2 — le cas que la compatibilité transitoire doit REFUSER.
     *
     * Les deux noms avec des valeurs différentes ne peuvent pas être résolus :
     * un `?:` bien intentionné en choisirait un en silence. C'est une erreur.
     * Les deux noms IDENTIQUES sont en revanche acceptables.
     */
    @Test
    fun `les deux noms d identifiant avec des valeurs differentes sont une erreur explicite`() = withClient { client ->
        val texte = client.appeler(
            "get_work",
            buildJsonObject {
                put("work_item_id", "wi_000000000000")
                put("id", "wi_111111111111")
            },
        )
        assertTrue(
            "manquant ou invalide" in texte || "ambigu" in texte.lowercase() || "conflit" in texte.lowercase(),
            "deux identifiants DIFFÉRENTS doivent produire une erreur explicite, " +
                "pas un choix silencieux. Réponse : $texte",
        )
    }
}
