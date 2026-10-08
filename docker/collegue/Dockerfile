# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Installation des outils de build temporaires
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Dépendances VERROUILLÉES (pyproject.toml -> locks/runtime.txt, hachées, universelles 3.11/3.12) :
# --require-hashes refuse tout paquet absent du verrou ou à l'empreinte différente ; --no-deps car la
# fermeture transitive est déjà entièrement épinglée. Le serveur MCP et le dashboard partagent cette image.
COPY locks/runtime.txt locks/runtime.txt
RUN pip install --user --no-cache-dir --require-hashes --no-deps -r locks/runtime.txt

# Stage 2: Runtime minimal
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=4121
ENV HEALTH_PORT=4122
ENV PATH=/home/collegue/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV COLLEGUE_HOME=/app/.collegue

WORKDIR /app

# Création utilisateur non-root
RUN groupadd -r collegue && useradd -r -g collegue collegue

# Installation de curl pour le healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copie des dépendances depuis le builder
COPY --from=builder /root/.local /home/collegue/.local

# Copie des fichiers de configuration
COPY --chown=collegue:collegue entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# Copie du code source : le paquet embarque ses ressources (skills, graines de prompts, règles IaC,
# migrations Alembic) résolues par importlib.resources, sans /app/skills ni répertoire courant.
COPY --chown=collegue:collegue ./collegue ./collegue

EXPOSE ${PORT} ${HEALTH_PORT}

# Répertoire des données persistantes, inscriptible par l'utilisateur non-root
RUN mkdir -p /app/.collegue && chown -R collegue:collegue /app/.collegue

USER collegue

CMD ["./entrypoint.sh"]
