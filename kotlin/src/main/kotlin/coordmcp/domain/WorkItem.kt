package coordmcp.domain

import java.time.Instant

// ════════════════════════════════════════════════════════════════════════════
// DOMAINE — les invariants de coord-mcp vivent ici.
//
// Invariants VÉRIFIÉS sur les 2090 work items réels le 2026-09-13, pas déduits
// du schéma :
//   - released => outcome non vide          : 0 violation
//   - updated_at >= created_at              : 0 violation
//   - repo et titre non vides               : 0 violation
//   - statut ∈ {declared, claimed, checked_out, released, abandoned}
//   - scope_files : 2089 en JSON, 1 en CSV  : deux formats coexistent
// ════════════════════════════════════════════════════════════════════════════

/**
 * Un refus est un TYPE, pas une phrase interpolée.
 *
 * Une chaîne ne se teste qu'en recopiant du français et dérive dès qu'on
 * reformule le message. `Refusal.MissingLesson` reste vrai quoi qu'il arrive
 * au texte affiché.
 */
public sealed interface Refusal {
    public data class IllegalTransition(
        public val from: WorkStatus,
        public val to: WorkStatus,
    ) : Refusal

    public data class MissingLesson(public val item: String) : Refusal

    public data class MissingReason(public val item: String) : Refusal

    public data class UnknownStatus(public val raw: String) : Refusal

    public data class InvalidField(public val field: String, public val detail: String) : Refusal
}

public sealed interface DomainResult<out T> {
    public data class Ok<T>(public val value: T) : DomainResult<T>
    public data class Rejected(public val refusals: List<Refusal>) : DomainResult<Nothing>
}

private fun reject(vararg refusals: Refusal): DomainResult<Nothing> =
    DomainResult.Rejected(refusals.toList())

private fun invalid(field: String, detail: String): DomainResult<Nothing> =
    reject(Refusal.InvalidField(field, detail))

/**
 * Jeton de concurrence optimiste.
 *
 * Toute mutation l'incrémente. Un appelant qui fournit la révision lue est
 * rejeté si un autre a muté entre-temps — c'est ce qui empêche deux agents de
 * s'écraser en silence (cas réel : deux `release` concurrents, un outcome perdu).
 */
@JvmInline
public value class Revision private constructor(public val value: Long) {
    public companion object {
        public fun of(raw: Long): DomainResult<Revision> =
            if (raw < 1) invalid("revision", "$raw < 1 : une révision est au moins 1")
            else DomainResult.Ok(Revision(raw))
    }
}

/** Issue d'une écriture. Aucun chemin ne renvoie un succès ambigu. */
public sealed interface WriteOutcome {
    /** Mutation appliquée, avec la nouvelle révision. */
    public data class Applied(public val revision: Revision) : WriteOutcome

    /** Révision périmée : RIEN n'a été appliqué. */
    public data class StaleRevision(
        public val expected: Revision,
        public val current: Revision,
    ) : WriteOutcome

    /** L'item n'existe pas. */
    public data object NotFound : WriteOutcome
}

@JvmInline
public value class WorkItemId private constructor(public val value: String) {
    public companion object {
        public fun of(raw: String): DomainResult<WorkItemId> {
            val t = raw.trim()
            return if (t.isEmpty()) invalid("WorkItemId", "vide") else DomainResult.Ok(WorkItemId(t))
        }

        /**
         * Nouvel identifiant : `wi_` + 12 caractères hexadécimaux, même forme que
         * le service Python. L'unicité est garantie par la clé primaire — un
         * doublon ferait échouer l'insertion, pas un écrasement silencieux.
         */
        /** Même longueur que le service Python : `wi_` + 12 caractères hexadécimaux. */
        private const val ID_HEX_LENGTH = 12

        public fun generate(): WorkItemId =
            WorkItemId(
                "wi_" + java.util.UUID.randomUUID().toString().replace("-", "").take(ID_HEX_LENGTH),
            )
    }
}

/**
 * Le dépôt d'un work item.
 *
 * Deux formes coexistent RÉELLEMENT en base : le chemin absolu local (le cas
 * normal) et, pour UNE ligne héritée (`wi_83ED1B31…`), une **URL de remote**.
 * On modélise les deux plutôt que de rejeter la ligne : la rejeter ferait
 * disparaître l'item et effacerait la trace qu'un écrivain a produit cette
 * forme. Un invariant utile dit ce qui EXISTE, il ne fait pas semblant.
 */
public sealed interface Repo {
    public val raw: String

    public data class LocalPath(override val raw: String) : Repo
    public data class RemoteUrl(override val raw: String) : Repo

    public companion object {
        public fun of(raw: String): DomainResult<Repo> {
            val t = raw.trim()
            return when {
                t.isEmpty() -> invalid("repo", "vide")
                t.startsWith("/") -> DomainResult.Ok(LocalPath(t))
                t.startsWith("http://") || t.startsWith("https://") || t.startsWith("git@") ->
                    DomainResult.Ok(RemoteUrl(t))
                else -> invalid("repo", "ni chemin absolu ni URL de remote : '$t'")
            }
        }
    }
}

