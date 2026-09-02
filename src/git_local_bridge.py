"""Adaptateur `RepoIdentityResolver` — lit l'identité du dépôt dans git, en local.

Marche pour TOUTE forge — GitHub, Forgejo, GitLab, Gitea, Bitbucket — parce
qu'elles partagent la convention d'URL `hôte/propriétaire/dépôt`. Une seule
expression régulière suffit donc là où il aurait fallu un client par forge.

Ne sort jamais du disque : pas de réseau, pas d'authentification, pas de jeton à
faire expirer. C'est ce qui en fait le résolveur par défaut — et, dans les faits,
le seul nécessaire (mesuré à 13 ms contre 643 ms pour `gh` sur la même machine).
"""

from __future__ import annotations

import re
import subprocess

# Un résolveur qui dépasse ce délai est traité comme absent. 3 s suffisent
# largement à une commande locale ; au-delà, c'est que le chemin est réseau et
# qu'il vaut mieux passer au suivant que de faire attendre un agent.
TIMEOUT_S = 3

# Couvre https://hôte/propriétaire/dépôt(.git)(/) et git@hôte:propriétaire/dépôt(.git)
_MOTIF = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


class GitLocalBridge:
    """`git remote get-url` — le chemin court, hors ligne."""

    nom = "git-local"

    def resoudre(self, repo_path: str) -> str | None:
        for remote in ("origin", "upstream"):
            try:
                r = subprocess.run(
                    ["git", "remote", "get-url", remote],
                    capture_output=True,
                    text=True,
                    cwd=repo_path,
                    timeout=TIMEOUT_S,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
            if r.returncode != 0:
                continue
            m = _MOTIF.search(r.stdout.strip())
            if m:
                return f"{m.group(1)}/{m.group(2)}"
        return None
