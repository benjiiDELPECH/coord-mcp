package coordmcp.application

import coordmcp.application.port.IssueSearchOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewWorkItem
import coordmcp.application.port.PriorDecisionsOutcome
import coordmcp.application.port.PriorDecisionsPort
import coordmcp.application.port.ScopeExpansion
import coordmcp.application.port.ScopeResolverPort
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.domain.CandidateScope
import coordmcp.domain.CiGate
import coordmcp.domain.CiGateDecision
import coordmcp.domain.CiTrigger
import coordmcp.domain.Conflict
import coordmcp.domain.ConflictDetector
import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.SuggestedAction
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItemId
import java.time.Instant

public data class CheckinCommand(
    val repoPath: String,
    val title: String,
    val scopeFiles: List<String>,
    val scopeSymbols: List<String>,
    val agentId: String?,
    /** `null` => détecté par règle depuis le périmètre. */
    val triggersCi: Boolean?,
)

/** Ce que le résolveur de code a pu faire. Un état, pas un silence. */
public sealed interface ScopeResolution {
    public data class Resolved(
        val expandedFiles: Int,
        val unresolvedSymbols: List<String>,
    ) : ScopeResolution

    /** Rien n'a été vérifié : les conflits rendus ne portent que sur les chemins DÉCLARÉS. */
    public data class Unavailable(val reason: String) : ScopeResolution
}

public sealed interface CheckinResult {
    public data class Created(
        val workItemId: WorkItemId,
        val conflicts: List<Conflict>,
        val suggestedAction: String,
        val ciGate: CiGateDecision,
        val triggersCi: Boolean,
        val scopeResolution: ScopeResolution,
        /** `false` = Graphiti n'a PAS été consulté : on ne prétend pas avoir cherché. */
        val priorDecisionsChecked: Boolean,
        val similarOpenIssues: Int,
    ) : CheckinResult

    public data class Invalid(val detail: String) : CheckinResult
}

/**
 * CAS D'USAGE — déclarer un travail et détecter ce qui bloque.
 *
 * Il n'écrit qu'un item `declared` : les conflits et la barrière CI sont des
 * CONSTATS rendus à l'appelant. Un conflit se signale, il ne se tranche pas —
 * c'est l'agent qui sait s'il a une raison de recouvrir un périmètre déjà pris.
 *
 * Deux natures de conflit sont cherchées : les CHEMINS recouverts, et les
 * SYMBOLES partagés — invisibles aux seuls chemins. Quand le résolveur de code
 * ne répond pas, on le DIT : une liste de conflits vide au motif que le résolveur
 * est muet ferait passer une absence de vérification pour une vérification
 * réussie.
 */
/**
 * Les trois sources de connaissance sollicitées par un `checkin`.
 *
 * Groupées : en arguments positionnels, elles étaient trois ports de types
 * proches — trois occasions d'inverser deux paramètres sans que le compilateur
 * s'en aperçoive.
 */
public data class CheckinKnowledge(
    public val resolver: ScopeResolverPort,
    public val issues: IssueTrackerPort,
    public val priorDecisions: PriorDecisionsPort,
)

