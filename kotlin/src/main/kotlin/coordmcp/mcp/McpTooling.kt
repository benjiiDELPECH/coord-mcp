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

    /**
     * Identifiant de work item — résolu sous DEUX noms, sans jamais deviner.
     *
     * Le contrat Python nomme ce paramètre `work_item_id` sur les quatre outils
     * concernés (`get_work`, `release_work`, `abandon_work`, `relink_issue`).
     * Le portage Kotlin a introduit `id` dans un sous-ensemble d'entre eux et
     * gardé `work_item_id` dans les autres : deux vocabulaires pour une même
     * chose. Conséquence mesurée : `get_work` et `release_work` rendaient
     * « id manquant ou invalide » à des clients qui envoyaient pourtant la
     * valeur, sous son nom canonique.
     *
     * `id` reste donc accepté comme alias DÉPRÉCIÉ — mais le cas où les deux
     * noms portent des valeurs DIFFÉRENTES est une erreur, pas un arbitrage.
     * Un `?:` bien intentionné choisirait l'un des deux en silence, et
     * l'opération s'appliquerait au mauvais work item sans que rien ne le dise.
     *
     * Ajouté À CÔTÉ de `arg`, jamais dedans : `arg` a 13 appelants directs et
     * un impact CRITICAL (gitnexus). Le modifier exposerait des outils pour qui
     * `id` n'a pas ce sens.
     */
    internal sealed interface WorkItemIdArg {
        data object Absent : WorkItemIdArg
        data class Present(val raw: String) : WorkItemIdArg
        data class Ambiguous(val canonical: String, val alias: String) : WorkItemIdArg
    }

    internal fun workItemIdArg(arguments: Map<String, JsonElement>?): WorkItemIdArg {
        val canonical = arg(arguments, "work_item_id")
        val alias = arg(arguments, "id")
        return when {
            canonical != null && alias != null && canonical != alias ->
                WorkItemIdArg.Ambiguous(canonical, alias)
            canonical != null -> WorkItemIdArg.Present(canonical)
            alias != null -> WorkItemIdArg.Present(alias)
            else -> WorkItemIdArg.Absent
        }
    }

    /** Le motif de refus, ou `null` si l'identifiant est exploitable. */
    internal fun workItemIdError(arg: WorkItemIdArg): String? = when (arg) {
        WorkItemIdArg.Absent -> "work_item_id manquant"
        is WorkItemIdArg.Ambiguous ->
            "work_item_id et id portent des valeurs differentes " +
                "(${arg.canonical} / ${arg.alias}) : ambigu, refuse"
        is WorkItemIdArg.Present -> null
    }

    internal fun workItemIdRaw(arg: WorkItemIdArg): String =
        (arg as? WorkItemIdArg.Present)?.raw.orEmpty()

    /**
     * Un argument TEXTE porté par deux noms — même discipline que l'identifiant.
     *
     * `release_work` prend `outcome` selon le contrat Python, mais le domaine
     * interne l'appelle `lesson`. Le portage a exposé le nom INTERNE dans le
     * schéma, si bien qu'un client envoyant `outcome` — le seul nom qu'il
     * connaisse — recevait `REFUSED: MissingLesson`, c'est-à-dire un refus
     * d'argument déguisé en refus métier. Deux noms, une chose, et le cas où
     * les deux diffèrent reste une erreur.
     */
    internal sealed interface TextArg {
        data object Absent : TextArg
        data class Present(val value: String) : TextArg
        data class Ambiguous(val canonical: String, val alias: String) : TextArg
    }

    internal fun aliasedTextArg(
        arguments: Map<String, JsonElement>?,
        canonical: String,
        alias: String,
    ): TextArg {
        val c = arg(arguments, canonical)
        val a = arg(arguments, alias)
        return when {
            c != null && a != null && c != a -> TextArg.Ambiguous(c, a)
            c != null -> TextArg.Present(c)
            a != null -> TextArg.Present(a)
            else -> TextArg.Absent
        }
    }

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
        // TOUT type JSON, pas seulement un objet. Le cast en `JsonObject`
        // rendait `null` sur un TABLEAU, donc aucun contenu structuré n'était
        // émis, et le client refusait sur `-32600: has an output schema but did
        // not return structured content`. Or le contrat Python rend justement un
        // tableau pour `list_active_work` (`-> list[dict[str, Any]]`) : la forme
        // correcte de la charge utile était précisément celle que ce cast
        // rejetait. Un `as? JsonObject` bien intentionné verrouillait le bug.
        val structured = try {
            kotlinx.serialization.json.Json.parseToJsonElement(body)
        } catch (_: kotlinx.serialization.SerializationException) {
            null
        }
        // Le SDK déclare un schéma de sortie qui EXIGE une propriété `result` :
        // le contenu structuré doit être encapsulé, pas posé à plat. Sans cette
        // enveloppe, le client refuse sur `-32602: data must have required
        // property 'result'` — constaté en production juste après la bascule.
        // L'enveloppe vaut pour toute charge utile, tableau compris.
        val wrapped = structured?.let {
            kotlinx.serialization.json.buildJsonObject { put("result", it) }
        }

        return CallToolResult(
            content = listOf(TextContent(text = body)),
            structuredContent = wrapped,
        )
    }
}
