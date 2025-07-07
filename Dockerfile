# 1. Utiliser une image Python officielle comme image de base
FROM python:3.10-slim

# 2. Définir des variables d'environnement
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PORT 4121
# Port pour le serveur de santé dédié
ENV HEALTH_PORT 4122

# 3. Définir le répertoire de travail dans le conteneur
WORKDIR /app

# 4. Met à jour les paquets et installe les dépendances système si nécessaire
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc libffi-dev musl-dev && rm -rf /var/lib/apt/lists/*

# 5. Copier les fichiers de dépendances et les scripts d'entrée/santé
COPY requirements.txt .
COPY entrypoint.sh .
COPY ./collegue/health_server.py ./collegue/health_server.py

# 6. Rendre le script d'entrée exécutable
RUN chmod +x ./entrypoint.sh

# 7. Installer les dépendances Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 8. Copier le reste du code de l'application dans le répertoire de travail
COPY ./collegue ./collegue

# 9. Exposer les ports
# Port de l'application principale
EXPOSE ${PORT}
# Port du serveur de santé
EXPOSE ${HEALTH_PORT}

# 10. Définir la commande pour exécuter l'application via le script d'entrée
CMD ["./entrypoint.sh"]
