"""
Best-effort bridge to GitNexus for semantic scope expansion.

coord-mcp's default conflict detection compares declared `scope_files` by exact
path overlap. Two agents touching different files that both derive from the same
symbol (e.g. a Controller and a model it imports) are invisible to that check.
If the target repo is indexed by GitNexus, `expand_scope` resolves declared
`scope_symbols` to their blast-radius file set, so conflict detection can compare
on the richer set instead.

TWO PATHS, IN THIS ORDER — mesuré le 11/09/2026 :

  1. SERVICE CHAUD (`http://localhost:8006/api/mcp`, outil `impact`) : 38-78 ms
     par symbole une fois le graphe chargé (1 498 ms au tout premier appel).
  2. CLI (`gitnexus impact …`) : 367-1 540 ms par symbole — un processus complet
     à chaque appel. Conservé en SECOURS, jamais en chemin principal.

Un `subprocess.run` par symbole sur un chemin chaud est une inversion
d'architecture : le coût mesuré est 10 à 40× celui du service déjà lancé, et le
`timeout` de 10 s qui l'accompagne plafonne la dégradation au lieu de la
supprimer. Le CLI n'est donc plus tenté en premier.

ÉQUIVALENCE : avec le bon nom de paramètre (`maxDepth`), les deux voies rendent
EXACTEMENT le même ensemble de fichiers, vérifié sur 5 symboles et 3 profondeurs.
Le service n'est donc pas un « résultat différent » : c'est le même, plus vite.

CACHE (P2) : le blast-radius d'un symbole bouge lentement (il suit les commits,
pas les appels). Un cache TTL par `(repo_alias, symbol, depth)` fait tomber le
coût d'un appel répété à ~0 ms. Verrou explicite : depuis que `server.py` exécute
les outils via `asyncio.to_thread`, plusieurs agents peuvent appeler ce module en
parallèle.

Never raises: any failure (service absent, CLI absent, repo not indexed, symbol
not found, timeout) degrades to an empty expansion with a warning. Semantic
expansion is a pure enhancement on top of file-path matching, never a hard
dependency — checkin must always succeed even without GitNexus installed.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

GITNEXUS_TIMEOUT_S = 10

# P0 — service GitNexus chaud. Chaîne vide ⇒ voie service désactivée.
SERVICE_URL = os.environ.get("COORD_GITNEXUS_SERVICE_URL", "http://localhost:8006/api/mcp")
# Budget du chemin chaud. On pose un BUDGET, pas un timeout de 10 s : au-delà,
# on préfère un avertissement lisible à une attente qui bloque l'appelant.
SERVICE_TIMEOUT_S = 2.0
# P2 — durée de validité d'une entrée de cache.
CACHE_TTL_S = 300.0

# INVALIDATION EXACTE. Un TTL est une DEVINETTE : si l'index GitNexus est
# reconstruit (nouveau commit analysé), un cache à 300 s sert un rayon d'impact
# PÉRIMÉ pendant jusqu'à 5 minutes — et un conflit peut être manqué. GitNexus
# expose la révision de son index (`list_repos` → `lastCommit` + `indexedAt`) :
# c'est la seule clé d'invalidation correcte. On la relit au plus toutes les 30 s.
INDEX_REV_TTL_S = 30.0

# Parallélisation : MESURÉE PUIS REVERTÉE (voir `expand_scope`). Conservée ici
# comme trace de ce qui a été essayé — la passer à >1 dégrade le temps réel.
EXPANSION_MAX_WORKERS = 1

# BUDGET GLOBAL d'expansion, en secondes. Sans lui, le pire cas d'un `checkin` est
# N × (2 s de service + 10 s de repli CLI) : avec 5 symboles, ~60 s d'attente
# possible pour un appel de coordination. On pose un BUDGET, pas une somme de
# timeouts — c'est la différence entre borner la dégradation et l'espérer.
EXPANSION_BUDGET_S = 5.0

_CACHE: dict[tuple[str, str, int], tuple[float, list[str], str | None]] = {}
_CACHE_LOCK = threading.Lock()

_SESSION: dict[str, str | None] = {"id": None}
_SESSION_LOCK = threading.Lock()


def gitnexus_available() -> bool:
    """Le CLI GitNexus est-il présent ? (voie de secours uniquement.)"""
    return shutil.which("gitnexus") is not None


def service_enabled() -> bool:
    """Le service chaud est-il configuré ? (indépendant de sa joignabilité.)"""
    return bool(SERVICE_URL.strip())


# Verrous « single-flight » : un par clé de cache. Sans eux, N agents qui
# déclarent le même symbole au même instant ratent TOUS le cache (lecture avant
# écriture) et paient chacun la résolution complète. Mesuré le 11/09/2026 :
# 8 fils concurrents → 8 appels au service, soit exactement le scénario
# multi-agents que ce cache est censé amortir. Trouvé par un test de concurrence,
# pas par relecture.
_INFLIGHT: dict[tuple[str, str, int], threading.Lock] = {}
_INDEX_REV: dict[str, tuple[float, str]] = {}


def _reset_cache() -> None:
    """Vide le cache — utilisé par les tests pour garantir la déterminisme."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
        _INDEX_REV.clear()
    with _SESSION_LOCK:
        _SESSION["id"] = None