public class CheckinUseCase(
    private val reader: WorkItemReader,
    private val writer: WorkItemWriter,
    private val knowledge: CheckinKnowledge,
    private val ciLimit: Int = CiGate.DEFAULT_LIMIT,
    private val clock: () -> Instant = Instant::now,
) {

    public fun checkin(command: CheckinCommand): CheckinResult {
        val repo = when (val r = Repo.of(command.repoPath)) {
            is DomainResult.Ok -> r.value
            is DomainResult.Rejected -> return CheckinResult.Invalid("repo_path invalide")
        }
        if (command.title.isBlank()) {
            return CheckinResult.Invalid("title est requis")
        }

        val paths = command.scopeFiles.map { it.trim() }.filter { it.isNotEmpty() }
        val symbols = command.scopeSymbols.map { it.trim() }.filter { it.isNotEmpty() }

        val scope = when (val r = ScopeFiles.parse(jsonArrayOf(paths))) {
            is DomainResult.Ok -> r.value
            is DomainResult.Rejected -> return CheckinResult.Invalid("scope_files invalide")
        }

        // Détection par règle quand l'appelant ne se prononce pas : un agent qui
        // oublie de déclarer une CI lourde fait tomber le lanceur partagé.
        val triggersCi = command.triggersCi ?: CiTrigger.detect(paths)

        val expansion = knowledge.resolver.expand(command.repoPath, symbols)
        val expandedFiles = (expansion as? ScopeExpansion.Resolved)?.files.orEmpty()
        val candidate = CandidateScope(
            paths = (paths + expandedFiles).toSet(),
            symbols = symbols.toSet(),
        )

        val conflicts = ConflictDetector.detectAll(candidate, reader.active())

        val recall = recall(command)
        val activeCi = reader.countCiActive()
        val ciGate = if (triggersCi) CiGate.evaluate(activeCi, ciLimit) else CiGateDecision.Proceed(activeCi, ciLimit)

        val id = WorkItemId.generate()
        writer.create(
            NewWorkItem(
                id = id,
                repo = repo,
                title = command.title,
                scope = scope,
                symbols = symbols,
                expandedFiles = expandedFiles.toList().sorted(),
                agentId = command.agentId,
                triggersCi = triggersCi,
                at = clock(),
            ),
        )

        return CheckinResult.Created(
            workItemId = id,
            conflicts = conflicts,
            // Libellés COPIÉS du service Python, au caractère près : ce sont des
            // chaînes que des agents peuvent comparer, pas des codes internes.
            // Un remplacement à l'identique se joue sur ce genre de détail.
            // `priorDecisions = null` : Graphiti n'est pas encore interrogé par le
            // service Kotlin. Rendre 0 ferait taire la branche REVIEW_PRIOR_DECISIONS
            // en laissant croire qu'on a regardé.
            suggestedAction = SuggestedAction.of(conflicts.size, recall.similarIssues, recall.priorDecisions),
            priorDecisionsChecked = recall.priorDecisions != null,
            similarOpenIssues = recall.similarIssues,
            ciGate = ciGate,
            triggersCi = triggersCi,
            scopeResolution = when (expansion) {
                is ScopeExpansion.Resolved -> ScopeResolution.Resolved(
                    expandedFiles = expansion.files.size,
                    unresolvedSymbols = expansion.unresolved.keys.sorted(),
                )
                is ScopeExpansion.Unavailable -> ScopeResolution.Unavailable(expansion.reason)
            },
        )
    }

    /** Ce que la connaissance existante a pu rendre. Aucun zéro ne masque un échec. */
    private data class Recall(val similarIssues: Int, val priorDecisions: Int?)

    private fun recall(command: CheckinCommand): Recall {
        // Un échec du suivi d'issues n'invente pas zéro résultat : il est NOMMÉ à
        // part, et la recommandation reste fondée sur ce qu'on sait.
        val similar = when (val found = knowledge.issues.searchOpenIssues(command.repoPath, command.title)) {
            is IssueSearchOutcome.Found -> found.issues.size
            is IssueSearchOutcome.Unavailable -> 0
        }

        // `null` = NON VÉRIFIÉ. Une mémoire injoignable ne rend pas « aucune
        // décision » : elle rend une absence de vérification, visible dans le JSON.
        val prior = when (val memory = knowledge.priorDecisions.search(command.repoPath, command.title)) {
            is PriorDecisionsOutcome.Found -> memory.count
            is PriorDecisionsOutcome.Unavailable -> null
        }
        return Recall(similar, prior)
    }

    /** Le scope est écrit sous la MÊME forme que celle que le domaine sait relire. */
    private fun jsonArrayOf(paths: List<String>): String =
        paths.joinToString(prefix = "[", postfix = "]") { "\"${it.replace("\"", "\\\"")}\"" }
}
