# MCP Fixes Documentation

## Problèmes Résolus

### 1. Problème de l'Endpoint de Santé (/_health)

**Problème**: L'endpoint `/_health` retournait une erreur 404 lorsqu'il était accédé via nginx.

**Cause**: La configuration nginx redirigait les requêtes vers le backend principal sur le port 4121, alors que le serveur de santé fonctionne sur le port 4122.

**Solution**: Mise à jour de la configuration nginx pour rediriger les requêtes `/_health` vers le serveur de santé sur le port 4122.

```nginx
# Endpoint de santé spécifique - redirige vers le serveur de santé sur le port 4122
location /_health {
    proxy_pass http://collegue-app:4122/_health;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### 2. Problème de Validation des ID de Session

**Problème**: Le serveur ne validait pas correctement les ID de session fournis par le client, ce qui provoquait l'erreur "No valid session ID provided".

**Cause**: Les ID de session pouvaient être fournis de différentes manières (headers, URL, corps de la requête), mais le serveur ne les acceptait pas tous.

**Solution**: Mise à jour de la configuration nginx pour extraire les ID de session de différentes sources et les transmettre au backend de manière cohérente.

```nginx
# Initialiser la variable session_id
set $session_id "";

# Extraction du session_id depuis l'URL si présent
if ($arg_session_id) {
    set $session_id $arg_session_id;
}
# Sinon, utiliser le header X-Session-ID s'il existe
if ($http_x_session_id) {
    set $session_id $http_x_session_id;
}
# Ou le header MCP-Session-ID s'il existe
if ($http_mcp_session_id) {
    set $session_id $http_mcp_session_id;
}

# Transmission du session ID sous différents formats pour assurer la compatibilité
proxy_set_header X-Session-ID $session_id;
proxy_set_header MCP-Session-ID $session_id;
proxy_set_header x-session-id $session_id;
```

## Comment Tester les Changements

> **Note importante**: Les tests suivants nécessitent un environnement Docker fonctionnel avec docker-compose installé. Si vous n'avez pas accès à cet environnement, assurez-vous que les modifications de configuration sont correctement appliquées avant de déployer en production.

### Test de l'Endpoint de Santé

1. Redémarrer les services Docker:
   ```bash
   docker-compose down
   docker-compose up -d
   ```

2. Vérifier que l'endpoint de santé fonctionne:
   ```bash
   curl http://localhost:8088/_health
   ```
   
   Vous devriez recevoir une réponse 200 OK avec le contenu `{"status":"ok"}`.

### Vérification des Configurations

Si vous ne pouvez pas redémarrer les conteneurs Docker, vérifiez que:

1. Le fichier `nginx/nginx.conf` contient la configuration mise à jour pour l'endpoint `/_health`:
   ```nginx
   location /_health {
       proxy_pass http://collegue-app:4122/_health;
       # ...autres headers...
   }
   ```

2. Le serveur de santé est configuré pour écouter sur le port 4122 dans `collegue/health_server.py`.

### Test de la Validation des ID de Session

1. Exécuter le script de test MCP:
   ```bash
   python3 test_mcp_connection.py
   ```

2. Vérifier que l'initialisation MCP fonctionne correctement et qu'un ID de session est généré.

3. Vérifier que les requêtes suivantes utilisant cet ID de session sont acceptées par le serveur.

## Configurations Recommandées pour les Clients MCP

### 1. Configuration HTTP Directe

Pour les clients MCP qui se connectent directement au serveur via HTTP, utilisez la configuration suivante:

```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/",
    "headers": {
      "Accept": "application/json, text/event-stream",
      "Content-Type": "application/json"
    },
    "transport": "streamable-http"
  }
}
```

### 2. Configuration NPX (Nouvelle Approche)

Une nouvelle approche utilisant NPX pour exécuter le client Collegue est maintenant disponible dans le dossier `bin`:

```json
{
  "collegue": {
    "command": "npx",
    "args": [
      "-y",
      "@modelcontextprotocol/collegue"
    ]
  }
}
```

Cette configuration utilise NPX pour exécuter le package `@modelcontextprotocol/collegue`, qui agit comme un client local communiquant avec le serveur MCP.

#### Avantages de l'Approche NPX

- Gestion automatique des sessions et des reconnexions
- Meilleure gestion des erreurs et des timeouts
- Possibilité d'utiliser des fonctionnalités avancées du client
- Compatibilité avec les outils MCP qui supportent l'exécution de commandes

#### Comment Utiliser la Configuration NPX

1. Assurez-vous que Node.js et NPM sont installés sur votre système
2. Copiez le fichier `bin/mcp.json` dans le répertoire approprié pour votre outil MCP
3. L'outil MCP utilisera automatiquement cette configuration pour démarrer le client Collegue

Pour plus d'informations, consultez le fichier `bin/README.md`.

## Notes Supplémentaires

- Les ID de session peuvent être fournis de différentes manières:
  - Dans l'URL comme paramètre de requête: `?session_id=<ID>`
  - Dans le header HTTP `X-Session-ID`
  - Dans le header HTTP `MCP-Session-ID`
  - Dans le corps de la requête JSON-RPC comme paramètre `sessionId`

- Le serveur accepte maintenant les ID de session de toutes ces sources, avec une priorité donnée à l'URL, puis aux headers.

- Si vous rencontrez encore des problèmes avec la validation des ID de session, assurez-vous que l'ID de session est correctement extrait de la réponse d'initialisation et transmis dans les requêtes suivantes.