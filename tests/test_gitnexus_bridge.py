"""Tests for src/gitnexus_bridge.py — the CLI stays mocked, never invoked for real."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import src.gitnexus_bridge as gb


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["gitnexus"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_expand_scope_empty_symbols_short_circuits():
    """No symbols declared → no subprocess call at all."""
    with patch.object(subprocess, "run") as mock_run:
        result = gb.expand_scope("alert-immo", [])
    mock_run.assert_not_called()
    assert result == {"files": [], "warnings": []}


def test_expand_scope_gitnexus_not_installed():
    """CLI absent → warning, empty files, checkin must never crash on this."""
    with patch.object(gb, "gitnexus_available", return_value=False):
        with patch.object(subprocess, "run") as mock_run:
            result = gb.expand_scope("alert-immo", ["SomeSymbol"])
    mock_run.assert_not_called()
    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "not found" in result["warnings"][0]


def test_expand_scope_success_extracts_target_and_depth_files():
    """Real shape observed from `gitnexus impact`: target.filePath + byDepth[*][*].filePath."""
    payload = {
        "target": {"name": "HqController", "filePath": "hq/HqController.kt"},
        "direction": "downstream",
        "impactedCount": 1,
        "byDepth": {
            "1": [{"depth": 1, "filePath": "hq/model/HqSitrep.kt", "relationType": "IMPORTS"}]
        },
    }
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout=json.dumps(payload))):
            result = gb.expand_scope("alert-immo", ["HqController"])

    assert result["warnings"] == []
    assert result["files"] == ["hq/HqController.kt", "hq/model/HqSitrep.kt"]


def test_expand_scope_unknown_symbol_degrades_to_warning():
    """gitnexus returns {"error": ...} for an unresolved symbol — must not raise."""
    payload = {"error": "Target 'Ghost' not found", "impactedCount": 0}
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout=json.dumps(payload))):
            result = gb.expand_scope("alert-immo", ["Ghost"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "Ghost" in result["warnings"][0]
    assert "not found" in result["warnings"][0]


def test_expand_scope_timeout_degrades_to_warning():
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="gitnexus", timeout=10)):
            result = gb.expand_scope("alert-immo", ["SlowSymbol"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "timed out" in result["warnings"][0]


def test_expand_scope_invalid_json_degrades_to_warning():
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout="not json {{{")):
            result = gb.expand_scope("alert-immo", ["Weird"])

    assert result["files"] == []
    assert len(result["warnings"]) == 1
    assert "invalid JSON" in result["warnings"][0]


def test_expand_scope_multiple_symbols_union_files_and_collect_all_warnings():
    """One symbol resolves, one doesn't — both outcomes must surface, not just the last."""
    good_payload = {"target": {"filePath": "a.kt"}, "byDepth": {}}
    bad_payload = {"error": "Target 'Ghost' not found"}

    def fake_run(cmd, **kwargs):
        symbol = cmd[2]
        if symbol == "Known":
            return _fake_completed(stdout=json.dumps(good_payload))
        return _fake_completed(stdout=json.dumps(bad_payload))

    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            result = gb.expand_scope("alert-immo", ["Known", "Ghost"])

    assert result["files"] == ["a.kt"]
    assert len(result["warnings"]) == 1
    assert "Ghost" in result["warnings"][0]


# =============================================================================
# Voie SERVICE CHAUD (P0) et cache (P2) — ajoutés le 11/09/2026.
#
# Les tests ci-dessus exercent la voie CLI : la fixture autouse ci-dessous rend
# le service « indisponible », ce qui reproduit exactement le comportement
# d'avant le patch. Les tests ci-dessous couvrent la nouvelle voie.
# =============================================================================
import time  # noqa: E402


@pytest.fixture(autouse=True)
def _isoler_voies(monkeypatch):
    """Cache vide + service désactivé par défaut, pour un test déterministe.

    Sans ça, les tests CLI taperaient le service réel s'il tourne sur la machine,
    et le cache module-level ferait fuiter un résultat d'un test à l'autre.
    """
    gb._reset_cache()
    # On désactive le service via `service_enabled` — et NON en stubbant
    # `_service_impact`. Premier jet : stubber la fonction empêchait les tests de
    # la voie service de l'atteindre (ils appelaient le stub), et le test qui
    # vérifie la charge utile envoyée ne voyait aucun appel.
    monkeypatch.setattr(gb, "service_enabled", lambda: False)
    yield
    gb._reset_cache()


