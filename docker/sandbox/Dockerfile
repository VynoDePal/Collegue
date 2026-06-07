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

# Utilisateur non-root par défaut (le `docker run` peut surcharger --user pour
# matcher l'UID hôte et garder le workspace écrivable/persisté).
RUN useradd --create-home --uid 1000 sandbox

WORKDIR /workspace
USER sandbox

# Pas d'ENTRYPOINT applicatif : l'exécuteur fournit la commande à chaque run.
CMD ["python", "--version"]
