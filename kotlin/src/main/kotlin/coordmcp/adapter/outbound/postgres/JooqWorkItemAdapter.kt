package coordmcp.adapter.outbound.postgres

import coordmcp.application.port.NewWorkItem
import coordmcp.application.port.WorkItemReader
import coordmcp.application.port.WorkItemWriter
import coordmcp.db.jooq.tables.WorkItems.WORK_ITEMS
import coordmcp.db.jooq.tables.records.WorkItemsRecord
import coordmcp.domain.DomainResult
import coordmcp.domain.Repo
import coordmcp.domain.Revision
import coordmcp.domain.ScopeFiles
import coordmcp.domain.WorkItem
import coordmcp.domain.WorkItemDraft
import coordmcp.domain.WorkItemId
import coordmcp.domain.WorkStatus
import coordmcp.domain.WriteOutcome
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import org.jooq.UpdateSetMoreStep
import org.jooq.impl.DSL

/**
 * Adaptateur PostgreSQL des TRAVAUX — lecture et écriture.
 *
 * Il implémente les ports, il ne les définit pas. Le SQL vient de jOOQ, généré
 * depuis la base RÉELLE : une colonne renommée casse la compilation, pas la
 * production.
 *
 * Toute mutation passe par UNE seule fonction gardée ([guardedUpdate]) : dupliquer
 * le compare-and-swap à chaque nouvelle mutation finirait par en oublier une —
 * et une mutation sans garde est précisément le bug qu'on a corrigé.
 */
public class JooqWorkItemAdapter(private val db: JooqDatabase) : WorkItemReader, WorkItemWriter {

    private val dsl = db.dsl

    override fun findById(id: WorkItemId): WorkItem? =
        dsl.selectFrom(WORK_ITEMS).where(WORK_ITEMS.ID.eq(id.value)).fetchOne()?.let(JooqWorkItemMapper::toDomain)

    override fun count(): Long =
        dsl.selectCount().from(WORK_ITEMS).fetchOne()?.value1()?.toLong() ?: 0L

    override fun active(): List<WorkItem> =
        dsl.selectFrom(WORK_ITEMS)
            .where(WORK_ITEMS.STATUS.`in`(ACTIVE_STATUSES))
            // DESC, comme le service Python : la parité d'ORDRE compte, un agent qui
            // lit « les plus récents d'abord » prendrait le mauvais item sinon.
            .orderBy(WORK_ITEMS.CREATED_AT.desc(), WORK_ITEMS.ID.desc())
            .fetch()
            .map(JooqWorkItemMapper::toDomain)

    override fun countCiActive(): Int =
        dsl.selectCount().from(WORK_ITEMS)
            .where(WORK_ITEMS.STATUS.eq(WorkStatus.CHECKED_OUT.storageValue))
            .and(WORK_ITEMS.TRIGGERS_CI.isTrue)
            .fetchOne()?.value1() ?: 0

    /**
     * Création d'un travail en état `declared`.
     *
     * Le scope est sérialisé en JSON, comme le service Python : les deux doivent
     * écrire la MÊME représentation, sinon la lecture de l'un deviendrait du CSV
     * pour l'autre — on a déjà vu cette divergence dans les données migrées.
     */
    override fun create(item: NewWorkItem) {
        val stamp = OffsetDateTime.ofInstant(item.at, ZoneOffset.UTC)
        dsl.insertInto(WORK_ITEMS)
            .columns(
                WORK_ITEMS.ID,
                WORK_ITEMS.REPO,
                WORK_ITEMS.TITLE,
                WORK_ITEMS.SCOPE_FILES,
                WORK_ITEMS.GITHUB_ISSUE_NUMBER,
                WORK_ITEMS.SCOPE_SYMBOLS,
                WORK_ITEMS.SCOPE_SYMBOLS_EXPANDED,
                WORK_ITEMS.SCOPE_RESOURCES,
                WORK_ITEMS.STATUS,
                WORK_ITEMS.AGENT_ID,
                WORK_ITEMS.TRIGGERS_CI,
                WORK_ITEMS.CREATED_AT,
                WORK_ITEMS.UPDATED_AT,
                WORK_ITEMS.REVISION,
            )
            .values(
                item.id.value,
                item.repo.raw,
                item.title,
                JooqWorkItemMapper.toJsonScope(item.scope),
                item.issueNumber,
                JooqWorkItemMapper.toJsonList(item.symbols),
                JooqWorkItemMapper.toJsonList(item.expandedFiles),
                JooqWorkItemMapper.toJsonList(item.resources),
                WorkStatus.DECLARED.storageValue,
                item.agentId,
                item.triggersCi,
                stamp,
                stamp,
                1,
            )
            .execute()
    }