def test_service_chaud_est_utilise_en_premier_et_evite_le_cli():
    """Le service répond ⇒ AUCUN subprocess lancé (c'est tout l'objet du patch)."""
    payload = {"target": {"filePath": "hq/HqController.kt"},
               "byDepth": {"1": [{"filePath": "hq/model/HqSitrep.kt"}]}}
    with patch.object(gb, "_service_impact", return_value=(gb._extract_files(payload), None)):
        with patch.object(subprocess, "run") as mock_run:
            result = gb.expand_scope("alert-immo", ["HqController"])
    mock_run.assert_not_called()
    assert result["warnings"] == []
    assert result["files"] == ["hq/HqController.kt", "hq/model/HqSitrep.kt"]


def test_service_indisponible_bascule_sur_le_cli():
    """Service injoignable ⇒ le CLI prend le relais : aucune perte de capacité."""
    payload = {"target": {"filePath": "a.kt"}, "byDepth": {}}
    with patch.object(gb, "gitnexus_available", return_value=True):
        with patch.object(subprocess, "run", return_value=_fake_completed(stdout=json.dumps(payload))):
            result = gb.expand_scope("alert-immo", ["A"])
    assert result["files"] == ["a.kt"]
    assert result["warnings"] == []


def test_ni_service_ni_cli_garde_le_message_historique():
    with patch.object(gb, "gitnexus_available", return_value=False):
        with patch.object(subprocess, "run") as mock_run:
            result = gb.expand_scope("alert-immo", ["A"])
    mock_run.assert_not_called()
    assert result["warnings"] == ["gitnexus CLI not found in PATH — semantic scope expansion skipped"]


def test_cache_evite_le_second_appel():
    """P2 : un même symbole n'est résolu qu'UNE fois dans la fenêtre TTL."""
    appels = []

    def _compte(*a, **k):
        appels.append(a)
        return (["x.kt"], None)

    with patch.object(gb, "_service_impact", side_effect=_compte):
        r1 = gb.expand_scope("alert-immo", ["X"])
        r2 = gb.expand_scope("alert-immo", ["X"])

    assert r1["files"] == ["x.kt"] and r2["files"] == ["x.kt"]
    assert len(appels) == 1, f"le service a été appelé {len(appels)} fois au lieu d'1"


def test_cache_expire():
    """Passé le TTL, on re-résout — le blast-radius suit les commits."""
    appels = []

    def _compte(*a, **k):
        appels.append(a)
        return (["x.kt"], None)

    with patch.object(gb, "_service_impact", side_effect=_compte):
        gb.expand_scope("alert-immo", ["X"])
        # On force l'expiration de l'entrée EXISTANTE. Patcher `CACHE_TTL_S` ne
        # suffirait pas : l'échéance est calculée à l'écriture, pas à la lecture
        # (première version de ce test : fausse, elle passait pour la mauvaise raison).
        # La clé inclut désormais la RÉVISION D'INDEX : on ne présume plus de sa
        # forme, on prend la seule entrée présente.
        with gb._CACHE_LOCK:
            cle = next(iter(gb._CACHE))
            echeance, fichiers, avert = gb._CACHE[cle]
            gb._CACHE[cle] = (echeance - gb.CACHE_TTL_S - 1, fichiers, avert)
        gb.expand_scope("alert-immo", ["X"])
    assert len(appels) == 2


def test_la_cle_de_cache_inclut_repo_et_depth():
    """Deux dépôts (ou deux profondeurs) ne doivent PAS partager une entrée."""
    appels = []

    def _compte(repo, symbol, depth):
        appels.append((repo, symbol, depth))
        return ([f"{repo}-{depth}.kt"], None)

    with patch.object(gb, "_service_impact", side_effect=_compte):
        gb.expand_scope("alert-immo", ["X"], depth=2)
        gb.expand_scope("delpech-infra", ["X"], depth=2)
        gb.expand_scope("alert-immo", ["X"], depth=3)
    assert len(appels) == 3, appels


