"""Port « identité du dépôt » — le domaine ne connaît que la question.

Même forme que `scope_resolver.py` : ce fichier ne contient QUE le Protocol et
la résolution en cascade. Les implémentations vivent dans `*_bridge.py`, comme
`gitnexus_bridge` implémente `ScopeResolver`.

POURQUOI CE PORT EXISTE
=======================

`_detect_repo_slug` appelait `gh repo view` en dur, dans le chemin critique de
`checkin`, `checkout_work` et `list_active_work`. Mesuré le 02.09.2026 sur
241 appels réels d'agents :

    outil                médiane    p90      max     erreurs
    list_active_work      0,51 s   60,7 s   300 s    13 / 58
    checkout_work         1,67 s   58,5 s   311 s     9 / 58
    abandon_work          0,06 s    0,7 s     3 s     0 / 17   <- ne sort pas du processus

Un facteur 5 000, à langage et base de données identiques. Le goulot n'était ni
Python ni SQLite : c'était un appel réseau vers une forge étrangère au dépôt.

Et le défaut le plus grave n'était pas la lenteur :

    git-local    -> benjamin/alert-immo          13,3 ms
    github-cli   -> benjiiDELPECH/alert-immo    643,0 ms

`gh` rendait le nom du MIROIR GitHub, pas celui du dépôt Forgejo réel. Les
éléments de travail étaient enregistrés sous un mauvais nom : deux agents sur le
même dépôt pouvaient être classés ailleurs, et leur collision passer inaperçue.
Pour un serveur dont la raison d'être est de détecter les collisions, c'est une
panne silencieuse de sa fonction première.

LES TROIS COUCHES
=================

    noyau         déclarer / détecter / journaliser   -> SQLite seul
    vérification  périmètre déclaré vs diff réel      -> git, légitimement
    liaison       tickets, PR parallèles              -> forge, accessoire

Le noyau doit fonctionner SANS AUCUNE FORGE. Le défaut corrigé ici est le cas
type d'une dépendance de commodité — la liaison aux tickets — qui avait
contaminé le noyau, pour aller chercher une simple clé de base locale.
"""

from __future__ import annotations

from typing import Protocol

from .git_local_bridge import GitLocalBridge
from .github_cli_bridge import GitHubCliBridge


class RepoIdentityResolver(Protocol):
    """Répond à : « quel est le nom canonique `propriétaire/dépôt` d'ici ? »"""

    nom: str

    def resoudre(self, repo_path: str) -> str | None:  # pragma: no cover - protocole
        ...


# L'ordre EST le correctif, pas un détail. Le local d'abord : il répond toujours,
# hors ligne, et une seule expression régulière couvre GitHub, Forgejo, GitLab,
# Gitea et Bitbucket. Le réseau n'est sollicité que s'il échoue — ce qui ne se
# produit pas sur un dépôt git valide.
RESOLVEURS: list[RepoIdentityResolver] = [GitLocalBridge(), GitHubCliBridge()]


def resoudre_depot(repo_path: str) -> str | None:
    """Nom canonique `propriétaire/dépôt`, ou None si aucun résolveur ne sait.

    Ne lève jamais : un résolveur qui échoue est simplement le suivant à essayer.
    L'appelant reçoit None et décide — contrat historique de `_detect_repo_slug`.

    Ce serveur coordonne des agents parallèles : s'il tombe, ils codent tous à
    l'aveugle sur les mêmes fichiers. Il doit dégrader, jamais s'interrompre.
    """
    for resolveur in RESOLVEURS:
        try:
            slug = resolveur.resoudre(repo_path)
        except Exception:  # noqa: BLE001 — un résolveur défaillant ne doit
            continue      # jamais faire tomber la résolution entière.
        if slug:
            return slug
    return None
