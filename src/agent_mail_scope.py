"""Pré-calcul de surface de conflit pour MCP Agent Mail.

POURQUOI CE MODULE
==================

`mcp_agent_mail` (Dicklesworthstone) est une couche de coordination mûre pour
agents de code : identités, boîtes aux lettres, fils de discussion, réservations
de fichiers consultatives, archive adossée à git, garde pre-commit, console TUI.
Elle fait tout ce que coord-mcp fait, en mieux — sauf UNE chose.

Sa détection de conflits est **purement syntaxique** : elle compare des motifs
glob. Mesuré le 02.09.2026 sur ce dépôt :

    Agent A déclare  ConfidenceCalculator.kt      (1 fichier)
    Agent B déclare  DvfComparablesService.kt     (1 fichier, autre module)

    Agent Mail  (globs)              -> 0 conflit, les deux agents partent
    expansion GitNexus               -> 1 CONFLIT

    car `ConfidenceCalculator` a un rayon d'explosion de 30 FICHIERS,
    et `DvfComparablesService` en fait partie.

Aucune ligne commune, aucun conflit git, deux PR qui fusionnent proprement — et
le produit casse, parce que B consomme ce que A vient de changer. C'est le
conflit SÉMANTIQUE : il n'a aucune trace textuelle, il vit dans le graphe
d'appels. Aucun outil qui raisonne sur des chemins ne peut le voir, quel que
soit son degré de sophistication : l'information n'est pas dans les chemins.

CE QUE CE MODULE FAIT — ET NE FAIT PAS
======================================

Il ne remplace ni ne modifie Agent Mail. Leur documentation dit :

    « the design assumes paths are calculated in advance by the calling agent »

C'est exactement l'ouverture dont on a besoin. `file_reservation_paths` accepte
une liste arbitraire ; il suffit de la lui fournir plus complète :

    symboles déclarés
       -> GitNexus impact (downstream)
          -> liste de fichiers
             -> file_reservation_paths(paths = cette liste)

Aucune contribution amont n'est nécessaire pour que ça marche. Le serveur fait
son travail sur une surface qu'on lui a donnée juste.

DÉGRADATION
===========

Si GitNexus est absent, périmé, ou lent, ce module rend les fichiers déclarés
tels quels et le signale. Il ne bloque JAMAIS : un agent empêché de réserver
parce que l'analyse d'impact est indisponible coderait sans aucune coordination
— strictement pire que de coordonner sur une surface incomplète.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import gitnexus_bridge


@dataclass
class SurfaceDeConflit:
    """Ce qu'on donne à `file_reservation_paths`, plus de quoi l'auditer."""

    paths: list[str]
    """La liste à passer telle quelle à Agent Mail."""

    declares: list[str] = field(default_factory=list)
    """Ce que l'agent avait déclaré à la main."""

    ajoutes_par_expansion: list[str] = field(default_factory=list)
    """Ce que le graphe d'appels a révélé en plus. C'est la valeur ajoutée."""

    avertissements: list[str] = field(default_factory=list)
    """Symboles non résolus, index périmé, GitNexus absent. Jamais silencieux."""

    expansion_active: bool = True
    """False = surface purement déclarative. L'agent DOIT le savoir : la
    coordination est alors aussi faible que celle d'Agent Mail seul."""

    def resume(self) -> str:
        """Une ligne lisible, destinée au champ `reason` de la réservation.

        Ça compte plus qu'il n'y paraît : le `reason` est ce que l'AUTRE agent
        lit quand il se heurte à la réservation. « 1 déclaré + 29 par impact »
        lui dit immédiatement pourquoi un fichier qu'il croyait libre ne l'est
        pas — sinon la réservation paraît arbitraire, et il passe outre.
        """
        n_d, n_e = len(self.declares), len(self.ajoutes_par_expansion)
        base = f"{n_d} déclaré(s)"
        if not self.expansion_active:
            return f"{base} · expansion INDISPONIBLE — surface incomplète"
        if n_e:
            return f"{base} + {n_e} par rayon d'explosion (GitNexus)"
        return f"{base} · aucun impact supplémentaire"


def surface_pour_agent_mail(
    repo_alias: str,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    depth: int = 2,
) -> SurfaceDeConflit:
    """Calcule la liste de chemins à réserver, expansion sémantique incluse.

    `repo_alias`   nom du dépôt dans l'index GitNexus (`gitnexus list-repos`)
    `scope_files`  fichiers que l'agent déclare toucher
    `scope_symbols` symboles qu'il déclare toucher — c'est EUX qui portent la
                   valeur : un symbole se traduit en son rayon d'explosion
    `depth`        profondeur de parcours du graphe. 2 par défaut : au-delà, la
                   surface enfle et la réservation devient un verrou global.

    Rend toujours un objet exploitable. Ne lève pas.
    """
    declares = sorted(set(scope_files or []))
    symboles = list(scope_symbols or [])

    if not symboles:
        return SurfaceDeConflit(
            paths=declares,
            declares=declares,
            expansion_active=True,
            avertissements=[],
        )

    if not gitnexus_bridge.gitnexus_available():
        return SurfaceDeConflit(
            paths=declares,
            declares=declares,
            expansion_active=False,
            avertissements=[
                "GitNexus indisponible — surface limitée aux fichiers déclarés. "
                "Un conflit sémantique (deux modules liés par le graphe d'appels) "
                "ne sera PAS détecté."
            ],
        )

    resultat = gitnexus_bridge.expand_scope(repo_alias, symboles, depth=depth)
    etendus = set(resultat.get("files") or [])
    avertissements = list(resultat.get("warnings") or [])

    tout = sorted(set(declares) | etendus)
    ajoutes = sorted(etendus - set(declares))

    return SurfaceDeConflit(
        paths=tout,
        declares=declares,
        ajoutes_par_expansion=ajoutes,
        avertissements=avertissements,
        expansion_active=True,
    )


def arguments_reservation(
    surface: SurfaceDeConflit,
    project_key: str,
    agent_name: str,
    ttl_seconds: int = 3600,
    exclusive: bool = True,
    motif: str = "",
) -> dict:
    """Le dictionnaire prêt à passer à `file_reservation_paths` d'Agent Mail.

    Signature amont (README de mcp_agent_mail) :
        project_key, agent_name, paths[], ttl_seconds, exclusive, reason

    Le `reason` est enrichi du résumé d'expansion — voir `SurfaceDeConflit.resume`
    pour pourquoi ce détail n'est pas cosmétique.
    """
    raison = f"{motif} · {surface.resume()}" if motif else surface.resume()
    return {
        "project_key": project_key,
        "agent_name": agent_name,
        "paths": surface.paths,
        "ttl_seconds": ttl_seconds,
        "exclusive": exclusive,
        "reason": raison,
    }