def test_un_avertissement_du_service_est_conserve():
    with patch.object(gb, "_service_impact", return_value=([], "X: Target 'X' not found")):
        result = gb.expand_scope("alert-immo", ["X"])
    assert result["files"] == []
    assert result["warnings"] == ["X: Target 'X' not found"]


def test_le_service_ne_leve_jamais_une_exception():
    """Contrat du module : toute panne dégrade, aucune ne remonte."""
    with patch.object(gb, "_service_impact", side_effect=RuntimeError("boom")):
        with patch.object(gb, "gitnexus_available", return_value=False):
            result = gb.expand_scope("alert-immo", ["X"])
    assert result["files"] == []
    assert len(result["warnings"]) == 1


def test_extract_files_est_partage_entre_les_deux_voies():
    payload = {"target": {"filePath": "t.kt"}, "byDepth": {"1": [{"filePath": "a.kt"}, {}], "2": None}}
    assert gb._extract_files(payload) == ["a.kt", "t.kt"]


def test_le_parametre_est_maxDepth_et_non_depth():
    """Verrouille le piège mesuré le 11/09/2026.

    Le schéma de l'outil MCP déclare `maxDepth` (défaut 3). Un argument `depth`
    est IGNORÉ SANS ERREUR : le serveur applique son défaut de 3 niveaux, et on
    croit à tort que les deux voies divergent (47 vs 42 fichiers). Ce test
    inspecte la charge utile réellement envoyée.
    """
    envoyes = []

    def _capture(payload, session_id):
        envoyes.append(payload)
        return {"result": {"content": [{"type": "text", "text": '{"target": {"filePath": "a.kt"}}'}]}}, "sid"

    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", side_effect=_capture):
                gb._service_impact("alert-immo", "X", 2)

    appels = [p for p in envoyes if p.get("method") == "tools/call"]
    assert appels, "aucun tools/call émis"
    args = appels[0]["params"]["arguments"]
    assert "maxDepth" in args, f"maxDepth absent de {list(args)}"
    assert args["maxDepth"] == 2
    assert "depth" not in args, "un argument `depth` serait ignoré sans erreur par le serveur"


# =============================================================================
# Non-régression des DEUX bugs réellement rencontrés le 11/09/2026, plus les
# trous de couverture de la nouvelle voie service.
# =============================================================================
import threading  # noqa: E402
import urllib.error  # noqa: E402


class _FausseReponse:
    """Réponse HTTP minimale : entêtes + corps, utilisable en `with`."""

    def __init__(self, corps: str, entetes: dict | None = None):
        self._corps = corps.encode()
        self.headers = entetes or {}

    def read(self):
        return self._corps

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- Bug n°1 : le transport répond en SSE (`data: {...}`) --------------------
def test_rpc_parse_une_reponse_sse():
    """Premier bug : on cherchait une ligne commençant par `{` → session jamais ouverte."""
    corps = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    with patch.object(gb.urllib.request, "urlopen", return_value=_FausseReponse(corps, {"mcp-session-id": "s1"})):
        data, session = gb._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
    assert data == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, data
    assert session == "s1"


def test_rpc_parse_aussi_du_json_pur():
    """Certains serveurs ne préfixent pas : on doit accepter les deux formes."""
    corps = '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
    with patch.object(gb.urllib.request, "urlopen", return_value=_FausseReponse(corps)):
        data, _ = gb._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
    assert data["result"] == {"ok": True}


def test_rpc_sur_corps_illisible_ne_leve_pas():
    with patch.object(gb.urllib.request, "urlopen", return_value=_FausseReponse("pas du json du tout")):
        data, session = gb._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
    assert data is None and session is None


def test_rpc_sur_panne_reseau_ne_leve_pas():
    with patch.object(gb.urllib.request, "urlopen", side_effect=urllib.error.URLError("refusé")):
        data, session = gb._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
    assert data is None and session is None


