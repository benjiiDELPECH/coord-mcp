"""Pré-calcul de surface pour Agent Mail — ce que les globs ne peuvent pas voir.

Ces tests verrouillent la seule chose que `mcp_agent_mail` ne sait pas faire, et
qu'on lui apporte sans le modifier : traduire un SYMBOLE en son rayon d'explosion
avant de réserver.

Le cas nominal est vérifié contre un faux résolveur : la valeur mesurée sur le
dépôt réel (1 symbole -> 30 fichiers) sert de référence dans les commentaires,
pas d'assertion — un test qui dépend de l'index GitNexus casserait à chaque
réindexation, pour une raison sans rapport avec ce qu'il vérifie.
"""

from __future__ import annotations

import pytest

from src import agent_mail_scope
from src.agent_mail_scope import arguments_reservation, surface_pour_agent_mail


@pytest.fixture
def gitnexus_muet(monkeypatch):
    """GitNexus absent : cas le plus courant sur un poste non indexé."""
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge, "gitnexus_available", lambda: False
    )


@pytest.fixture
def gitnexus_repond(monkeypatch):
    """Un rayon d'explosion factice, stable et lisible."""
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge, "gitnexus_available", lambda: True
    )
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge,
        "expand_scope",
        lambda alias, symboles, depth=2: {
            "files": [
                "src/confidence/ConfidenceCalculator.kt",
                "src/enrich/DvfComparablesService.kt",
                "src/enrich/ComparablesEngine.kt",
            ],
            "warnings": [],
        },
    )


# ── Ce qui justifie le module ────────────────────────────────────────


def test_le_symbole_devient_un_rayon_d_explosion(gitnexus_repond) -> None:
    """LE test. Un symbole déclaré, trois fichiers réservés.

    Sans ça, Agent Mail réserverait `ConfidenceCalculator.kt` seul, un second
    agent réserverait `DvfComparablesService.kt`, aucun conflit ne serait
    signalé — et le produit casserait sans qu'aucune ligne commune n'ait été
    touchée. Mesuré en réel sur alert-immo : 1 symbole -> 30 fichiers.
    """
    s = surface_pour_agent_mail("alert-immo", scope_symbols=["ConfidenceCalculator"])
    assert len(s.paths) == 3
    assert "src/enrich/DvfComparablesService.kt" in s.paths
    assert len(s.ajoutes_par_expansion) == 3


def test_le_conflit_semantique_est_attrape(gitnexus_repond) -> None:
    """Reproduit exactement le scénario où les globs échouent.

    A et B ne déclarent AUCUN fichier commun. L'intersection est vide pour une
    comparaison de chemins, non vide une fois l'impact étendu.
    """
    a = surface_pour_agent_mail("alert-immo", scope_symbols=["ConfidenceCalculator"])
    b_declare = {"src/enrich/DvfComparablesService.kt"}

    assert not (set(["src/confidence/ConfidenceCalculator.kt"]) & b_declare), (
        "les fichiers déclarés ne se recouvrent pas — un glob ne verrait rien"
    )
    assert set(a.paths) & b_declare, "l'expansion doit révéler le chevauchement"


# ── Dégradation : ne jamais bloquer ──────────────────────────────────


def test_sans_gitnexus_on_rend_les_declares_et_on_le_dit(gitnexus_muet) -> None:
    """Un agent empêché de réserver coderait SANS coordination — pire que de
    coordonner sur une surface incomplète. Donc on dégrade, et on l'annonce."""
    s = surface_pour_agent_mail(
        "alert-immo", scope_files=["a/b.kt"], scope_symbols=["Truc"]
    )
    assert s.paths == ["a/b.kt"]
    assert s.expansion_active is False
    assert s.avertissements, "un silence ici ferait croire à une surface complète"
    assert "INDISPONIBLE" in s.resume()


def test_sans_symbole_on_n_appelle_pas_gitnexus(monkeypatch) -> None:
    """Pas de symbole = rien à étendre. Appeler l'index quand même coûterait
    ~1 s par réservation pour un résultat connu d'avance."""
    appels: list = []
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge,
        "gitnexus_available",
        lambda: appels.append("appelé") or True,
    )
    s = surface_pour_agent_mail("alert-immo", scope_files=["x.kt", "y.kt"])
    assert s.paths == ["x.kt", "y.kt"]
    assert appels == []


def test_symbole_introuvable_avertit_sans_perdre_les_declares(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge, "gitnexus_available", lambda: True
    )
    monkeypatch.setattr(
        agent_mail_scope.gitnexus_bridge,
        "expand_scope",
        lambda a, s, depth=2: {"files": [], "warnings": ["Truc: Target not found"]},
    )
    s = surface_pour_agent_mail(
        "alert-immo", scope_files=["garde.kt"], scope_symbols=["Truc"]
    )
    assert s.paths == ["garde.kt"], "les déclarés ne doivent jamais être perdus"
    assert s.avertissements


# ── Contrat avec Agent Mail ──────────────────────────────────────────


def test_arguments_conformes_a_la_signature_amont(gitnexus_repond) -> None:
    """Signature du README de mcp_agent_mail :
    project_key, agent_name, paths[], ttl_seconds, exclusive, reason
    """
    s = surface_pour_agent_mail("alert-immo", scope_symbols=["ConfidenceCalculator"])
    a = arguments_reservation(s, project_key="p", agent_name="agent-A", motif="bd-1")
    assert set(a) == {
        "project_key",
        "agent_name",
        "paths",
        "ttl_seconds",
        "exclusive",
        "reason",
    }
    assert isinstance(a["paths"], list) and a["paths"]


def test_le_motif_explique_l_expansion_a_l_autre_agent(gitnexus_repond) -> None:
    """Le `reason` est ce que lit l'agent qui se heurte à la réservation.

    Sans explication, un fichier qu'il croyait libre paraît réservé
    arbitrairement — et il passe outre, ce qui annule toute la coordination.
    """
    s = surface_pour_agent_mail("alert-immo", scope_symbols=["ConfidenceCalculator"])
    raison = arguments_reservation(s, project_key="p", agent_name="a", motif="bd-1")[
        "reason"
    ]
    assert "bd-1" in raison
    assert "rayon d'explosion" in raison


def test_pas_de_doublon_entre_declares_et_etendus(gitnexus_repond) -> None:
    """Un chemin declare ET dans le rayon d'explosion ne doit apparaitre qu'une
    fois : Agent Mail compte les chemins, un doublon fausserait ses conflits."""
    s = surface_pour_agent_mail(
        "alert-immo",
        scope_files=["src/confidence/ConfidenceCalculator.kt"],
        scope_symbols=["ConfidenceCalculator"],
    )
    assert len(s.paths) == len(set(s.paths))
    assert "src/confidence/ConfidenceCalculator.kt" not in s.ajoutes_par_expansion