/**
 * Statuts observés en base. L'énumération ne PORTE PAS la table de transitions :
 * celle-ci appartient à l'agrégat, sinon n'importe quel appelant peut la
 * dupliquer et devenir une seconde source de vérité sur la légalité.
 */
public enum class WorkStatus {
    DECLARED,
    CLAIMED,
    CHECKED_OUT,
    RELEASED,
    ABANDONED,
    ;

    public companion object {
        public fun parse(raw: String): DomainResult<WorkStatus> {
            val t = raw.trim().uppercase()
            return entries.find { it.name == t }
                ?.let { DomainResult.Ok(it) }
                ?: reject(Refusal.UnknownStatus(raw))
        }
    }

    public val isTerminal: Boolean get() = this == RELEASED || this == ABANDONED

    /**
     * Représentation en base. Explicite et symétrique de [parse] : la base stocke
     * `checked_out`, l'énumération s'appelle `CHECKED_OUT`. Laisser cette
     * conversion implicite (un `name` oublié, un `lowercase` ajouté ici et pas
     * là) produirait des statuts illisibles en base sans qu'aucun type ne s'en
     * plaigne.
     */
    public val storageValue: String get() = name.lowercase()
}

/**
 * Table de transitions — VOLONTAIREMENT privée au fichier. La légalité d'un
 * mouvement est une décision de l'agrégat, pas un service public qu'un appelant
 * pourrait pré-vérifier puis contourner.
 */
private fun isLegalMove(from: WorkStatus, to: WorkStatus): Boolean = when (from) {
    WorkStatus.DECLARED -> to == WorkStatus.CLAIMED ||
        to == WorkStatus.CHECKED_OUT ||
        to == WorkStatus.ABANDONED
    WorkStatus.CLAIMED -> to == WorkStatus.CHECKED_OUT || to == WorkStatus.ABANDONED
    WorkStatus.CHECKED_OUT -> to == WorkStatus.RELEASED || to == WorkStatus.ABANDONED
    WorkStatus.RELEASED, WorkStatus.ABANDONED -> false
}

/**
 * Format des fichiers de périmètre. Deux formats coexistent RÉELLEMENT en base :
 * le JSON (2089 lignes) et le CSV hérité (1 ligne). Le choix est explicite et
 * conservé : « normaliser » en silence effacerait la trace d'un écrivain hors
 * convention.
 */
public sealed interface ScopeFiles {
    public val paths: List<String>

    public data class Json(override val paths: List<String>) : ScopeFiles
    public data class Csv(override val paths: List<String>) : ScopeFiles

    public companion object {
        public fun parse(raw: String?): DomainResult<ScopeFiles> {
            val t = raw?.trim().orEmpty()
            if (t.isEmpty()) return DomainResult.Ok(Json(emptyList()))

            if (t.startsWith("[")) {
                return try {
                    DomainResult.Ok(Json(decodeJsonArray(t)))
                } catch (e: IllegalArgumentException) {
                    invalid("scope_files", "annoncé JSON mais illisible : ${e.message}")
                }
            }
            val csv = t.split(',').map { it.trim() }.filter { it.isNotEmpty() }
            return if (csv.isEmpty()) invalid("scope_files", "non vide mais sans entrée")
            else DomainResult.Ok(Csv(csv))
        }

        private fun decodeJsonArray(s: String): List<String> {
            val body = s.removePrefix("[").removeSuffix("]").trim()
            if (body.isEmpty()) return emptyList()
            return Regex("\"((?:[^\"\\\\]|\\\\.)*)\"").findAll(body)
                .map { m -> m.groupValues[1].replace("\\\"", "\"").replace("\\\\", "\\") }
                .toList()
        }
    }
}

/** Les champs descriptifs, groupés. Évite une liste de huit arguments où l'ordre devient implicite. */
public data class WorkItemDraft(
    public val repo: Repo,
    public val title: String,
    public val scope: ScopeFiles,
    public val createdAt: Instant,
    public val updatedAt: Instant,
    /**
     * Symboles déclarés et blast-radius résolu.
     *
     * Deux agents peuvent toucher des FICHIERS différents et le MÊME symbole :
     * sans ces listes, ce conflit est invisible au seul recoupement de chemins.
     * `expanded` est le résultat du résolveur de code ; il peut être vide si le
     * résolveur n'a pas répondu — auquel cas `checkin` le SIGNALE, il ne fait
     * pas comme si l'ensemble était vide par nature.
     */
    /** Numéro d'issue rattaché, s'il existe : sans lui, ni critères ni PR ne sont consultables. */
    public val issueNumber: Int? = null,
    public val symbols: List<String> = emptyList(),
    public val expandedFiles: List<String> = emptyList(),
    /**
     * Ressources d'INFRASTRUCTURE que ce travail occupe : nœud, label K8s,
     * affinité, VM, port, secret, chemin de stockage.
     *
     * Un troisième axe, à côté des fichiers et des symboles, parce qu'il y a un
     * troisième angle mort : deux agents fichiers-disjoints et
     * symboles-disjoints peuvent se télescoper sur le cluster. Aucune
     * comparaison de dépôt ne le voit.
     */
    public val resources: List<String> = emptyList(),
    /**
     * Ce travail consommera-t-il une place dans le lanceur CI partagé ?
     *
     * C'est ce booléen qui décide si la barrière de concurrence s'applique : un
     * travail qui n'entre pas dans le lanceur n'a rien à y attendre.
     */
    public val triggersCi: Boolean = false,
) {
    init {
        require(title.isNotBlank()) { "un work item sans titre n'est pas exploitable" }
        require(!updatedAt.isBefore(createdAt)) {
            "updated_at ($updatedAt) antérieur à created_at ($createdAt)"
        }
    }
}