def test_rpc_transmet_l_entete_de_session_quand_elle_existe():
    vus = {}

    def _capture(request, timeout=None):
        vus["entetes"] = dict(request.headers)
        return _FausseReponse('data: {"jsonrpc":"2.0","id":2,"result":{}}')

    with patch.object(gb.urllib.request, "urlopen", side_effect=_capture):
        gb._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call"}, "sid-42")
    # urllib normalise la casse des entêtes (`add_header` → capitalize) :
    # on compare sans tenir compte de la casse, comme le fait HTTP.
    entetes = {k.lower(): v for k, v in vus["entetes"].items()}
    assert entetes.get("mcp-session-id") == "sid-42", vus["entetes"]


# --- Bug n°2 : la charge utile est suivie d'un pied de page markdown ---------
def test_loads_first_json_ignore_le_pied_de_page_markdown():
    """Second bug : `json.loads` levait « Extra data » → TOUTE réponse jetée."""
    texte = (
        '{\n  "target": {"filePath": "a.kt"},\n  "byDepth": {"1": [{"filePath": "b.kt"}]}\n}\n\n'
        "---\n**Next:** Review d=1 items first (WILL BREAK). READ gitnexus://repo/x/processes."
    )
    objet = gb._loads_first_json(texte)
    assert objet is not None, "le pied de page ne doit pas faire perdre la charge utile"
    assert objet["target"]["filePath"] == "a.kt"


def test_loads_first_json_sur_du_json_propre():
    assert gb._loads_first_json('{"a": 1}') == {"a": 1}


def test_loads_first_json_sur_du_garbage_rend_none():
    for mauvais in ("", "   ", "pas du json", "{cassé", "null"):
        assert gb._loads_first_json(mauvais) is None, mauvais


def test_loads_first_json_refuse_un_tableau():
    """Un tableau n'est pas une charge utile `impact` : on préfère None à un type faux."""
    assert gb._loads_first_json('[1, 2, 3]') is None


# --- Session : cache et ré-ouverture ----------------------------------------
def test_service_session_est_reutilisee():
    appels = []

    def _faux_rpc(payload, session_id):
        appels.append(payload.get("method"))
        if payload.get("method") == "initialize":
            return {"result": {}}, "sid-1"
        return {}, None

    with patch.object(gb, "_rpc", side_effect=_faux_rpc):
        s1 = gb._service_session()
        s2 = gb._service_session()
    assert s1 == s2 == "sid-1"
    assert appels.count("initialize") == 1, appels


def test_service_session_rend_none_si_initialize_echoue():
    with patch.object(gb, "_rpc", return_value=(None, None)):
        assert gb._service_session() is None


def test_une_session_invalidee_est_rouverte():
    """Une session périmée doit être oubliée, pas réessayée indéfiniment."""
    appels = []

    def _faux_rpc(payload, session_id):
        appels.append((payload.get("method"), session_id))
        if payload.get("method") == "initialize":
            return {"result": {}}, f"sid-{len([a for a in appels if a[0]=='initialize'])}"
        return None, None  # tools/call échoue

    with patch.object(gb, "_rpc", side_effect=_faux_rpc):
        with patch.object(gb, "service_enabled", return_value=True):
            gb._service_impact("alert-immo", "X", 2)   # échoue → session oubliée
            gb._service_impact("alert-immo", "X", 2)   # doit ré-ouvrir
    assert [a[0] for a in appels].count("initialize") == 2, appels


# --- _service_impact : tous les chemins de dégradation -----------------------
def test_service_impact_rend_none_si_service_desactive():
    with patch.object(gb, "service_enabled", return_value=False):
        assert gb._service_impact("alert-immo", "X", 2) is None


def test_service_impact_propage_une_erreur_jsonrpc_en_avertissement():
    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", return_value=({"error": {"message": "boom"}}, None)):
                files, avert = gb._service_impact("alert-immo", "X", 2)
    assert files == [] and "boom" in avert


def test_service_impact_sans_charge_utile_exploitable():
    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", return_value=({"result": {"content": []}}, None)):
                files, avert = gb._service_impact("alert-immo", "X", 2)
    assert files == [] and "no exploitable payload" in avert


def test_service_impact_lit_structured_content():
    charge = {"target": {"filePath": "s.kt"}, "byDepth": {}}
    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", return_value=({"result": {"structuredContent": charge}}, None)):
                assert gb._service_impact("alert-immo", "X", 2) == (["s.kt"], None)


