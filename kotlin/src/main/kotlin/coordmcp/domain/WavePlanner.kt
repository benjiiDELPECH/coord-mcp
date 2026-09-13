package coordmcp.domain

/**
 * Planification de vagues — fonction PURE du domaine, aucun port, aucune base.
 *
 * Deux travaux ne peuvent pas s'exécuter en parallèle s'ils touchent les mêmes
 * fichiers. On partitionne donc l'ensemble actif en un minimum de vagues où
 * chaque vague est sans conflit interne : c'est un coloriage glouton du graphe
 * de conflits.
 *
 * Glouton, donc pas optimal au sens strict — mais DÉTERMINISTE, ce qui compte
 * davantage : deux appels sur le même état rendent le même plan, et un
 * ordonnancement qu'on ne peut pas reproduire ne se diagnostique pas.
 */
public object WavePlanner {

    public fun plan(items: List<WorkItem>): List<List<WorkItemId>> {
        // Tri par identifiant : sans lui, l'ordre d'itération déciderait du plan
        // et le résultat varierait d'une exécution à l'autre.
        val ordered = items.sortedBy { it.id.value }
        val waves = mutableListOf<MutableList<WorkItem>>()

        for (item in ordered) {
            val target = waves.firstOrNull { wave -> wave.none { conflicts(it, item) } }
            if (target == null) {
                waves += mutableListOf(item)
            } else {
                target += item
            }
        }
        return waves.map { wave -> wave.map { it.id } }
    }

    /** Deux travaux se disputent-ils un fichier ? */
    public fun conflicts(left: WorkItem, right: WorkItem): Boolean =
        left.scope.paths.toSet().intersect(right.scope.paths.toSet()).isNotEmpty()
}
