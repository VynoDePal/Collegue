# Solution pour le problème de connexion MCP

## Problème initial

Le problème concernait une erreur de connexion au serveur MCP avec le transport streamable-http :

```
Error: failed to initialize server: failed to create streamable http client: failed to initialize client: failed to send initialized notification: notification failed with status 400: Bad Request: No valid session ID provided, failed to create streamable http client with oauth: failed to initialize client: failed to register client: registration request failed with status 404: Not Found, failed to create sse client: failed to start client: unexpected status code: 400, failed to create sse client with oauth: failed to start client: failed to register client: registration request failed with status 404: Not Found.
```

## Causes identifiées

Après analyse, plusieurs problèmes ont été identifiés :

1. **Incohérence de transport** : Le script `entrypoint.sh` utilisait `--transport http` alors que la configuration dans `docker-compose.yml` spécifiait `MCP_TRANSPORT: streamable-http`.

2. **Configuration nginx inadéquate** : La configuration nginx ne traitait pas spécifiquement le chemin `/mcp/` et ne gérait pas correctement les en-têtes de session.

3. **Gestion des ID de session** : Le client ne gérait pas correctement l'extraction et l'utilisation des ID de session, ce qui provoquait l'erreur "No valid session ID provided".

## Modifications apportées

### 1. Mise à jour de entrypoint.sh

Modification du transport pour utiliser streamable-http :

```bash
# Avant
exec fastmcp run /app/collegue/app.py:app --transport http

# Après
exec fastmcp run /app/collegue/app.py:app --transport streamable-http
```

### 2. Amélioration de la configuration nginx

Ajout d'une section spécifique pour le chemin `/mcp/` avec une configuration adaptée pour streamable-http et la gestion des sessions :

```nginx
# Configuration spécifique pour le endpoint MCP
location /mcp/ {
    proxy_pass http://collegue_backend;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    
    # Configuration pour streamable-http
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    
    # Headers spécifiques pour MCP streamable-http
    proxy_set_header Accept-Encoding "";
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Port $server_port;
    
    # Préservation des headers de session
    proxy_pass_request_headers on;
    
    # Support pour les requêtes POST/GET
    client_max_body_size 50M;
    client_body_buffer_size 16k;
    proxy_busy_buffers_size 24k;
    proxy_temp_file_write_size 64k;
    
    # Sticky session pour assurer que les requêtes d'une même session vont au même backend
    proxy_set_header X-Session-ID $http_x_session_id;
}
```

### 3. Amélioration du client MCP

Plusieurs améliorations ont été apportées au client dans `test_mcp_session.py` :

1. **Extraction améliorée de l'ID de session** :
   - Ajout de la prise en charge du format JSON pour l'extraction de l'ID de session
   - Vérification des en-têtes de réponse pour l'ID de session

2. **Initialisation avec transport explicite** :
   - Ajout de `"transport": "streamable-http"` dans les paramètres d'initialisation
   - Configuration explicite des en-têtes pour streamable-http

3. **Utilisation cohérente de l'ID de session** :
   - Utilisation de plusieurs en-têtes pour l'ID de session (`X-Session-ID` et `MCP-Session-ID`)
   - Inclusion de l'ID de session dans le corps de la requête JSON-RPC

## Comment tester la solution

1. Redémarrer les services Docker :
   ```bash
   docker-compose down
   docker-compose up -d
   ```

2. Exécuter le script de test :
   ```bash
   python3 test_mcp_session.py
   ```

Le script devrait maintenant se connecter correctement au serveur MCP, initialiser une session et effectuer des requêtes pour lister les outils et les ressources disponibles.

## Configuration recommandée pour les clients MCP

Pour les clients MCP qui se connectent à ce serveur, utilisez la configuration suivante :

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

Cette configuration est compatible avec les modifications apportées au serveur et devrait permettre une connexion réussie.