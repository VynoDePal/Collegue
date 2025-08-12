
## Découverte MCP côté Resource Server

Le Resource Server (Collegue) doit exposer:

```http
GET /.well-known/oauth-protected-resource
```

Exemple de réponse:

```json
{
  "authorization_servers": [
    "http://localhost:4123/realms/master"
  ],
  "resource_id": "collegue",
  "scopes_supported": ["mcp.read", "mcp.write"]
}
```

Note: L'endpoint `/.well-known/oauth-authorization-server` appartient à
l'Authorization Server (Keycloak). Il n'est pas requis sur le Resource Server.

## Nginx et health server

Pour exposer publiquement la découverte depuis la même origine que MCP
(`http://localhost:8088`), Nginx proxyfie `/.well-known/*` vers le health
server (FastAPI) sur le port 4122. Cela évite d'imposer l'exposition directe
de Collegue pour ces endpoints.

Exemple minimal pour le endpoint MCP (sans réécriture des headers de session):

```nginx
location /mcp/ {
    proxy_pass http://collegue_backend;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Propager explicitement l'en-tête Authorization
    proxy_set_header Authorization $http_authorization;

    # streamable-http
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    proxy_set_header Accept-Encoding "";

    # Ne pas réécrire X-Session-ID / MCP-Session-ID
    proxy_pass_request_headers on;
}
```

### Mise à l'échelle et sessions

FastMCP (streamable-http) maintient des sessions en mémoire locale.

- En développement, privilégier __un seul réplica__ (`collegue-app`) pour
  éviter les erreurs `400 No valid session ID provided`.
- En production multi-réplicas, mettre en place des __sticky sessions__ côté
  proxy (p. ex. `ip_hash` dans `upstream`) ou un stockage de session partagé.

## Configuration Keycloak (DCR)

Dans le realm (`master` par défaut en dev):

- Realm Settings → Client Registration Policies → Choisissez:
  - Anonymous (développement), ou
  - Initial Access Token (recommandé). Créez un token initial.
- Vérifiez que `/.well-known/openid-configuration` expose
  `registration_endpoint`.

### Provisioning automatique (kc-provisioner)

Le service `kc-provisioner` automatise la post-configuration du client créé
par DCR (Windsurf):

- Crée les client scopes `mcp.read` et `mcp.write` s'ils sont absents.
- Les marque comme __Default__ au niveau du realm (inclusion automatique).
- Active le service account sur le client DCR.
- Attache `mcp.read` et `mcp.write` au client (idempotent).
- Gère les `401` (refresh admin token) et les particularités du `client-secret`.

## Variables d'environnement (exemples)

```env
# Côté Collegue (Resource Server)
OAUTH_ENABLED=true
OAUTH_ISSUER=http://localhost:4123/realms/master
OAUTH_JWKS_URI=http://keycloak:8080/realms/master/protocol/openid-connect/certs
OAUTH_AUDIENCE=collegue
OAUTH_REQUIRED_SCOPES=mcp.read,mcp.write

# URL publique de l'Authorization Server (pour découverte côté client)
OAUTH_AUTH_SERVER_PUBLIC=http://localhost:4123/realms/master

# Côté Keycloak (Docker)
KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=admin
```

### Configuration finale (local)

Exemple `.env` fonctionnel en local (Keycloak via Docker):

```env
LLM_API_KEY="<votre_clé>"
OAUTH_ENABLED=true
OAUTH_JWKS_URI=http://keycloak:8080/realms/master/protocol/openid-connect/certs
OAUTH_ISSUER=http://localhost:4123/realms/master
OAUTH_AUTH_SERVER_PUBLIC=http://localhost:4123/realms/master
OAUTH_ALGORITHM=RS256
OAUTH_REQUIRED_SCOPES="mcp.read,mcp.write"
# Optionnel mais recommandé pour un resource_id ASCII
OAUTH_AUDIENCE=collegue

# Keycloak (dev)
KEYCLOAK_ADMIN=<admin>
KEYCLOAK_ADMIN_PASSWORD=<mot_de_passe>
PROVISION_CLIENT_ID=windsurf-client
WAIT_FOR_CLIENT_SECONDS=600
```

