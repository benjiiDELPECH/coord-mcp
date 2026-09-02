"""Port « identité du dépôt » — le local d'abord, jamais le réseau en premier.

Ces tests verrouillent la correction du 02.09.2026. Le défaut qu'ils empêchent
de revenir n'était pas seulement de la lenteur : `gh repo view` rendait
`benjiiDELPECH/alert-immo` (le miroir GitHub) alors que le dépôt réel est
`benjamin/alert-immo` sur Forgejo. Le serveur enregistrait donc les éléments de
travail sous un MAUVAIS nom de dépôt — deux agents sur le même dépôt pouvaient
être classés ailleurs, et leur collision passait inaperçue.

Un test de non-régression sur la performance seule aurait laissé passer ça.
"""

from __future__ import annotations

import subprocess

import pytest

from src.git_local_bridge import GitLocalBridge
from src.github_cli_bridge import GitHubCliBridge
from src.repo_identity import RESOLVEURS, resoudre_depot


def _init_depot(tmp_path, url: str):
    d = tmp_path / "depot"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=d, check=True)
    return str(d)


# ── L'adaptateur local ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,attendu",
    [
        ("https://git.delpech.dev/benjamin/alert-immo.git", "benjamin/alert-immo"),
        ("https://git.delpech.dev/benjamin/alert-immo", "benjamin/alert-immo"),
        ("git@git.delpech.dev:benjamin/alert-immo.git", "benjamin/alert-immo"),
        ("https://github.com/torvalds/linux.git", "torvalds/linux"),
        ("git@gitlab.com:groupe/projet.git", "groupe/projet"),
        # Une barre finale traîne souvent dans les URL copiées à la main.
        ("https://git.delpech.dev/benjamin/alert-immo/", "benjamin/alert-immo"),
    ],
)
def test_le_local_lit_toutes_les_forges(tmp_path, url, attendu) -> None:
    """Une seule expression régulière couvre GitHub, Forgejo, GitLab, Gitea.

    C'est ce qui rend l'adaptateur local suffisant dans tous les cas observés :
    les forges partagent la convention `hôte/propriétaire/dépôt`.
    """
    assert GitLocalBridge().resoudre(_init_depot(tmp_path, url)) == attendu


def test_le_local_rend_none_hors_depot(tmp_path) -> None:
    """Aucune exception ne doit remonter : l'appelant reçoit None et décide."""
    vide = tmp_path / "pas-un-depot"
    vide.mkdir()
    assert GitLocalBridge().resoudre(str(vide)) is None


def test_le_local_rend_none_sans_remote(tmp_path) -> None:
    d = tmp_path / "sans-remote"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    assert GitLocalBridge().resoudre(str(d)) is None


# ── L'ordre des adaptateurs ──────────────────────────────────────────


def test_le_local_passe_avant_le_reseau() -> None:
    """L'ordre EST le correctif, pas un détail d'implémentation.

    Inverser cette liste réintroduirait les 300 s d'attente ET le mauvais nom
    de dépôt. Le test échoue si quelqu'un réordonne.
    """
    assert RESOLVEURS[0].nom == "git-local"
    assert [a.nom for a in RESOLVEURS].index("git-local") < [
        a.nom for a in RESOLVEURS
    ].index("github-cli")


def test_github_nest_pas_appele_quand_le_local_repond(tmp_path, monkeypatch) -> None:
    """La preuve d'EFFET, pas seulement d'ordre.

    Sans ça, on vérifierait que la liste est bien triée sans jamais vérifier que
    le réseau est réellement épargné.
    """
    appels: list[str] = []

    def _mouchard(self, repo_path):  # noqa: ANN001
        appels.append(repo_path)
        return "quelqu-un/autre"

    monkeypatch.setattr(GitHubCliBridge, "resoudre", _mouchard)
    d = _init_depot(tmp_path, "https://git.delpech.dev/benjamin/alert-immo.git")

    assert resoudre_depot(d) == "benjamin/alert-immo"
    assert appels == [], "l'adaptateur réseau a été appelé alors que le local savait"


def test_repli_sur_github_si_le_local_echoue(tmp_path, monkeypatch) -> None:
    """Le repli existe toujours — on ne l'a pas supprimé, on l'a déclassé."""
    monkeypatch.setattr(GitLocalBridge, "resoudre", lambda self, p: None)
    monkeypatch.setattr(GitHubCliBridge, "resoudre", lambda self, p: "prop/depot")
    vide = tmp_path / "vide"
    vide.mkdir()
    assert resoudre_depot(str(vide)) == "prop/depot"


# ── Robustesse ───────────────────────────────────────────────────────


def test_un_adaptateur_qui_leve_ne_casse_pas_la_resolution(
    tmp_path, monkeypatch
) -> None:
    """Un adaptateur défaillant est le suivant à essayer, pas une panne.

    Le serveur coordonne des agents parallèles : s'il tombe, ils codent tous à
    l'aveugle sur les mêmes fichiers. Il doit dégrader, jamais s'interrompre.
    """

    def _explose(self, repo_path):  # noqa: ANN001
        raise RuntimeError("adaptateur cassé")

    monkeypatch.setattr(GitLocalBridge, "resoudre", _explose)
    monkeypatch.setattr(GitHubCliBridge, "resoudre", lambda self, p: "prop/depot")
    assert resoudre_depot(str(tmp_path)) == "prop/depot"


def test_aucun_adaptateur_ne_sait_rend_none(tmp_path, monkeypatch) -> None:
    """Contrat historique de `_detect_repo_slug` : None, jamais une exception."""
    monkeypatch.setattr(GitLocalBridge, "resoudre", lambda self, p: None)
    monkeypatch.setattr(GitHubCliBridge, "resoudre", lambda self, p: None)
    assert resoudre_depot(str(tmp_path)) is None


def test_le_local_est_rapide(tmp_path) -> None:
    """Garde-fou de non-régression : la mesure du 02.09 donnait 13 ms.

    Le seuil est volontairement large (500 ms). Il n'existe pas pour mesurer la
    vitesse mais pour attraper un retour au réseau : `gh` mettait 643 ms sur la
    même machine, et jusqu'à 300 s quand la forge ne répondait pas.
    """
    import time

    d = _init_depot(tmp_path, "https://git.delpech.dev/benjamin/alert-immo.git")
    t0 = time.monotonic()
    assert GitLocalBridge().resoudre(d) == "benjamin/alert-immo"
    assert (time.monotonic() - t0) < 0.5
