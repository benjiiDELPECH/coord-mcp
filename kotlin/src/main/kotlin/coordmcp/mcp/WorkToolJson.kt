package coordmcp.mcp

import coordmcp.domain.WorkItem
import coordmcp.domain.WriteOutcome
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Sérialisation des réponses d'outils — séparée du câblage MCP.
 *
 * Deux responsabilités distinctes : le serveur décide QUEL outil existe et
 * comment il est invoqué ; ce fichier décide de la FORME des réponses. Les
 * mélanger faisait dépasser le seuil de fonctions de la classe serveur, ce qui
 * n'était que le symptôme d'un vrai défaut de cohésion.
 *
 * Les refus sont sérialisés tels quels : `STALE_REVISION`, `REFUSED`,
 * `NOT_FOUND`. Aucun d'eux n'est déguisé en succès.
 */
internal object WorkToolJson {

    internal fun count(total: Long): String = jsonOf { put("count", total) }

    internal fun item(item: WorkItem): String = jsonOf {
        put("id", item.id.value)
        put("status", item.status.storageValue)
        put("title", item.title)
        put("repo", item.repo.raw)
        put("outcome", item.outcome)
        put("revision", item.revision.value)
    }

    internal fun notFound(id: String): String = jsonOf {
        put("error", "NOT_FOUND")
        put("id", id)
    }

    /**
     * Entrée malformée. Distinct de `NOT_FOUND` à dessein : « cette tâche n'existe
     * pas » et « tu m'as envoyé un argument illisible » demandent deux réactions
     * différentes de l'agent.
     */
    internal fun invalidArgument(detail: String): String = jsonOf {
        put("error", "INVALID_ARGUMENT")
        put("detail", detail)
    }

    internal fun refused(detail: String): String = jsonOf {
        put("error", "REFUSED")
        put("detail", detail)
    }

    internal fun outcome(outcome: WriteOutcome): String = when (outcome) {
        is WriteOutcome.Applied -> jsonOf {
            put("work_status", "released")
            put("revision", outcome.revision.value)
        }
        is WriteOutcome.StaleRevision -> jsonOf {
            put("error", "STALE_REVISION")
            put("expected_revision", outcome.expected.value)
            put("current_revision", outcome.current.value)
        }
        WriteOutcome.NotFound -> jsonOf { put("error", "NOT_FOUND") }
    }

    /** Liste des travaux non terminaux — la vue que les agents consultent le plus. */
    internal fun activeItems(items: List<WorkItem>): String = jsonOf {
        put("count", items.size)
        put("items", buildJsonArray {
            items.forEach { item ->
                addJsonObject {
                    put("id", item.id.value)
                    put("status", item.status.storageValue)
                    put("title", item.title)
                    put("repo", item.repo.raw)
                    put("revision", item.revision.value)
                }
            }
        })
    }

    private fun jsonOf(build: JsonObjectBuilder.() -> Unit): String =
        buildJsonObject(build).toString()
}
