package coordmcp.mcp

import coordmcp.application.CheckinResult
import coordmcp.application.ScopeResolution
import coordmcp.application.CheckoutResult
import coordmcp.domain.AdrAllocation
import coordmcp.domain.CiGateDecision
import coordmcp.domain.AuditEntry
import coordmcp.domain.WorkItemId
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonArray
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Sérialisation des REGISTRES ANNEXES — audit, allocations, planification.
 *
 * Séparée de `WorkToolJson` : un objet qui sérialise à la fois l'état des
 * travaux et les registres finit par dépasser toute limite de cohésion — et
 * c'est le symptôme, pas la règle, qui l'a signalé.
 */
internal object RegistryToolJson {

    /** Vagues sans conflit : chaque vague peut tourner en parallèle en interne. */
    internal fun waves(waves: List<List<WorkItemId>>): String = jsonOf {
        put("wave_count", waves.size)
        put("waves", buildJsonArray {
            waves.forEach { wave ->
                addJsonArray { wave.forEach { id -> add(id.value) } }
            }
        })
    }

    internal fun auditEntries(entries: List<AuditEntry>): String = jsonOf {
        put("count", entries.size)
        put("entries", buildJsonArray {
            entries.forEach { entry ->
                addJsonObject {
                    put("id", entry.id)
                    put("timestamp", entry.timestamp.toString())
                    put("tool", entry.tool)
                    put("agent_id", entry.agentId)
                    put("work_item_id", entry.workItemId)
                }
            }
        })
    }

    internal fun allocations(allocations: List<AdrAllocation>): String = jsonOf {
        put("count", allocations.size)
        put("allocations", buildJsonArray {
            allocations.forEach { allocation ->
                addJsonObject {
                    put("adr_number", allocation.adrNumber)
                    put("repo_path", allocation.repoPath)
                    put("topic_slug", allocation.topicSlug)
                    put("filename", allocation.filename)
                    put("allocated_to", allocation.allocatedTo)
                    put("allocated_at", allocation.allocatedAt.toString())
                }
            }
        })
    }

    /**
     * Résultat de `checkin`. Les conflits et la barrière CI sont des CONSTATS
     * rendus tels quels : un agent doit pouvoir décider, pas recevoir un verdict
     * déjà rendu à sa place.
     */
    internal fun checkinCreated(result: CheckinResult.Created): String = jsonOf {
        put("work_item_id", result.workItemId.value)
        put("suggested_action", result.suggestedAction)
        put("similar_open_issues", result.similarOpenIssues)
        put("prior_decisions_checked", result.priorDecisionsChecked)
        put("triggers_ci", result.triggersCi)
        putJsonObject("scope_resolution") {
            when (val resolution = result.scopeResolution) {
                is ScopeResolution.Resolved -> {
                    put("status", "resolved")
                    put("expanded_files", resolution.expandedFiles)
                    put("unresolved_symbols", buildJsonArray {
                        resolution.unresolvedSymbols.forEach { add(it) }
                    })
                }
                is ScopeResolution.Unavailable -> {
                    put("status", "unavailable")
                    put("reason", resolution.reason)
                }
            }
        }
        put("conflicts", buildJsonArray {
            result.conflicts.forEach { conflict ->
                addJsonObject {
                    put("work_item_id", conflict.workItemId.value)
                    put("reason", conflict.reason.name)
                    put("shared", buildJsonArray { conflict.shared.forEach { add(it) } })
                }
            }
        })
        putJsonObject("ci_gate") {
            put(
                "you_should",
                when (result.ciGate) {
                    is CiGateDecision.Proceed -> "PROCEED"
                    is CiGateDecision.Wait -> "WAIT"
                },
            )
            put("active_ci", result.ciGate.activeCi)
            put("limit", result.ciGate.limit)
        }
    }

    internal fun checkoutResult(result: CheckoutResult): String = when (result) {
        is CheckoutResult.CheckedOut -> jsonOf {
            put("work_status", "checked_out")
            put("revision", result.revision.value)
            putCiGate(result.ciGate)
        }
        is CheckoutResult.Blocked -> jsonOf {
            put("blocked", true)
            put("blockers", buildJsonArray { result.blockers.forEach { add(it) } })
            putCiGate(result.ciGate)
        }
        is CheckoutResult.Refused -> jsonOf {
            put("error", "REFUSED")
            put("detail", result.refusals.joinToString())
        }
        is CheckoutResult.Stale -> jsonOf {
            put("error", "STALE_REVISION")
            put("expected_revision", result.expected.value)
            put("current_revision", result.current.value)
        }
        CheckoutResult.NotFound -> jsonOf { put("error", "NOT_FOUND") }
    }

    private fun kotlinx.serialization.json.JsonObjectBuilder.putCiGate(gate: CiGateDecision) {
        putJsonObject("ci_gate") {
            put("you_should", if (gate is CiGateDecision.Wait) "WAIT" else "PROCEED")
            put("active_ci", gate.activeCi)
            put("limit", gate.limit)
        }
    }

    internal fun adrAllocated(number: Int, filename: String): String = jsonOf {
        put("adr_number", number)
        put("filename", filename)
    }

    /** Toutes les tentatives ont échoué : on le dit, on ne prétend pas avoir alloué. */
    internal fun adrContended(attempts: Int): String = jsonOf {
        put("error", "CONTENDED")
        put("attempts", attempts)
        put("detail", "aucun numéro n'a été réservé — relancer l'allocation")
    }

    private fun jsonOf(build: JsonObjectBuilder.() -> Unit): String =
        buildJsonObject(build).toString()
}
