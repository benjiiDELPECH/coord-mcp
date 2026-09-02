"""Adaptateur `RepoIdentityResolver` — `gh repo view`. REPLI SEULEMENT.

Conservé, mais déclassé derrière `GitLocalBridge`. Deux raisons, mesurées le
02.09.2026 :

  LENTEUR    643 ms sur une machine saine, et jusqu'à 300 s quand la forge
             interrogée n'est pas celle du dépôt — ce qui est le cas ici, les
             dépôts étant sur Forgejo. `gh` cherche, ne trouve pas, expire.

  FAUSSETÉ   sur alert-immo, `gh` rend `benjiiDELPECH/alert-immo` (le miroir
             GitHub) là où le dépôt réel est `benjamin/alert-immo`. Les éléments
             de travail se retrouvaient enregistrés sous un mauvais nom de
             dépôt, et deux agents en collision pouvaient être classés ailleurs.

Il ne s'exécute donc que si le remote est introuvable localement — situation qui
ne se produit pas sur un dépôt git valide. Le timeout reste court : c'est cet
appel qui rendait `checkout_work` à 311 s.
"""

from __future__ import annotations

import json
import subprocess

TIMEOUT_S = 3


class GitHubCliBridge:
    """`gh repo view --json nameWithOwner` — jamais en premier."""

    nom = "github-cli"

    def resoudre(self, repo_path: str) -> str | None:
        try:
            r = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner"],
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout).get("nameWithOwner")
        except (json.JSONDecodeError, AttributeError):
            return None
