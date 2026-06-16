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

# Navigateur headless pour le gate E2E (actif par défaut, #503 suivi) : Playwright + chromium
# + ses libs système (libnss3, libatk… via --with-deps, root). Le gate démarre le
# backend, sert le frontend buildé (base d'URL backend injectée) et pilote l'UI
# RÉELLE dans chromium pour attraper les ruptures front<->back (contrat/préfixe/
# CORS/base d'URL) qu'aucune autre passe n'exerce. PLAYWRIGHT_BROWSERS_PATH partagé
# et lisible pour que l'utilisateur `sandbox` (non-root) trouve le binaire au run.
# N.B. NON construit par la CI (elle build docker/collegue/Dockerfile) — image
# d'exécution réelle uniquement.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python -m pip install --no-cache-dir playwright \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

# Utilisateur non-root par défaut (le `docker run` peut surcharger --user pour
# matcher l'UID hôte et garder le workspace écrivable/persisté).
RUN useradd --create-home --uid 1000 sandbox

WORKDIR /workspace
USER sandbox

# Pas d'ENTRYPOINT applicatif : l'exécuteur fournit la commande à chaque run.
CMD ["python", "--version"]