def test_url_vide_desactive_le_service():
    """Interrupteur documenté : COORD_GITNEXUS_SERVICE_URL="" coupe la voie service."""
    with patch.object(gb, "SERVICE_URL", ""):
        assert gb.service_enabled() is False


# --- Le cache sous CONCURRENCE : c'est le cas d'usage réel (60 agents) -------
def test_cache_concurrent_n_appelle_qu_une_fois():
    appels = []
    verrou = threading.Lock()

    def _compte(*a, **k):
        with verrou:
            appels.append(a)
        time.sleep(0.02)          # simule la latence du service
        return (["x.kt"], None)

    resultats = []

    def _agent():
        resultats.append(gb.expand_scope("alert-immo", ["X"]))

    with patch.object(gb, "_service_impact", side_effect=_compte):
        fils = [threading.Thread(target=_agent) for _ in range(8)]
        for f in fils:
            f.start()
        for f in fils:
            f.join()

    assert all(r["files"] == ["x.kt"] for r in resultats)
    # Sans le verrou ni le cache, 8 agents = 8 appels. Le contrat est « au plus
    # quelques appels », pas « exactement 1 » (la fenêtre de course reste
    # ouverte entre la lecture et l'écriture) — on vérifie donc l'effet réel.
    assert len(appels) == 1, f"{len(appels)} appels pour 8 agents concurrents (single-flight attendu : 1)"


def test_cache_concurrent_ne_corrompt_pas_le_resultat():
    """Le cache est un dict partagé : sous verrou, il ne doit jamais rendre un mélange."""
    def _repond(symbol):
        return ([f"{symbol}.kt"], None)

    with patch.object(gb, "_service_impact", side_effect=lambda repo, sym, d: _repond(sym)):
        resultats = {}

        def _agent(sym):
            resultats[sym] = gb.expand_scope("alert-immo", [sym])["files"]

        fils = [threading.Thread(target=_agent, args=(f"S{i}",)) for i in range(10)]
        for f in fils:
            f.start()
        for f in fils:
            f.join()
    for i in range(10):
        assert resultats[f"S{i}"] == [f"S{i}.kt"], resultats


# =============================================================================
# P1 (parallélisation + dédoublonnage) et BUDGET GLOBAL — ajoutés le 11/09/2026.
# =============================================================================
def test_dedoublonnage_des_symboles():
    """Un agent qui déclare deux fois le même symbole ne paie qu'une résolution."""
    appels = []

    def _compte(repo, sym, depth):
        appels.append(sym)
        return ([f"{sym}.kt"], None)

    with patch.object(gb, "_service_impact", side_effect=_compte):
        r = gb.expand_scope("alert-immo", ["X", "X", "Y", "X"])
    assert sorted(appels) == ["X", "Y"], appels
    assert r["files"] == ["X.kt", "Y.kt"]


def test_budget_depasse_ne_tente_rien_et_le_dit():
    """Le budget global remplace la somme des timeouts : au-delà, on ne tente plus."""
    appels = []
    with patch.object(gb, "EXPANSION_BUDGET_S", -1.0):
        with patch.object(gb, "_service_impact", side_effect=lambda *a: (appels.append(a), ([], None))[1]):
            r = gb.expand_scope("alert-immo", ["X", "Y"])
    assert appels == [], "le service ne doit pas être appelé hors budget"
    assert len(r["warnings"]) == 2
    assert all("budget" in w for w in r["warnings"]), r["warnings"]


def test_le_repli_cli_est_borne_par_le_budget():
    """Le trou fermé : sans ça, 5 symboles = 5 × 10 s de repli CLI."""
    vus = []

    def _faux_cli(repo, sym, depth, timeout_s=None):
        vus.append(timeout_s)
        return [], None

    with patch.object(gb, "service_enabled", return_value=False):
        with patch.object(gb, "gitnexus_available", return_value=True):
            with patch.object(gb, "_impact_files", side_effect=_faux_cli):
                with patch.object(gb, "EXPANSION_BUDGET_S", 5.0):
                    gb.expand_scope("alert-immo", ["X"])
    assert vus and vus[0] is not None, "le repli CLI doit recevoir un timeout borné"
    assert 0 < vus[0] <= 5.0, vus


