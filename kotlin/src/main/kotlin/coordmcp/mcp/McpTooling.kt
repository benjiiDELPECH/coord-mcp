package coordmcp.mcp

import coordmcp.domain.DomainResult
import coordmcp.domain.Revision
import coordmcp.domain.WorkItemId
import io.modelcontextprotocol.kotlin.sdk.types.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonPrimitive

/**
 * Outillage commun aux outils MCP : lecture d'arguments et enveloppe de réponse.
 *
 * Les conversions rendent `null` sur une entrée MALFORMÉE — c'est à l'appelant
 * d'en faire un `INVALID_ARGUMENT`. Ce fichier ne décide pas à sa place : la
 * distinction entre « absent » et « illisible » est une décision de contrat, pas
 * de parsing.
 */
internal object McpTooling {

    internal fun arg(arguments: Map<String, JsonElement>?, key: String): String? =
        arguments?.get(key)?.jsonPrimitive?.content

    internal fun parseId(raw: String): WorkItemId? = (WorkItemId.of(raw) as? DomainResult.Ok)?.value

    internal fun parseRevision(raw: String): Revision? =
        raw.toLongOrNull()?.let { (Revision.of(it) as? DomainResult.Ok)?.value }

    /**
     * `expectedRevision` a TROIS états, pas deux.
     *
     * Le réduire à `Revision?` confondait « absent » (légitime : écriture
     * explicitement non contrôlée) et « illisible » (à refuser). Cette confusion
     * dégradait une entrée invalide vers l'écriture SANS GARDE — l'opération la
     * moins sûre. Un type à trois états rend la confusion impossible.
     */
    internal sealed interface RevisionArg {
        data object Absent : RevisionArg
        data class Present(val revision: Revision) : RevisionArg
        data object Malformed : RevisionArg
    }

    internal fun revisionArg(arguments: Map<String, JsonElement>?): RevisionArg {
        val raw = arg(arguments, "expectedRevision") ?: return RevisionArg.Absent
        val parsed = parseRevision(raw) ?: return RevisionArg.Malformed
        return RevisionArg.Present(parsed)
    }

    /**
     * Réponse d'outil : texte POUR le modèle, contenu structuré POUR le protocole.
     *
     * Les deux sont requis. Un outil qui déclare un schéma de sortie et ne renvoie
     * que du texte fait échouer le client sur `-32600: has an output schema but did
     * not return structured content` — constaté en production le 2026-09-13, juste
     * après la bascule : le service répondait, mais l'appel d'outil était refusé.
     *
     * Tous les outils passent par ici, donc la correction est unique et ne peut pas
     * être oubliée sur un outil ajouté plus tard.
     */
    internal fun text(body: String): CallToolResult {
        val structured = try {
            kotlinx.serialization.json.Json.parseToJsonElement(body)
                as? kotlinx.serialization.json.JsonObject
        } catch (_: kotlinx.serialization.SerializationException) {
            null
        }
        // Le SDK déclare un schéma de sortie qui EXIGE une propriété `result` :
        // le contenu structuré doit être encapsulé, pas posé à plat. Sans cette
        // enveloppe, le client refuse sur `-32602: data must have required
        // property 'result'` — constaté en production juste après la bascule.
        val wrapped = structured?.let {
            kotlinx.serialization.json.buildJsonObject { put("result", it) }
        }

        return CallToolResult(
            content = listOf(TextContent(text = body)),
            structuredContent = wrapped,
        )
    }
}
