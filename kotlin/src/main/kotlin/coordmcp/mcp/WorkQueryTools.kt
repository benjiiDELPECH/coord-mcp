package coordmcp.mcp

import coordmcp.application.port.WorkItemReader
import coordmcp.domain.WavePlanner
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.ToolSchema
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Outils de LECTURE des travaux.
 *
 * Une classe par responsabilité : mélanger lectures, commandes et registres
 * annexes dans un seul adaptateur le faisait dépasser le seuil de fonctions —
 * symptôme d'un vrai défaut de cohésion, pas d'une règle tatillonne.
 */
internal class WorkQueryTools(private val reader: WorkItemReader) {

    fun register(mcp: Server) {
        registerCount(mcp)
        registerGet(mcp)
        registerListActive(mcp)
        registerPlanWaves(mcp)
    }

    private fun registerPlanWaves(mcp: Server) {
        mcp.addTool(
            name = "plan_parallel_waves",
            description = "Partitionne les travaux actifs en vagues sans conflit de fichiers : " +
                "deux travaux d'une même vague ne se disputent aucun chemin. " +
                "Déterministe — deux appels sur le même état rendent le même plan.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("repo_path") {
                        put("type", "string")
                        put("description", "Chemin absolu du dépôt. Omis => tous les travaux actifs.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val repoPath = McpTooling.arg(request.arguments, "repo_path")
            val active = reader.active().filter { repoPath == null || it.repo.raw == repoPath }
            McpTooling.text(RegistryToolJson.waves(WavePlanner.plan(active)))
        }
    }

    private fun registerCount(mcp: Server) {
        mcp.addTool(
            name = "work_count",
            description = "Nombre de work items présents dans le registre.",
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { _ ->
            McpTooling.text(WorkToolJson.count(reader.count()))
        }
    }

    private fun registerGet(mcp: Server) {
        mcp.addTool(
            name = "get_work",
            description = "Lit un work item par identifiant, avec sa révision courante.",
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("work_item_id") {
                        put("type", "string")
                        put("description", "Identifiant, ex. wi_41bcdcd68e07")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val idArg = McpTooling.workItemIdArg(request.arguments)
            McpTooling.workItemIdError(idArg)?.let { motif ->
                return@addTool McpTooling.text(WorkToolJson.invalidArgument(motif))
            }
            val raw = McpTooling.workItemIdRaw(idArg)
            val parsed = McpTooling.parseId(raw)
            McpTooling.text(
                when {
                    parsed == null -> WorkToolJson.invalidArgument("work_item_id illisible : '$raw'")
                    else -> reader.findById(parsed)?.let(WorkToolJson::item)
                        ?: WorkToolJson.notFound(raw)
                },
            )
        }
    }

    private fun registerListActive(mcp: Server) {
        mcp.addTool(
            name = "list_active_work",
            description = "Travaux non terminaux (declared, claimed, checked_out), " +
                "du plus ancien au plus récent.",
            // `repo_path` DÉCLARÉ. Sans lui, un filtre explicitement demandé
            // disparaissait en silence et l'outil rendait tous les dépôts : la
            // réponse restait bien formée, donc rien ne signalait l'erreur.
            // C'est la régression la plus dangereuse des trois, parce qu'elle
            // ne plante pas. Le défaut « tous dépôts » est légitime — c'est le
            // contrat Python — mais il doit être un CHOIX, pas un oubli.
            inputSchema = ToolSchema(
                properties = buildJsonObject {
                    putJsonObject("repo_path") {
                        put("type", "string")
                        put("description", "Chemin absolu du dépôt. Omis => tous les travaux actifs.")
                    }
                },
            ),
            // Schéma de sortie NEUTRALISÉ : le SDK en déclare un par défaut
            // qui exige {"result": [...]}. Nos charges utiles sont des objets,
            // donc le client les refusait (-32602) après la bascule.
            outputSchema = null,
        ) { request ->
            val repoPath = McpTooling.arg(request.arguments, "repo_path")
            val active = reader.active().filter { repoPath == null || it.repo.raw == repoPath }
            McpTooling.text(WorkToolJson.activeItems(active))
        }
    }
}