def _verrou_de(cle: tuple[str, str, int]) -> threading.Lock:
    """Verrou dédié à une clé de cache (créé à la demande, sous `_CACHE_LOCK`)."""
    with _CACHE_LOCK:
        return _INFLIGHT.setdefault(cle, threading.Lock())


# ---------------------------------------------------------------------------
# Voie 1 — service chaud (MCP « streamable http »)
# ---------------------------------------------------------------------------
def _rpc(payload: dict, session_id: str | None) -> tuple[dict | None, str | None]:
    """Un aller-retour JSON-RPC. Renvoie (json|None, nouvelle_session|None).

    Jamais d'exception : toute erreur devient (None, None) et l'appelant dégrade.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    request = urllib.request.Request(
        SERVICE_URL, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=SERVICE_TIMEOUT_S) as response:
            new_session = response.headers.get("mcp-session-id")
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None, None

    # Le transport « streamable http » répond en SSE : les charges utiles sont
    # préfixées `data: `. Un premier jet de ce code cherchait une ligne
    # commençant par `{` et ne trouvait donc JAMAIS rien (session=ÉCHEC) —
    # attrapé en décomposant le coût, pas en lisant le code.
    for line in reversed(body.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line), new_session
        except json.JSONDecodeError:
            continue
    # Certains serveurs répondent en JSON pur (pas de SSE).
    try:
        return json.loads(body), new_session
    except json.JSONDecodeError:
        return None, new_session


def _service_session() -> str | None:
    """Ouvre (ou réutilise) une session MCP sur le service chaud."""
    with _SESSION_LOCK:
        if _SESSION["id"]:
            return _SESSION["id"]
    data, session_id = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coord-mcp", "version": "1"},
            },
        },
        None,
    )
    if not data or not session_id:
        return None
    _rpc(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id,
    )
    with _SESSION_LOCK:
        _SESSION["id"] = session_id
    return session_id


def _loads_first_json(text: str) -> dict | None:
    """Parse le PREMIER objet JSON et ignore ce qui suit.

    Le service `impact` ajoute un pied de page markdown APRÈS le JSON
    (« --- \n**Next:** Review d=1 items first… ») : `json.loads` lève alors
    `Extra data`, et un premier jet de ce code perdait donc toute la réponse.
    Le CLI, lui, n'a pas ce défaut (son pied de page part sur stderr) — d'où
    l'écart de comportement entre les deux voies, invisible à la lecture.
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.strip())
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _index_revision(repo_alias: str) -> str | None:
    """Révision de l'index GitNexus pour ce dépôt : `lastCommit@indexedAt`.

    Sert de clé d'invalidation EXACTE au cache de blast-radius. Renvoie None si
    le service est indisponible — l'appelant retombe alors sur le seul TTL, ce
    qui est moins bon mais jamais faux au point de lever.
    """
    if not service_enabled():
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        connu = _INDEX_REV.get(repo_alias)
        if connu and connu[0] > now:
            return connu[1]

    session = _service_session()
    if not session:
        return None
    data, _ = _rpc(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_repos", "arguments": {}}},
        session,
    )
    if not data:
        return None
    result = data.get("result") or {}
    charge = None
    for bloc in result.get("content") or []:
        charge = _loads_first_json((bloc or {}).get("text") or "")
        if charge is not None:
            break
    if charge is None and result.get("structuredContent"):
        charge = result["structuredContent"]
    if not isinstance(charge, dict):
        return None

    for depot in charge.get("repositories") or []:
        if (depot or {}).get("name") == repo_alias:
            rev = f"{(depot.get('lastCommit') or '?')[:12]}@{depot.get('indexedAt') or '?'}"
            with _CACHE_LOCK:
                _INDEX_REV[repo_alias] = (now + INDEX_REV_TTL_S, rev)
            return rev
    return None


