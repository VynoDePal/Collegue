# Guide de Configuration Sentry pour Collègue MCP

Pour permettre au workflow "Debug & Fix" d'analyser vos erreurs, Collègue a besoin d'accéder à votre compte Sentry.

## 1. Prérequis
- Un compte [Sentry.io](https://sentry.io) (ou une instance self-hosted).
- Des droits d'admin ou de manager pour créer des tokens d'intégration.

## 2. Création du Token d'API

Il y a deux façons de créer un token. La méthode recommandée est via une **Intégration Interne**.

### Méthode A : Intégration Interne (Recommandé)
Cette méthode crée un token lié à l'organisation, idéal pour un bot/outil.

1. Allez dans **Settings** > **Integrations** > **Developer Extensions**.
2. Cliquez sur **Create New Integration**.
3. Choisissez **Internal Integration** et cliquez sur **Next**.
4. Nommez l'intégration (ex: `Collegue-MCP`).
5. Configurez les **Permissions** (Scopes) suivantes :
   - **Issues & Events** : `Read`
   - **Projects** : `Read`
   - **Releases** : `Read`
   - **Organization** : `Read` (optionnel, pour lister les projets automatiquement)
6. Cliquez sur **Save**.
7. Copiez le **Token** généré (commence souvent par `sntry_`).

### Méthode B : User Auth Token (Développement)
Pour un test rapide personnel.

1. Allez dans **User Settings** (votre avatar) > **API Tokens**.
2. Cliquez sur **Create New Token**.
3. Cochez les scopes suivants : `event:read`, `issue:read`, `project:read`, `org:read`.
4. Créez le token.

## 3. Configuration dans Collègue

Vous devez fournir ce token et le slug de votre organisation à Collègue.

### Option A : Configuration MCP (Recommandé pour Windsurf/Cursor)
Ajoutez ces lignes dans votre fichier de configuration MCP (`mcp_config.json` ou via l'interface de votre IDE) :

```json
{
  "env": {
    "SENTRY_AUTH_TOKEN": "votre_token_ici",
    "SENTRY_ORG": "slug_de_votre_organisation"
  }
}
```

> **Note** : Le `slug` de l'organisation se trouve dans l'URL Sentry : `sentry.io/organizations/{slug}/...`

### Option B : Fichier `.env`
Si vous exécutez le serveur manuellement, ajoutez au fichier `.env` à la racine :

```bash
SENTRY_AUTH_TOKEN=votre_token_ici
SENTRY_ORG=votre_slug_org
# Optionnel : URL Sentry si self-hosted
# SENTRY_URL=https://sentry.mon-entreprise.com
```

## 4. Validation

Pour vérifier que la connexion fonctionne :

1. Démarrez Collègue.
2. Demandez à l'agent : *"Liste mes projets Sentry"*.
3. S'il répond avec la liste, la configuration est réussie !

## 5. Dépannage

- **Erreur 401 (Unauthorized)** : Token invalide. Vérifiez qu'il n'a pas expiré.
- **Erreur 403 (Forbidden)** : Scopes manquants. Vérifiez que le token a bien les droits `Read` sur Issues et Projects.
- **Projet introuvable** : Vérifiez que l'utilisateur associé au token a bien accès au projet spécifique dans Sentry.
