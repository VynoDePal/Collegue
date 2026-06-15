# Image d'exécution du sandbox isolé (C8, brief §4.4/§6).
# Stack cible des projets générés : Web = backend Python + frontend JS/TS (§8).
# Lancée par collegue/sandbox/executor.py via `docker run` durci (réseau coupé,
# capabilities droppées, root FS read-only, utilisateur non-root, workspace seul
# chemin monté). N'exécute JAMAIS de code sur l'hôte.

FROM python:3.12-slim

# Node.js + npm pour le frontend JS/TS ; outils de build minimaux pour les deps
# Python natives. Nettoyage du cache apt pour garder l'image légère.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Runner de tests du gate qualité, ÉPINGLÉ (pytest<9). pytest 9.0 a retiré
# `FixtureDef` du namespace top-level, ce qui casse `from pytest import FixtureDef`
# de pytest-asyncio<0.24 (que les projets générés pinnent couramment, ex. 0.23.6) :
# ImportError à la collecte → toute tâche à tests échoue au gate (constaté run v6
# après un rebuild qui a tiré pytest 9.x). On fige le runner sur 8.x + un
# pytest-asyncio aligné sur l'écosystème projet. Pré-installé (vs deps du projet)
# pour que le gate dispose d'un runner même si le projet ne le déclare pas.
RUN python -m pip install --no-cache-dir "pytest>=8.1,<9" "pytest-asyncio==0.23.6"

# Utilisateur non-root par défaut (le `docker run` peut surcharger --user pour
# matcher l'UID hôte et garder le workspace écrivable/persisté).
RUN useradd --create-home --uid 1000 sandbox

WORKDIR /workspace
USER sandbox

# Pas d'ENTRYPOINT applicatif : l'exécuteur fournit la commande à chaque run.
CMD ["python", "--version"]
