package coordmcp.mcp

import coordmcp.application.CheckinCommand
import coordmcp.application.CheckinResult
import coordmcp.application.CheckoutCommand
import coordmcp.application.CheckoutUseCase
import coordmcp.application.CheckinUseCase
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Outil `checkin` — déclarer un travail.
 *
 * Classe séparée : `checkin` porte la détection de conflits et la barrière CI,
 * ce qui en fait une responsabilité distincte des commandes de cycle de vie.
 */
internal class CheckinTools(
    private val checkin: CheckinUseCase,
    private val checkout: CheckoutUseCase,
) {

    fun register(mcp: Server) {
        registerCheckout(mcp)
        mcp.addTool(
            name = "checkin",
            description = "Déclare un travail et rend ce qui bloque : conflits de périmètre " +
                "avec les travaux actifs, et état de la barrière de concurrence CI. " +
                "Un conflit est SIGNALÉ, jamais tranché ici.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("repo_path") { put("type", "string") }
                    putJsonObject("title") { put("type", "string") }
                    putJsonObject("scope_files") {
                        put("type", "array")
                        put("description", "Chemins relatifs touchés par le travail.")
                    }
                    putJsonObject("scope_symbols") {
                        put("type", "array")
                        put("description", "Symboles touchés — révèlent les conflits invisibles aux seuls chemins.")
                    }
                    putJsonObject("agent_id") { put("type", "string") }
                    putJsonObject("triggers_ci") {
                        put("type", "boolean")
                        put("description", "Omis => détecté par règle depuis le périmètre.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val result = checkin.checkin(
                CheckinCommand(
                    repoPath = McpTooling.arg(args, "repo_path").orEmpty(),
                    title = McpTooling.arg(args, "title").orEmpty(),
                    scopeFiles = stringList(args, "scope_files"),
                    scopeSymbols = stringList(args, "scope_symbols"),
                    agentId = McpTooling.arg(args, "agent_id"),
                    triggersCi = boolOrNull(args, "triggers_ci"),
                ),
            )
            McpTooling.text(
                when (result) {
                    is CheckinResult.Invalid -> WorkToolJson.invalidArgument(result.detail)
                    is CheckinResult.Created -> RegistryToolJson.checkinCreated(result)
                },
            )
        }
    }

    /**
     * `checkout_work` — la porte d'ARRIVÉE.
     *
     * Différence essentielle avec `checkin` : `checkin` SIGNAIT la barrière CI,
     * `checkout_work` l'APPLIQUE. Si elle est saturée, le statut ne change pas —
     * ce n'est pas à l'appelant de vouloir bien se comporter.
     */
    private fun registerCheckout(mcp: Server) {
        mcp.addTool(
            name = "checkout_work",
            description = "Fait entrer un travail en cours (checked_out). " +
                "La barrière de concurrence CI est APPLIQUÉE : si elle est saturée, " +
                "le statut n'est pas modifié.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("expected_revision") { put("type", "integer") }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val expected = when (val r = McpTooling.revisionArg(args)) {
                McpTooling.RevisionArg.Absent -> null
                McpTooling.RevisionArg.Malformed -> return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("expected_revision doit être un entier >= 1"),
                )
                is McpTooling.RevisionArg.Present -> r.revision
            }
            val result = checkout.checkout(
                CheckoutCommand(
                    workItemId = McpTooling.arg(args, "work_item_id").orEmpty(),
                    expectedRevision = expected,
                ),
            )
            McpTooling.text(RegistryToolJson.checkoutResult(result))
        }
    }

    private fun stringList(arguments: Map<String, JsonElement>?, key: String): List<String> {
        val element = arguments?.get(key) ?: return emptyList()
        val array = element as? JsonArray ?: return listOfNotNull(element.jsonPrimitive.contentOrNull)
        return array.mapNotNull { it.jsonPrimitive.contentOrNull }
    }

    private fun boolOrNull(arguments: Map<String, JsonElement>?, key: String): Boolean? =
        arguments?.get(key)?.jsonPrimitive?.contentOrNull?.toBooleanStrictOrNull()

    private val JsonElement.jsonPrimitive: kotlinx.serialization.json.JsonPrimitive
        get() = this as kotlinx.serialization.json.JsonPrimitive

    private val kotlinx.serialization.json.JsonPrimitive.contentOrNull: String?
        get() = if (this is kotlinx.serialization.json.JsonNull) null else content
}