Pour Windsurf, la configuration MCP doit simplement pointer vers le SSE/HTTP
du serveur MCP, sans bloc `oauth`:

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:8088/mcp/"
    }
  }
}
```

Windsurf détectera automatiquement l'Authorization Server via
`/.well-known/oauth-protected-resource`.

## Dépannage

- __401 invalid_token__
  - Vérifier la propagation de `Authorization` côté Nginx.
  - Vérifier l'accessibilité du JWKS depuis `collegue-app`:
    `http://keycloak:8080/realms/master/protocol/openid-connect/certs`.
  - Vérifier `iss` du token (doit être `http://localhost:4123/realms/master`).
  - Laisser `OAUTH_AUDIENCE` vide au début, ou aligner `aud` côté Keycloak si
    vous le définissez.
  - Si le token n'expose pas `scope`/`scp`, tester avec
    `OAUTH_REQUIRED_SCOPES=` vide, puis réactiver.

- __400 No valid session ID provided__
  - Éviter le multi-réplicas sans sticky sessions.
  - Ne pas réécrire les en-têtes de session (X-Session-ID, MCP-Session-ID).
# Authentification OAuth pour Collegue MCP

## Introduction

Cette documentation décrit l'implémentation de l'authentification OAuth dans le projet Collegue MCP. L'authentification est basée sur le fournisseur `BearerAuthProvider` de FastMCP, qui permet de vérifier les tokens JWT émis par un serveur d'identité OAuth.

## Configuration

L'authentification OAuth peut être configurée via les variables d'environnement ou le fichier `.env`. Voici les paramètres disponibles :

| Variable | Description | Valeur par défaut |
|----------|-------------|-------------------|
| `OAUTH_ENABLED` | Active/désactive l'authentification OAuth | `False` |
| `OAUTH_JWKS_URI` | URL du endpoint JWKS du serveur d'identité | `None` |
| `OAUTH_ISSUER` | Émetteur des tokens (issuer) | `None` |
| `OAUTH_ALGORITHM` | Algorithme de signature des tokens | `"RS256"` |
| `OAUTH_AUDIENCE` | Audience cible des tokens | `None` |
| `OAUTH_REQUIRED_SCOPES` | Scopes requis pour accéder aux endpoints | `[]` |
| `OAUTH_PUBLIC_KEY` | Clé publique pour vérifier les tokens (alternative à JWKS) | `None` |

## Méthodes de configuration

### Méthode 1 : Via JWKS (recommandée)

Cette méthode est recommandée pour la production car elle permet la rotation automatique des clés.

```env
OAUTH_ENABLED=true
OAUTH_JWKS_URI=https://votre-serveur-identity/.well-known/jwks.json
OAUTH_ISSUER=https://votre-serveur-identity/
OAUTH_ALGORITHM=RS256
OAUTH_AUDIENCE=votre-application
OAUTH_REQUIRED_SCOPES=read,write
```

### Méthode 2 : Via clé publique

Cette méthode est plus simple mais nécessite une mise à jour manuelle si la clé change.

```env
OAUTH_ENABLED=true
OAUTH_PUBLIC_KEY=-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----
OAUTH_ISSUER=https://votre-serveur-identity/
OAUTH_ALGORITHM=RS256
OAUTH_AUDIENCE=votre-application
OAUTH_REQUIRED_SCOPES=read
```

## Fonctionnement

1. Le serveur Collegue (Resource Server) expose l'endpoint:
   - `/.well-known/oauth-protected-resource` (via Nginx → health server)
2. Cet endpoint indique au client MCP (Windsurf) quel Authorization Server
   utiliser (Keycloak) grâce au champ `authorization_servers`.
3. Windsurf récupère alors la configuration de l'Authorization Server via
   les endpoints Keycloak:
   - `/.well-known/openid-configuration`
   - `/.well-known/oauth-authorization-server`
4. Windsurf tente la Dynamic Client Registration (DCR) sur Keycloak
   (via `registration_endpoint`). Selon la politique du realm, activer
   l'anonymous DCR ou utiliser un "Initial Access Token".
5. Une fois le client enregistré, Windsurf obtient un token et l'envoie
   en `Authorization: Bearer <token>` vers Collegue. Le `BearerAuthProvider`
   vérifie le token (JWKS, issuer, audience, scopes) et autorise l'accès.

## Tests

Des tests unitaires sont disponibles dans `tests/test_oauth.py` pour valider le bon fonctionnement de l'authentification.

Pour exécuter les tests :

```bash
python -m unittest tests/test_oauth.py
```

## Sécurité

- L'authentification est désactivée par défaut pour faciliter le développement.
- En production, il est fortement recommandé d'activer l'authentification.
- Utilisez de préférence la méthode JWKS pour la gestion des clés.
- Protégez les fichiers de configuration contenant les clés privées.