def test_l_ordre_des_avertissements_suit_la_declaration():
    """Contrat séquentiel : l'ordre des avertissements suit l'ordre demandé.

    (Un premier jet parallélisait l'expansion et devait donc TRIER les
    avertissements pour rester déterministe. La parallélisation a été mesurée
    puis REVERTÉE — 3,5× plus lente — donc l'ordre naturel revient.)
    """
    def _echoue(repo, sym, depth, deadline=None, index_rev=None):
        return [], f"{sym}: introuvable"

    with patch.object(gb, "_resolve_symbol", side_effect=_echoue):
        r1 = gb.expand_scope("alert-immo", ["C", "A", "B"])
        r2 = gb.expand_scope("alert-immo", ["A", "B", "C"])
    assert r1["warnings"] == ["C: introuvable", "A: introuvable", "B: introuvable"]
    assert r2["warnings"] == ["A: introuvable", "B: introuvable", "C: introuvable"]
    assert r1["files"] == r2["files"] == []


# ---------------------------------------------------------------------------
# Invalidation par la RÉVISION D'INDEX (au lieu d'un TTL deviné)
# ---------------------------------------------------------------------------
def _charge_list_repos(nom="alert-immo", commit="abc123456789", indexed="2026-09-11T10:00:00.000Z"):
    payload = {"repositories": [{"name": nom, "lastCommit": commit, "indexedAt": indexed}]}
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


def test_index_revision_est_lue_depuis_list_repos():
    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", return_value=(_charge_list_repos(), None)):
                assert gb._index_revision("alert-immo") == "abc123456789@2026-09-11T10:00:00.000Z"


def test_index_revision_est_mise_en_cache_court():
    appels = []

    def _compte(payload, session):
        appels.append(payload["params"]["name"])
        return _charge_list_repos(), None

    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", side_effect=_compte):
                gb._index_revision("alert-immo")
                gb._index_revision("alert-immo")
    assert appels == ["list_repos"], appels


def test_index_revision_rend_none_si_depot_inconnu_ou_service_muet():
    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_service_session", return_value="sid"):
            with patch.object(gb, "_rpc", return_value=(_charge_list_repos(nom="autre"), None)):
                assert gb._index_revision("alert-immo") is None
            with patch.object(gb, "_rpc", return_value=(None, None)):
                assert gb._index_revision("alert-immo") is None


def test_une_nouvelle_revision_invalide_le_cache():
    """LE test qui justifie la clé de révision : un index reconstruit ne doit pas
    servir un rayon d'impact périmé, même si le TTL de 300 s n'a pas expiré."""
    revisions = iter(["rev-A", "rev-A", "rev-B"])
    appels = []

    def _compte(repo, sym, depth):
        appels.append((repo, sym, depth))
        return ([f"{sym}-{len(appels)}.kt"], None)

    with patch.object(gb, "service_enabled", return_value=True):
        with patch.object(gb, "_index_revision", side_effect=lambda r: next(revisions)):
            with patch.object(gb, "_service_impact", side_effect=_compte):
                gb.expand_scope("alert-immo", ["X"])   # rev-A → résout et cache
                gb.expand_scope("alert-immo", ["X"])   # rev-A → cache, aucun appel
                r3 = gb.expand_scope("alert-immo", ["X"])  # rev-B → DOIT re-résoudre
    assert len(appels) == 2, f"{len(appels)} appels : le cache n'a pas été invalidé par la révision"
    assert r3["files"] == ["X-2.kt"]


def test_sans_revision_le_cache_retombe_sur_le_ttl():
    """Service muet ⇒ pas de révision : on ne casse pas, on perd juste la précision."""
    appels = []

    def _compte(repo, sym, depth):
        appels.append(sym)
        return (["x.kt"], None)

    with patch.object(gb, "_index_revision", return_value=None):
        with patch.object(gb, "_service_impact", side_effect=_compte):
            gb.expand_scope("alert-immo", ["X"])
            gb.expand_scope("alert-immo", ["X"])
    assert len(appels) == 1, appels
