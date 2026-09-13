package coordmcp.adapter.outbound.postgres

import coordmcp.db.jooq.tables.records.WorkItemsRecord
import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemDraft
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus

/**
 * Traduction ligne SQL -> agrégat, séparée de l'accès aux données.
 *
 * Mélanger les deux faisait grossir l'adaptateur à chaque colonne ajoutée : la
 * persistance décide QUOI lire et écrire, le mapping décide COMMENT une ligne
 * devient un objet du domaine. Deux responsabilités, deux fichiers.
 */
internal object JooqWorkItemMapper {

    fun toDomain(record: WorkItemsRecord): WorkItem {
        val id = parse("WorkItemId") { WorkItemId.of(record.id) }
        val repo = parse("repo") { Repo.of(record.repo) }
        val status = parse("status") { WorkStatus.parse(record.status) }
        val scope = parse("scope_files") { ScopeFiles.parse(record.scopeFiles) }
        val revision = parse("revision") { Revision.of(record.revision.toLong()) }

        // Horodatages désormais de vrais TIMESTAMPTZ : plus aucun format à
        // deviner, contrairement aux trois conventions qui coexistaient en SQLite.
        val createdAt = record.createdAt.toInstant()
        val updatedAt = record.updatedAt.toInstant()

        return WorkItem.of(
            id = id,
            status = status,
            outcome = record.outcome,
            draft = WorkItemDraft(
            repo,
            record.title,
            scope,
            createdAt,
            updatedAt,
            record.githubIssueNumber,
            stringList(record.scopeSymbols),
            stringList(record.scopeSymbolsExpanded),
            stringList(record.scopeResources),
            record.triggersCi,
        ),
            revision = revision,
        )
    }

    /**
     * Une donnée qui viole un invariant de domaine est une CORRUPTION : on échoue
     * en nommant la colonne. La contourner propagerait l'incohérence dans les
     * compare-and-swap suivants.
     */
    private fun <T> parse(column: String, build: () -> DomainResult<T>): T =
        when (val result = build()) {
            is DomainResult.Ok -> result.value
            is DomainResult.Rejected ->
                error("donnée corrompue en base (colonne $column) : ${result.refusals.joinToString()}")
        }

    /**
     * Les colonnes `scope_*` contiennent des tableaux JSON. Illisible => liste
     * vide, mais la lecture ne peut pas échouer silencieusement ailleurs : c'est
     * le seul endroit où une donnée de coordination est tolérée absente.
     */
    private fun stringList(raw: String?): List<String> {
        if (raw.isNullOrBlank()) return emptyList()
        return Regex("\"((?:[^\"\\\\]|\\\\.)*)\"").findAll(raw)
            .map { it.groupValues[1] }
            .toList()
    }

    internal fun toJsonList(values: List<String>): String =
        values.joinToString(prefix = "[", postfix = "]") { "\"${it.replace("\"", "\\\"")}\"" }

    /** Un tableau JSON, jamais du CSV : le format de sortie est un contrat. */
    internal fun toJsonScope(scope: ScopeFiles): String =
        scope.paths.joinToString(prefix = "[", postfix = "]") { "\"${it.replace("\"", "\\\"")}\"" }

    internal fun revisionOf(raw: Int): Revision = when (val result = Revision.of(raw.toLong())) {
        is DomainResult.Ok -> result.value
        is DomainResult.Rejected -> error("révision corrompue en base : $raw")
    }

}
