package coordmcp.adapter.outbound.postgres

import coordmcp.application.port.AdrAllocationPort
import coordmcp.application.port.AdrAllocationQueryPort
import coordmcp.application.port.AdrClaim
import coordmcp.db.jooq.tables.AdrAllocations.ADR_ALLOCATIONS
import coordmcp.domain.AdrAllocation
import coordmcp.domain.AdrNumbering
import java.nio.file.Files
import java.nio.file.Path
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.stream.Collectors
import kotlin.streams.toList

/**
 * Adaptateur des allocations de numéros d'ADR — lecture ET réservation.
 *
 * 147 lignes migrées qui enregistrent les numéros déjà pris. Les perdre, c'est
 * réautoriser des numéros déjà alloués — exactement la collision que coord-mcp
 * existe pour empêcher.
 *
 * L'atomicité vient de `UNIQUE (repo_path, adr_number)` et d'un
 * `INSERT … ON CONFLICT DO NOTHING` en boucle : la contrainte décide, pas une
 * lecture-puis-écriture applicative qui laisserait ouverte la fenêtre de course
 * qu'on veut fermer.
 */
public class JooqAdrAllocationAdapter(private val db: JooqDatabase) :
    AdrAllocationQueryPort,
    AdrAllocationPort {

    private val dsl = db.dsl

    override fun allocations(repoPath: String?): List<AdrAllocation> {
        val base = dsl.selectFrom(ADR_ALLOCATIONS)
        val filtered = if (repoPath == null) base else base.where(ADR_ALLOCATIONS.REPO_PATH.eq(repoPath))

        return filtered
            .orderBy(ADR_ALLOCATIONS.REPO_PATH, ADR_ALLOCATIONS.ADR_NUMBER)
            .fetch()
            .map {
                AdrAllocation(
                    adrNumber = it.adrNumber,
                    repoPath = it.repoPath,
                    topicSlug = it.topicSlug,
                    filename = it.filename,
                    allocatedTo = it.allocatedTo,
                    allocatedAt = it.allocatedAt.toInstant(),
                )
            }
    }

    override fun claim(
        repoPath: String,
        topicSlug: String,
        allocatedTo: String?,
        at: Instant,
    ): AdrClaim {
        // Le slug est validé AVANT toute écriture : un slug hors convention
        // produirait un fichier que le scan suivant ne reconnaîtrait pas, donc un
        // numéro considéré comme libre et réalloué plus tard.
        if (!VALID_SLUG.matches(topicSlug)) {
            return AdrClaim.Rejected(
                "slug invalide '$topicSlug' — attendu [$VALID_SLUG_PATTERN] " +
                    "(utiliser AdrNumbering.slugify sur le sujet)",
            )
        }

        val maxOnDisk = maxOnDisk(repoPath)

        repeat(MAX_ATTEMPTS) {
            val maxInDatabase = maxInDatabase(repoPath)
            val candidate = AdrNumbering.nextCandidate(maxOnDisk, maxInDatabase)
            val filename = AdrNumbering.filename(candidate, topicSlug)

            val inserted = dsl.insertInto(ADR_ALLOCATIONS)
                .columns(
                    ADR_ALLOCATIONS.REPO_PATH,
                    ADR_ALLOCATIONS.ADR_NUMBER,
                    ADR_ALLOCATIONS.TOPIC_SLUG,
                    ADR_ALLOCATIONS.FILENAME,
                    ADR_ALLOCATIONS.ALLOCATED_TO,
                    ADR_ALLOCATIONS.ALLOCATED_AT,
                )
                .values(
                    repoPath,
                    candidate,
                    topicSlug,
                    filename,
                    allocatedTo,
                    OffsetDateTime.ofInstant(at, ZoneOffset.UTC),
                )
                .onConflict().doNothing()
                .execute()

            if (inserted == 1) return AdrClaim.Allocated(candidate, filename)
            // Zéro ligne = un autre agent a pris ce numéro entre notre calcul et
            // l'insertion. On recalcule : c'est la contrainte qui a arbitré, pas nous.
        }
        return AdrClaim.Contended(MAX_ATTEMPTS)
    }

    /**
     * Numéro le plus haut parmi les fichiers `ADR-NNN-*.md` du dépôt.
     *
     * Le disque compte autant que la base : un ADR écrit à la main n'est pas dans
     * `adr_allocations`, et l'ignorer produirait une collision de FICHIER — le
     * numéro serait « libre » en base et déjà pris sur le disque.
     */
    private fun maxOnDisk(repoPath: String): Int {
        val adrDir = Path.of(repoPath, "docs", "adr")
        if (!Files.isDirectory(adrDir)) return 0
        return Files.list(adrDir).use { stream ->
            stream.map { AdrNumbering.numberFromFilename(it.fileName.toString()) }
                .collect(Collectors.toList())
                .maxOrNull() ?: 0
        }
    }

    private fun maxInDatabase(repoPath: String): Int =
        dsl.select(org.jooq.impl.DSL.max(ADR_ALLOCATIONS.ADR_NUMBER))
            .from(ADR_ALLOCATIONS)
            .where(ADR_ALLOCATIONS.REPO_PATH.eq(repoPath))
            .fetchOne(0, Int::class.java) ?: 0

    private companion object {
        /** Même nombre de tentatives que le service Python, pour un comportement identique. */
        const val MAX_ATTEMPTS = 5

        const val VALID_SLUG_PATTERN = "a-z0-9 séparés par des tirets"

        /** `untitled` est le repli de `slugify` : il doit rester acceptable. */
        val VALID_SLUG = Regex("^(untitled|[a-z0-9]+(-[a-z0-9]+)*)$")
    }
}
