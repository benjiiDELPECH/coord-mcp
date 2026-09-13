package coordmcp.mcp

import coordmcp.application.ClaimExistingCommand
import coordmcp.application.ClaimNewCommand
import coordmcp.application.ClaimResult
import coordmcp.application.ClaimUseCase
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/** Outils de rattachement à une issue : `claim_issue` et `claim_new`. */
internal class ClaimTools(private val claims: ClaimUseCase) {

    fun register(mcp: Server) {
        registerClaimIssue(mcp)
        registerClaimNew(mcp)
    }

    private fun registerClaimIssue(mcp: Server) {
        mcp.addTool(
            name = "claim_issue",
            description = "Rattache un travail à une issue EXISTANTE : s'assigne l'issue puis " +
                "écrit le lien en base. La base n'est écrite qu'après confirmation du suivi d'issues.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("repo_path") { put("type", "string") }
                    putJsonObject("issue_number") { put("type", "integer") }
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
            val result = claims.claimExisting(
                ClaimExistingCommand(
                    workItemId = McpTooling.arg(args, "work_item_id").orEmpty(),
                    repoPath = McpTooling.arg(args, "repo_path").orEmpty(),
                    issueNumber = McpTooling.arg(args, "issue_number")?.toIntOrNull()
                        ?: return@addTool McpTooling.text(
                            WorkToolJson.invalidArgument("issue_number doit être un entier"),
                        ),
                    expectedRevision = expected,
                ),
            )
            McpTooling.text(ClaimToolJson.render(result))
        }
    }

    private fun registerClaimNew(mcp: Server) {
        mcp.addTool(
            name = "claim_new",
            description = "Crée une NOUVELLE issue et rattache le travail. Un jalon introuvable " +
                "fait échouer l'appel — il n'est jamais silencieusement omis.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("repo_path") { put("type", "string") }
                    putJsonObject("title") { put("type", "string") }
                    putJsonObject("body") { put("type", "string") }
                    putJsonObject("labels") { put("type", "array") }
                    putJsonObject("milestone_number") { put("type", "integer") }
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
            val result = claims.claimNew(
                ClaimNewCommand(
                    workItemId = McpTooling.arg(args, "work_item_id").orEmpty(),
                    repoPath = McpTooling.arg(args, "repo_path").orEmpty(),
                    title = McpTooling.arg(args, "title").orEmpty(),
                    body = McpTooling.arg(args, "body").orEmpty(),
                    labels = stringList(args, "labels"),
                    milestoneNumber = McpTooling.arg(args, "milestone_number")?.toIntOrNull(),
                    expectedRevision = expected,
                ),
            )
            McpTooling.text(ClaimToolJson.render(result))
        }
    }

    private fun stringList(arguments: Map<String, JsonElement>?, key: String): List<String> {
        val element = arguments?.get(key) ?: return emptyList()
        val array = element as? JsonArray ?: return listOfNotNull(element.jsonPrimitive.contentOrNull)
        return array.mapNotNull { it.jsonPrimitive.contentOrNull }
    }

    private val JsonElement.jsonPrimitive: kotlinx.serialization.json.JsonPrimitive
        get() = this as kotlinx.serialization.json.JsonPrimitive

    private val kotlinx.serialization.json.JsonPrimitive.contentOrNull: String?
        get() = if (this is kotlinx.serialization.json.JsonNull) null else content
}

/** Sérialisation des résultats de rattachement. */
internal object ClaimToolJson {

    fun render(result: ClaimResult): String = buildJsonObject {
        when (result) {
            is ClaimResult.Bound -> {
                put("issue_number", result.issueNumber)
                put("revision", result.revision.value)
            }
            is ClaimResult.TrackerFailed -> {
                put("error", "TRACKER_FAILED")
                put("detail", result.detail)
            }
            is ClaimResult.Invalid -> {
                put("error", "INVALID_ARGUMENT")
                put("detail", result.detail)
            }
            ClaimResult.NotFound -> put("error", "NOT_FOUND")
            is ClaimResult.Stale -> {
                put("error", "STALE_REVISION")
                put("expected_revision", result.expected.value)
                put("current_revision", result.current.value)
            }
        }
    }.toString()
}