/**
 * AGRÉGAT — WorkItem.
 *
 * Les mouvements sont des INTENTIONS NOMMÉES, pas un dispatcher générique.
 *
 * La version précédente exposait `transitionTo(next, at, outcome = this.outcome)`.
 * Trois défauts, dont un comportemental :
 *   1. `outcome` n'a de sens que pour une cible sur cinq — argument drapeau ;
 *   2. sa valeur par défaut HÉRITAIT de l'outcome courant, si bien qu'abandonner
 *      un item déjà released lui laissait la leçon du travail abouti : un état
 *      incohérent atteignable par simple omission ;
 *   3. le nom décrivait la mécanique, pas l'acte.
 *
 * Ici `release(lesson, at)` exige la leçon, et `abandon(reason, at)` ne peut pas
 * en hériter une par accident.
 */
public class WorkItem private constructor(
    public val id: WorkItemId,
    public val status: WorkStatus,
    public val outcome: String?,
    public val draft: WorkItemDraft,
    public val revision: Revision,
) {
    public val repo: Repo get() = draft.repo
    public val title: String get() = draft.title
    public val scope: ScopeFiles get() = draft.scope
    public val createdAt: Instant get() = draft.createdAt
    public val updatedAt: Instant get() = draft.updatedAt

    init {
        // Vérifié sur les 2005 items terminaux réels (1364 released + 641
        // abandoned) : TOUS portent un outcome non vide, 0 violation. Un item
        // terminal sans outcome est donc un item muet — on refuse de le
        // construire, y compris à la réhydratation depuis la base.
        if (status.isTerminal) {
            require(!outcome.isNullOrBlank()) {
                "un item ${status.name.lowercase()} (${id.value}) doit porter un outcome : " +
                    "sans lui, la décision est perdue"
            }
        }
    }

    public fun claim(at: Instant): DomainResult<WorkItem> = moveTo(WorkStatus.CLAIMED, at, null)

    public fun checkOut(at: Instant): DomainResult<WorkItem> = moveTo(WorkStatus.CHECKED_OUT, at, null)

    /**
     * Clôture. La leçon est OBLIGATOIRE et passée explicitement : impossible de
     * clôturer en héritant d'un outcome antérieur.
     */
    public fun release(lesson: String, at: Instant): DomainResult<WorkItem> {
        if (lesson.isBlank()) return reject(Refusal.MissingLesson(id.value))
        return moveTo(WorkStatus.RELEASED, at, lesson)
    }

    /**
     * Abandon : le motif est conservé dans `outcome`.

     * Le motif est OBLIGATOIRE. « Abandonné » sans raison est indistinguable de
     * « perdu » — constaté sur les données : les 641 items abandonnés en portent
     * tous un. Le refus est typé, pas une exception de construction.
     */
    public fun abandon(reason: String, at: Instant): DomainResult<WorkItem> =
        if (reason.isBlank()) {
            reject(Refusal.MissingReason(id.value))
        } else {
            moveTo(WorkStatus.ABANDONED, at, reason)
        }

    /**
     * Le déplacement ne touche PAS la révision : celle-ci est incrémentée par la
     * base, dans la même instruction que l'écriture, et seulement si la garde
     * `revision = expected` passe. L'incrémenter ici laisserait croire qu'une
     * mutation locale vaut mutation persistée.
     */
    private fun moveTo(next: WorkStatus, at: Instant, outcome: String?): DomainResult<WorkItem> =
        if (!isLegalMove(status, next)) {
            reject(Refusal.IllegalTransition(status, next))
        } else {
            DomainResult.Ok(WorkItem(id, next, outcome, draft.copy(updatedAt = at), revision))
        }

    public companion object {
        public fun of(
            id: WorkItemId,
            status: WorkStatus,
            outcome: String?,
            draft: WorkItemDraft,
            revision: Revision,
        ): WorkItem = WorkItem(id, status, outcome, draft, revision)
    }
}
