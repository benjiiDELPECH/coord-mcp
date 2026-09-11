"""Porte d'arrivée MÉCANIQUE — périmètre effectif et refus de certifier.

Contexte mesuré le 11/09/2026, sur un work item réel : `checkout_work` a rendu
`diff_source: "repo-fallback"`, `out_of_scope_count: 37` et
`ready_to_merge: true`. Trois défauts, tous dans le même sens — la porte
certifiait ce qu'elle n'avait pas évalué :

1. elle comparait le diff aux seuls fichiers DÉCLARÉS, jamais au périmètre
   effectif (déclaré ∪ expansion GitNexus) que `checkin` avait lui-même
   calculé — alors que `_find_conflicts` et `plan_parallel_waves` l'utilisent,
   eux ;
2. conséquence directe : `scope_files` vide ⇒ `out_of_scope` vide par
   construction ⇒ **aucun contrôle**, donc un feu vert gratuit pour un agent qui
   ne déclarait que des symboles ;
3. un écart de périmètre n'était qu'un `warning`, et un diff non attribuable à
   ce work item passait quand même.

Ces tests verrouillent la version mécanique : la porte BLOQUE, et elle ne
bloque que ce qu'elle SAIT faux.
"""

from __future__ import annotations

import importlib
import subprocess
from unittest.mock import patch

import pytest


@pytest.fixture
def gate(temp_db):
    """`temp_db` recharge `src.db` et `src.work_items`, mais PAS `src.checkout`.

    Or `checkout.py` fait `from .db import connection` : il a donc lié l'ANCIENNE
    fonction à l'import, et pointerait encore sur la base précédente — l'item
    créé par `checkin` y serait introuvable et `checkout()` rendrait
    `{"error": ...}`. On recharge le module ici (même besoin que
    `_reload_checkout()` dans `test_ci_concurrency.py`).
    """
    _db, wi = temp_db
    import src.checkout as _co
    importlib.reload(_co)
    return _db, wi, _co


def _fake_gh(cmd, **kwargs):
    """`gh` muet : pas de repo slug, pas de PR — la porte reste hors-ligne."""
    if cmd[:3] == ["gh", "repo", "view"]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not a repo")
    return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="stubbed")


def _resolver(files):
    return lambda repo_alias, symbols, depth=2: {"files": files, "warnings": []}


def _item(wi, **kwargs):
    """Crée un work item. `work_items.checkin` n'a pas de paramètre `triggers_ci`
    (il n'existe que sur l'outil MCP) : la colonne reste à son défaut (0), donc
    le gate de concurrence CI est inerte et ne pollue pas ces tests."""
    kwargs.setdefault("repo_path", ".")
    kwargs.setdefault("agent_id", "agent-a")
    return wi.checkin(**kwargs)["work_item_id"]


def _bloque(result):
    return any(
        "Hors périmètre" in b or "Aucun verdict d'impact" in b or "Diff vide" in b
        for b in result["blockers"]
    )


# ── 1. Le périmètre effectif ────────────────────────────────────────────────


def test_fichier_de_l_expansion_n_est_pas_hors_perimetre(gate):
    """CONtre-épreuve du périmètre effectif : un fichier atteint par le rayon
    d'impact GitNexus, non déclaré explicitement, ne doit PAS être bloqué.

    C'est le cas sémantique légitime — exactement ce que `scope_symbols` existe
    pour couvrir. Une porte qui le refuserait punirait l'agent d'avoir bien
    déclaré son intention.
    """
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["a.py", "hq/B.kt"]))
        wi_id = _item(wi, title="Déclare un symbole", scope_symbols=["S"])
        result = co.checkout(wi_id, diff_files=["hq/B.kt"])

    assert result["out_of_scope_count"] == 0, result["blockers"]
    assert result["ready_to_merge"] is True, result["blockers"]
    assert result["effective_scope_count"] == 2
    assert result["impact_verdict_attached"] is True


def test_hors_perimetre_effectif_bloque(gate):
    """Un vrai écart de périmètre n'est plus un avertissement : c'est un refus."""
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["a.py"]))
        wi_id = _item(wi, title="Un seul fichier", scope_symbols=["S"])
        result = co.checkout(wi_id, diff_files=["a.py", "intrus.py"])

    assert result["out_of_scope_count"] == 1
    assert result["ready_to_merge"] is False
    assert any("Hors périmètre" in b for b in result["blockers"]), result["blockers"]


def test_porte_muette_quand_seuls_des_symboles_sont_declares(gate):
    """LE trou fermé. `scope_files` vide ⇒ l'ancien calcul rendait `out_of_scope`
    vide par construction : la porte ne disait RIEN, ni avertissement ni refus,
    et certifiait `ready_to_merge: true` sans avoir comparé un seul fichier.
    """
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["hq/A.kt"]))
        wi_id = _item(wi, title="Symboles seuls", scope_symbols=["S"])
        result = co.checkout(wi_id, diff_files=["totalement/ailleurs.kt"])

    assert result["declared_files"] == []
    assert result["out_of_scope_count"] == 1, "la porte est restée muette"
    assert result["ready_to_merge"] is False
    assert _bloque(result), result["blockers"]


# ── 2. Refuser ce qu'on n'a pas mesuré ──────────────────────────────────────