def _extract_files(data: dict) -> list[str]:
    """Extrait `target.filePath` + `byDepth[*][*].filePath` de la réponse `impact`."""
    files: set[str] = set()
    target_path = (data.get("target") or {}).get("filePath")
    if target_path:
        files.add(target_path)
    for depth_bucket in (data.get("byDepth") or {}).values():
        for entry in depth_bucket or []:
            path = (entry or {}).get("filePath")
            if path:
                files.add(path)
    return sorted(files)


def _service_impact(repo_alias: str, symbol: str, depth: int) -> tuple[list[str], str | None] | None:
    """Interroge le service chaud. `None` ⇒ service inutilisable, passer au CLI.

    Renvoie `(files, warning)` quand le service a RÉPONDU (warning non nul si la
    réponse ne contient pas d'exploitable — ce n'est pas un échec du service).
    """
    if not service_enabled():
        return None

    session_id = _service_session()
    if not session_id:
        with _SESSION_LOCK:
            _SESSION["id"] = None  # session périmée : on réessaiera au prochain appel
        return None

    data, _ = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "impact",
                "arguments": {
                    "target": symbol,
                    "repo": repo_alias,
                    "direction": "downstream",
                    # ⚠️ `maxDepth`, PAS `depth`. Le schéma de l'outil MCP déclare
                    # `maxDepth` (défaut 3, borné 1–32) ; un argument `depth` est
                    # simplement IGNORÉ — sans erreur — et le serveur applique son
                    # défaut. Mesuré le 11/09/2026 sur AnalysisRunStore :
                    #   CLI --depth 1/2/3      → 5 / 42 / 47 entrées
                    #   service depth:1/2/3    → 47 / 47 / 47   (argument ignoré)
                    #   service maxDepth:1/2/3 → 5 / 42 / 47   (équivalence EXACTE)
                    # C'est ce qui faisait croire à une « divergence » entre les deux
                    # voies : c'était un nom d'argument faux, pas une traversée
                    # différente. Un test verrouille le nom ci-dessous.
                    "maxDepth": depth,
                },
            },
        },
        session_id,
    )
    if not data:
        with _SESSION_LOCK:
            _SESSION["id"] = None
        return None

    if data.get("error"):
        return [], f"{symbol}: {data['error']}"

    result = data.get("result") or {}
    # MCP encapsule la charge utile dans content[].text (JSON sous forme de texte).
    payload: dict | None = None
    for block in result.get("content") or []:
        text = (block or {}).get("text")
        if not text:
            continue
        parsed = _loads_first_json(text)
        if parsed is not None:
            payload = parsed
            break
    if result.get("structuredContent"):
        payload = result["structuredContent"]
    if payload is None:
        return [], f"{symbol}: service impact returned no exploitable payload"

    if payload.get("error"):
        return [], f"{symbol}: {payload['error']}"
    return _extract_files(payload), None


# ---------------------------------------------------------------------------
# Voie 2 — CLI (SECOURS)
# ---------------------------------------------------------------------------
def _impact_files(repo_alias: str, symbol: str, depth: int, timeout_s: float | None = None) -> tuple[list[str], str | None]:
    """Run `gitnexus impact <symbol> -r <repo_alias>` and extract affected file paths.

    Returns (files, warning). warning is None on success.
    """
    try:
        result = subprocess.run(
            ["gitnexus", "impact", symbol, "-r", repo_alias,
             "--depth", str(depth), "--direction", "downstream"],
            capture_output=True,
            text=True,
            timeout=timeout_s if timeout_s is not None else GITNEXUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [], f"{symbol}: gitnexus impact timed out after {timeout_s if timeout_s is not None else GITNEXUS_TIMEOUT_S}s"

    if not result.stdout.strip():
        return [], f"{symbol}: gitnexus impact returned no output ({result.stderr.strip()[:200]})"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], f"{symbol}: gitnexus impact returned invalid JSON"

    if data.get("error"):
        return [], f"{symbol}: {data['error']}"

    return _extract_files(data), None


