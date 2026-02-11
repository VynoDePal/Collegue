# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Installation des outils de build temporaires
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime minimal
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=4121
ENV HEALTH_PORT=4122
ENV PATH=/home/collegue/.local/bin:$PATH

WORKDIR /app

# Création utilisateur non-root
RUN groupadd -r collegue && useradd -r -g collegue collegue

# Copie des dépendances depuis le builder
COPY --from=builder /root/.local /home/collegue/.local

# Copie des fichiers de configuration
COPY --chown=collegue:collegue requirements.txt entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# Copie du code source
COPY --chown=collegue:collegue ./collegue ./collegue
COPY --chown=collegue:collegue ./skills ./skills

EXPOSE ${PORT} ${HEALTH_PORT}

USER collegue

CMD ["./entrypoint.sh"]
