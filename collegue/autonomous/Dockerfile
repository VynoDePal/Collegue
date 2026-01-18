FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances minimales pour le watchdog
COPY collegue/autonomous/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source nécessaire (package collegue)
# On suppose que le contexte de build est la racine du projet
COPY collegue/ ./collegue/

# Configuration du PYTHONPATH pour que les imports 'from collegue...' fonctionnent
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Lancement du watchdog
CMD ["python", "-m", "collegue.autonomous.watchdog"]
