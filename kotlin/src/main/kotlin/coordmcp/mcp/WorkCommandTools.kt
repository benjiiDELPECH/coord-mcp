package coordmcp.mcp

import coordmcp.application.WorkActionResult
import coordmcp.application.WorkLifecycleUseCase
import coordmcp.application.port.WorkItemWriter
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import java.time.Instant
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/** Outils qui MUTENT l'état. Séparés des lectures : ils engagent, elles observent. */
internal class WorkCommandTools(
    private val lifecycle: WorkLifecycleUseCase,
    private val writer: WorkItemWriter,
) {

    fun register(mcp: Server) {
        registerRelease(mcp)
        registerAbandon(mcp)
        registerRelinkIssue(mcp)
    }

    private fun registerRelease(mcp: Server) {
        mcp.addTool(
            name = "release_work",
            description = "Clôture un work item avec sa leçon. expectedRevision rend l'écriture " +
                "atomique : si un autre agent a muté l'item entre-temps, la réponse est " +
                "STALE_REVISION et RIEN n'est appliqué.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("outcome") {
                        put("type", "string")
                        put(
                            "description",
                            "La leçon durable. Obligatoire : un released muet perdrait la décision.",
                        )
                    }
                    putJsonObject("expectedRevision") {
                        put("type", "integer")
                        put("description", "Révision lue sur l'item. Omise => écriture non contrôlée.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val idArg = McpTooling.workItemIdArg(args)
            McpTooling.workItemIdError(idArg)?.let { motif ->
                return@addTool McpTooling.text(WorkToolJson.invalidArgument(motif))
            }
            val id = McpTooling.parseId(McpTooling.workItemIdRaw(idArg))
                ?: return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("work_item_id illisible"),
                )
            val lesson = when (val o = McpTooling.aliasedTextArg(args, "outcome", "lesson")) {
                McpTooling.TextArg.Absent -> ""
                is McpTooling.TextArg.Present -> o.value
                is McpTooling.TextArg.Ambiguous -> return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument(
                        "outcome et lesson portent des valeurs differentes : ambigu, refuse",
                    ),
                )
            }
            val expected = when (val r = McpTooling.revisionArg(args)) {
                McpTooling.RevisionArg.Absent -> null
                McpTooling.RevisionArg.Malformed -> return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("expectedRevision doit être un entier >= 1"),
                )
                is McpTooling.RevisionArg.Present -> r.revision
            }

            McpTooling.text(
                render(lifecycle.release(id, lesson, Instant.now(), expected)),
            )
        }
    }

    private fun registerAbandon(mcp: Server) {
        mcp.addTool(
            name = "abandon_work",
            description = "Marque un travail comme abandonné. Le motif est OBLIGATOIRE — " +
                "un abandonné sans motif est indistinguable d'un perdu.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("reason") { put("type", "string") }
                    putJsonObject("expectedRevision") { put("type", "integer") }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val idArg = McpTooling.workItemIdArg(args)
            McpTooling.workItemIdError(idArg)?.let { motif ->
                return@addTool McpTooling.text(WorkToolJson.invalidArgument(motif))
            }
            val id = McpTooling.parseId(McpTooling.workItemIdRaw(idArg))
                ?: return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("work_item_id illisible"),
                )
            val reason = McpTooling.arg(args, "reason").orEmpty()
            if (reason.isBlank()) {
                return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("reason est requis (motif d'abandon)"),
                )
            }
            val expected = when (val r = McpTooling.revisionArg(args)) {
                McpTooling.RevisionArg.Absent -> null
                McpTooling.RevisionArg.Malformed -> return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("expectedRevision doit être un entier >= 1"),
                )
                is McpTooling.RevisionArg.Present -> r.revision
            }

            McpTooling.text(render(lifecycle.abandon(id, reason, Instant.now(), expected)))
        }
    }

    private fun registerRelinkIssue(mcp: Server) {
        mcp.addTool(
            name = "relink_issue",
            description = "Rattache un numéro d'issue à un travail, sans changer son statut.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") { put("type", "string") }
                    putJsonObject("issue_number") {
                        put("type", "integer")
                        put("description", "Numéro d'issue, strictement positif.")
                    }
                    putJsonObject("expectedRevision") { put("type", "integer") }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val args = request.arguments
            val idArg = McpTooling.workItemIdArg(args)
            McpTooling.workItemIdError(idArg)?.let { motif ->
                return@addTool McpTooling.text(WorkToolJson.invalidArgument(motif))
            }
            val id = McpTooling.parseId(McpTooling.workItemIdRaw(idArg))
                ?: return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("work_item_id illisible"),
                )
            val issueNumber = McpTooling.arg(args, "issue_number")?.toIntOrNull()
                ?: return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("issue_number doit être un entier"),
                )
            if (issueNumber <= 0) {
                return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("issue_number doit être strictement positif"),
                )
            }
            val expected = when (val r = McpTooling.revisionArg(args)) {
                McpTooling.RevisionArg.Absent -> null
                McpTooling.RevisionArg.Malformed -> return@addTool McpTooling.text(
                    WorkToolJson.invalidArgument("expectedRevision doit être un entier >= 1"),
                )
                is McpTooling.RevisionArg.Present -> r.revision
            }

            McpTooling.text(
                WorkToolJson.outcome(writer.linkIssue(id, issueNumber, Instant.now(), expected)),
            )
        }
    }

    private fun render(result: WorkActionResult): String = when (result) {
        WorkActionResult.NotFound -> WorkToolJson.notFound("")
        is WorkActionResult.Refused -> WorkToolJson.refused(result.refusals.joinToString())
        is WorkActionResult.Done -> WorkToolJson.outcome(result.outcome, result.status)
    }
}
