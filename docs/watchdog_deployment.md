# Déploiement du Watchdog Autonome

Le Watchdog est un service autonome qui surveille Sentry et crée des Pull Requests sur GitHub pour corriger les erreurs.

## Pré-requis

### Variables d'environnement
Ajoutez ces variables à votre fichier `.env` ou à votre configuration de déploiement (Coolify, Portainer, etc.) :

```bash
# Sentry
SENTRY_ORG=votre-organisation-sentry
SENTRY_AUTH_TOKEN=votre-token-sentry  # Scopes: project:read, issue:read, event:read, org:read

# GitHub
GITHUB_TOKEN=votre-pat-github      # Scopes: repo (pour créer branches/PRs)
# GITHUB_OWNER=...                 # Optionnel: force le propriétaire du repo (sinon auto-détecté)
```

## Déploiement

Le service est intégré dans `docker-compose.yml` sous le nom `collegue-watchdog`.

### Lancement
```bash
docker-compose up -d collegue-watchdog
```
Ou pour tout relancer :
```bash
docker-compose up -d --build
```

## Vérification

Pour voir si le watchdog tourne et détecte les projets :
```bash
docker-compose logs -f collegue-watchdog
```

Vous devriez voir des logs comme :
```
INFO - 🔍 Démarrage du cycle de Self-Healing Multi-Projets...
INFO - 📡 Récupération des données pour l'org: votre-org
INFO - ✅ X projets et Y dépôts liés trouvés.
```