def test_aucun_verdict_d_impact_bloque(gate):
    """Symboles déclarés, expansion vide : le périmètre retombe sur le régime
    « globs ». Le résolveur GitNexus étant branché par défaut côté serveur, cet
    état signale une expansion qui a échoué — pas un mode normal.
    """
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver([]))  # symboles non résolus
        wi_id = _item(wi, title="Symbole non résolu", scope_symbols=["Inconnu"])
        result = co.checkout(wi_id, diff_files=["a.py"])

    assert result["impact_verdict_attached"] is False
    assert result["ready_to_merge"] is False
    assert any("Aucun verdict d'impact" in b for b in result["blockers"]), result["blockers"]


def test_diff_vide_non_attribuable_bloque(gate):
    """Diff vide ET source `repo-fallback` : la porte n'a rien mesuré. Même règle
    que le job `changes` de la porte CI — un run où rien n'a tourné ne publie pas
    un feu vert.
    """
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["a.py"]))
        wi_id = _item(wi, title="Diff introuvable", scope_symbols=["S"])
        with patch.object(co, "_auto_detect_diff_files", return_value=([], "repo-fallback", [])):
            result = co.checkout(wi_id)

    assert result["diff_source"] == "repo-fallback"
    assert result["actual_diff_files"] == []
    assert result["ready_to_merge"] is False
    assert any("Diff vide" in b for b in result["blockers"]), result["blockers"]


def test_le_cas_mesure_du_11_09_ne_passe_plus(gate):
    """NON-RÉGRESSION du cas réel : `repo-fallback` + 37 fichiers hors périmètre
    + `ready_to_merge: true`. Les 37 fichiers étaient un artefact du mauvais
    arbre ; la porte les comptait quand même comme hors périmètre et certifiait
    quand même la fusion.
    """
    _db, wi, co = gate
    intrus = [f"ailleurs/f{i}.kt" for i in range(37)]
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["hq/A.kt"]))
        wi_id = _item(wi, title="Le cas mesuré", scope_symbols=["S"])
        with patch.object(co, "_auto_detect_diff_files", return_value=(intrus, "repo-fallback", [])):
            result = co.checkout(wi_id)

    assert result["out_of_scope_count"] == 37
    assert result["diff_source"] == "repo-fallback"
    assert result["ready_to_merge"] is False, "le cas mesuré repasse au vert"


# ── 3. Le cas nominal reste vert (sans quoi un refus systématique suffirait) ──


def test_cas_nominal_reste_vert(gate):
    """CONTRE-ÉPREUVE globale : diff explicite, dans le périmètre, verdict
    d'impact présent ⇒ la porte DOIT dire oui."""
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi.set_scope_resolver(_resolver(["a.py", "hq/B.kt"]))
        wi_id = _item(wi, title="Travail propre", scope_symbols=["S"], scope_files=["a.py"])
        result = co.checkout(wi_id, diff_files=["a.py", "hq/B.kt"])

    assert result["out_of_scope_count"] == 0
    assert result["blockers"] == []
    assert result["ready_to_merge"] is True
    assert result["diff_source"] == "explicit"


def test_sans_symboles_le_comportement_declare_demeure(gate):
    """Rétro-compatibilité : aucun symbole déclaré, diff ⊆ fichiers déclarés,
    aucun blocage nouveau."""
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        wi_id = _item(wi, title="Déclaration par fichiers", scope_files=["a.py", "b.py"])
        result = co.checkout(wi_id, diff_files=["a.py"])

    assert result["out_of_scope_count"] == 0
    assert result["ready_to_merge"] is True
    assert result["impact_verdict_attached"] is False  # aucun symbole ⇒ rien à attacher


# ── 4. Symétrie du conflit parallèle ────────────────────────────────────────


def test_conflit_parallele_voit_l_expansion_de_l_autre(gate):
    """La porte comparait le diff aux `scope_files` BRUTS des autres items,
    pendant que `_find_conflicts` utilisait leur périmètre effectif. Elle était
    donc sémantiquement aveugle d'un seul côté : un autre agent qui n'avait
    déclaré qu'un symbole lui était invisible.
    """
    _db, wi, co = gate
    with patch.object(subprocess, "run", side_effect=_fake_gh):
        # L'autre agent ne déclare QUE le symbole « HqController », qui s'étend
        # sur hq/model/HqSitrep.kt — son scope_files est vide.
        wi.set_scope_resolver(_resolver(["hq/model/HqSitrep.kt"]))
        _item_autre = _item(wi, title="Autre agent — symbole seul", scope_symbols=["HqController"], agent_id="agent-b")

        wi.set_scope_resolver(_resolver(["hq/model/HqSitrep.kt"]))
        mine = _item(wi, title="Moi — touche le même fichier", scope_files=["hq/model/HqSitrep.kt"])
        result = co.checkout(mine, diff_files=["hq/model/HqSitrep.kt"])

    assert len(result["parallel_conflicts"]) == 1, result["parallel_conflicts"]
    # On asserte sur `work_item_id`, pas `agent_id` : la clé `agent_id` a été
    # ajoutée au dict de conflit par une branche NON fusionnée dans `origin/main`.
    assert result["parallel_conflicts"][0]["work_item_id"] == _item_autre
    assert "hq/model/HqSitrep.kt" in result["parallel_conflicts"][0]["overlapping_files"]
    assert result["ready_to_merge"] is False