    override fun applyTransition(
        id: WorkItemId,
        next: WorkStatus,
        outcome: String?,
        at: Instant,
        expected: Revision?,
    ): WriteOutcome = guardedUpdate(id, at, expected) { update ->
        update.set(WORK_ITEMS.STATUS, next.storageValue).set(WORK_ITEMS.OUTCOME, outcome)
    }

    override fun linkIssue(
        id: WorkItemId,
        issueNumber: Int,
        at: Instant,
        expected: Revision?,
    ): WriteOutcome = guardedUpdate(id, at, expected) { update ->
        update.set(WORK_ITEMS.GITHUB_ISSUE_NUMBER, issueNumber)
    }

    /**
     * Compare-and-swap en UNE instruction : `UPDATE … WHERE id = ? AND revision = ?
     * RETURNING revision`. Si la garde ne passe pas, aucune ligne ne revient —
     * c'est le signal de conflit. Un SELECT de relecture ne serait pas
     * équivalent : il pourrait observer la révision d'un AUTRE écrivain.
     */
    private fun guardedUpdate(
        id: WorkItemId,
        at: Instant,
        expected: Revision?,
        extraSets: (UpdateSetMoreStep<WorkItemsRecord>) -> UpdateSetMoreStep<WorkItemsRecord>,
    ): WriteOutcome {
        val base = dsl.update(WORK_ITEMS)
            .set(WORK_ITEMS.UPDATED_AT, OffsetDateTime.ofInstant(at, ZoneOffset.UTC))
            // `plus(DSL.inline(1))` : l'incrément reste une expression SQL calculée
            // par la base, pas une valeur lue puis réécrite — ce qui rouvrirait la
            // course que la garde ferme.
            .set(WORK_ITEMS.REVISION, WORK_ITEMS.REVISION.plus(DSL.inline(1)))

        val guarded = extraSets(base).let { update ->
            if (expected == null) {
                update.where(WORK_ITEMS.ID.eq(id.value))
            } else {
                // `toInt()` : le domaine porte une Revision en Long, la colonne est
                // un INTEGER PostgreSQL. Détail d'adaptateur, pas de domaine.
                update.where(
                    WORK_ITEMS.ID.eq(id.value).and(WORK_ITEMS.REVISION.eq(expected.value.toInt())),
                )
            }
        }

        val newRevision = guarded.returning(WORK_ITEMS.REVISION).fetchOne()?.get(WORK_ITEMS.REVISION)
            ?: return conflictOrNotFound(id, expected)

        return WriteOutcome.Applied(JooqWorkItemMapper.revisionOf(newRevision))
    }

    private fun conflictOrNotFound(id: WorkItemId, expected: Revision?): WriteOutcome {
        val current = dsl.select(WORK_ITEMS.REVISION)
            .from(WORK_ITEMS)
            .where(WORK_ITEMS.ID.eq(id.value))
            .fetchOne(WORK_ITEMS.REVISION)

        return when {
            current == null -> WriteOutcome.NotFound
            // Écriture sans garde : seule l'absence explique zéro ligne.
            expected == null -> WriteOutcome.NotFound
            else -> WriteOutcome.StaleRevision(expected, JooqWorkItemMapper.revisionOf(current))
        }
    }

    private companion object {
        /** Statuts non terminaux — même définition que le service Python. */
        val ACTIVE_STATUSES = listOf("declared", "claimed", "in_progress", "checked_out")
    }
}
