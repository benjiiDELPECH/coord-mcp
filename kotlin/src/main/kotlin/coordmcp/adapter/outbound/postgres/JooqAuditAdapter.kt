package coordmcp.adapter.outbound.postgres

import coordmcp.application.port.AuditQueryPort
import coordmcp.db.jooq.tables.AuditLog.AUDIT_LOG
import coordmcp.domain.AuditEntry

/**
 * Adaptateur de la piste d'audit.
 *
 * La piste est un journal de faits passés : lecture seule, par construction. Un
 * adaptateur qui n'expose aucune écriture rend la chose évidente plutôt que
 * conventionnelle.
 */
public class JooqAuditAdapter(private val db: JooqDatabase) : AuditQueryPort {

    private val dsl = db.dsl

    /**
     * Du plus récent au plus ancien. La borne est un INVARIANT d'interface, pas
     * une politesse : un `limit` arbitraire venu d'un agent ne doit pas pouvoir
     * tirer 5662 lignes dans le contexte d'un modèle. On clampe plutôt que de
     * refuser — demander 100 000 lignes est une maladresse, pas une attaque.
     */
    override fun tail(limit: Int): List<AuditEntry> {
        val bounded = limit.coerceIn(1, MAX_AUDIT_TAIL)
        return dsl.selectFrom(AUDIT_LOG)
            // `id` départage deux entrées de même horodatage : sans lui, l'ordre
            // n'est pas déterministe et un test devient instable.
            .orderBy(AUDIT_LOG.TIMESTAMP.desc(), AUDIT_LOG.ID.desc())
            .limit(bounded)
            .fetch()
            .map {
                AuditEntry(
                    id = it.id,
                    timestamp = it.timestamp.toInstant(),
                    tool = it.tool,
                    agentId = it.agentId,
                    workItemId = it.workItemId,
                    argsJson = it.argsJson,
                )
            }
    }

    private companion object {
        /** Borne dure : la piste ne doit pas noyer le contexte d'un agent. */
        const val MAX_AUDIT_TAIL = 500
    }
}