# ---------------------------------------------------------------------------
def _resolve_symbol(repo_alias: str, symbol: str, depth: int, deadline: float | None = None,
                    index_rev: str | None = None) -> tuple[list[str], str | None]:
    """Un symbole : cache → service chaud → CLI. Jamais d'exception."""
    # La révision d'index fait partie de la CLÉ : une reconstruction rend les
    # entrées précédentes inatteignables, sans attendre l'expiration du TTL.
    key = (f"{repo_alias}@{index_rev or '?'}", symbol, depth)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return list(cached[1]), cached[2]

    verrou = _verrou_de(key)
    with verrou:
        # Re-vérification SOUS le verrou : un autre fil a pu résoudre pendant
        # qu'on attendait. C'est ce qui fait passer 8 agents de 8 appels à 1.
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > now:
                return list(cached[1]), cached[2]
        return _resoudre_et_cacher(key, repo_alias, symbol, depth, now, deadline)


def _resoudre_et_cacher(key, repo_alias, symbol, depth, now, deadline=None):
    # Budget global : au-delà de l'échéance, on ne tente plus rien et on le DIT.
    if deadline is not None and time.monotonic() >= deadline:
        return [], f"{symbol}: budget d'expansion dépassé — symbole non résolu"
    files, warning = [], None
    try:
        answer = _service_impact(repo_alias, symbol, depth)
    except Exception as exc:  # noqa: BLE001 — best-effort par conception
        answer = None
        warning = f"{symbol}: service impact failed ({type(exc).__name__}: {exc})"

    if answer is not None:
        files, service_warning = answer
        warning = warning or service_warning
    elif gitnexus_available():
        restant = None if deadline is None else max(0.5, deadline - time.monotonic())
        files, warning = _impact_files(repo_alias, symbol, depth, timeout_s=restant)
    else:
        # Ni service ni CLI : on garde le message historique, attendu par les tests.
        return [], "gitnexus CLI not found in PATH — semantic scope expansion skipped"

    with _CACHE_LOCK:
        _CACHE[key] = (now + CACHE_TTL_S, list(files), warning)
    return files, warning


def expand_scope(repo_alias: str, symbols: list[str], depth: int = 2) -> dict:
    """Resolve declared symbols to their blast-radius file set.

    Returns {"files": [...], "warnings": [...]}. Best-effort — never raises,
    empty symbols or missing GitNexus both degrade to an empty, warning-only result.
    """
    if not symbols:
        return {"files": [], "warnings": []}

    if not service_enabled() and not gitnexus_available():
        return {"files": [], "warnings": ["gitnexus CLI not found in PATH — semantic scope expansion skipped"]}

    # Dédoublonnage : un agent qui déclare deux fois le même symbole ne doit pas
    # payer deux résolutions.
    uniques = list(dict.fromkeys(symbols))
    deadline = time.monotonic() + EXPANSION_BUDGET_S
    index_rev = _index_revision(repo_alias)

    # SÉQUENTIEL, et c'est un CHOIX MESURÉ. La parallélisation a été implémentée,
    # puis REVERTÉE le 11/09/2026 : sur le service GitNexus réel, 5 symboles en
    # 4 fils donnent une médiane de 3 172 ms contre 914 ms en séquentiel — soit
    # 3,5× PLUS LENT. Le service sérialise en interne ; les fils n'ajoutent que
    # la contention et le surcoût de contexte. Une parallélisation « évidente »
    # qui n'est pas mesurée est une régression déguisée.
    all_files: set[str] = set()
    warnings: list[str] = []
    for symbole in uniques:
        try:
            files, warning = _resolve_symbol(repo_alias, symbole, depth, deadline, index_rev)
        except Exception as exc:  # noqa: BLE001 — best-effort par conception
            files, warning = [], f"{symbole}: résolution interrompue ({type(exc).__name__}: {exc})"
        all_files.update(files)
        if warning:
            warnings.append(warning)

    return {"files": sorted(all_files), "warnings": warnings}
