# Guide de Configuration Multi-Projets Sentry

Collègue supporte la gestion de multiples projets Sentry (Python, JavaScript, etc.) grâce à la détection automatique des fichiers de configuration dans vos dépôts.

Cette approche permet de définir une organisation, un projet et même un token spécifique **par dépôt**, sans changer la configuration globale de Collègue.

## Fichiers Supportés

L'agent recherche automatiquement les fichiers suivants à la racine de votre dépôt :

### 1. `.sentryclirc` (Format INI Standard)
C'est le format standard utilisé par Sentry CLI.

```ini
[defaults]
url = https://sentry.io
org = mon-organisation
project = mon-projet-python

[auth]
token = sntry_votre_token_specifique_projet
```

### 2. `sentry.properties` (Format Java Properties)
Souvent utilisé dans les projets Java, Android ou React Native.

```properties
defaults.url=https://sentry.io
defaults.org=mon-organisation
defaults.project=mon-projet-react
auth.token=sntry_votre_token_specifique_projet
```

## Fonctionnement du Workflow Agentique

Lorsqu'il analyse une erreur (Workflow "Debug & Fix"), l'agent suit ce protocole :

1. **Détection** : Il vérifie si un fichier de configuration existe à la racine du dépôt.
2. **Parsing** : Il extrait les valeurs (`org`, `project`, `token`).
3. **Utilisation** : Il utilise ces valeurs pour interroger l'API Sentry spécifique à ce projet.

## Avantages

- **Multi-Utilisateurs** : Chaque développeur peut avoir son propre token dans son dépôt (attention à ne pas commiter les secrets, utilisez `.gitignore` et des templates !).
- **Multi-Langages** : Un dépôt Python et un dépôt Node.js peuvent pointer vers des projets Sentry différents.
- **Isolation** : Pas besoin de partager un token "Global Admin" dans les variables d'environnement de Collègue.

## Sécurité

> ⚠️ **ATTENTION** : Ne commitez JAMAIS de tokens Sentry réels dans votre dépôt Git public !

**Bonnes pratiques :**
1. Ajoutez `.sentryclirc` et `sentry.properties` à votre `.gitignore`.
2. Créez un fichier modèle `sentry.properties.example` dans le dépôt.
3. Laissez chaque développeur créer son propre fichier de configuration localement.
