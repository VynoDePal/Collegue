# Guide de Configuration MCP pour Windsurf

## Problèmes Identifiés et Solutions Implementées

### ✅ 1. Port Corrigé
- **Problème** : Le port était configuré sur 8080 mais Docker expose sur 8088
- **Solution** : Tous les scripts utilisent maintenant `http://localhost:8088/mcp/`

### ✅ 2. Headers HTTP Corrigés
- **Problème** : Le serveur MCP exige `Accept: application/json, text/event-stream`
- **Solution** : Headers correctement configurés dans tous les tests

### ✅ 3. Endpoint de Santé Ajouté
- **Problème** : L'endpoint `/_health` n'était pas accessible via nginx
- **Solution** : Configuration nginx mise à jour avec endpoint spécifique

### ✅ 4. Initialisation MCP Fonctionnelle
- **Problème** : Paramètres d'initialisation incorrects
- **Solution** : Structure JSON-RPC 2.0 correcte implémentée

### ⚠️ 5. Gestion de Session ID
- **Statut** : Partiellement résolu
- **Détail** : L'ID de session est créé côté serveur mais la transmission client n'est pas correcte

## Configuration Recommandée pour Windsurf

```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/"
  }
}
```

## Tests de Validation

### Test Basique
```bash
python3 test_mcp_connection.py
```

### Test Avancé avec Session
```bash
python3 test_mcp_session.py
```

## Résultats des Tests

✅ **Serveur Accessible** : Port 8088 fonctionne  
✅ **Initialisation MCP** : Status 200, protocol version 2024-11-05  
✅ **Création Session** : ID généré côté serveur  
❌ **Utilisation Session** : Transmission session ID à résoudre  

## Prochaines Étapes

1. **Pour le développement** : Le serveur fonctionne et peut être utilisé avec un client MCP compatible
2. **Pour Windsurf** : Utilisez la configuration recommandée ci-dessus
3. **Pour le debugging** : Surveillez les logs avec `docker compose logs collegue-app`

## Logs de Session Exemple

```
INFO:mcp.server.streamable_http_manager:Created new transport with session ID: c88d16d3556a421e89f3f4f6ce92eecb
```

## 🚀 Configuration Rapide avec Windsurf

### Configuration de base (utilise les variables d'environnement)
```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/"
  }
}
```

### Configuration personnalisée avec modèle et clé API
```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/",
    "LLM_MODEL": "gemini-3-flash-preview",
    "LLM_API_KEY": "AIzaSy-votre-cle-api-gemini"
  }
}
```

## 📋 Priorité de Configuration

Le système utilise la priorité suivante pour les paramètres :
1. **MCP Config** (mcp_config.json dans Windsurf) - **Priorité maximale**
2. **Variables d'environnement** (.env ou export)
3. **Valeurs par défaut** du code

### Exemple de priorité
- Si `LLM_MODEL` est défini dans mcp_config.json → utilise cette valeur
- Sinon, si `LLM_MODEL` est dans .env → utilise la valeur .env
- Sinon → utilise la valeur par défaut "gemini-3-flash-preview"

## 🔧 Installation et Configuration

### Prérequis
- Python 3.10+
- Docker et Docker Compose
- Clé API Google Gemini ([obtenir une clé](https://aistudio.google.com/apikey))

### Installation locale
```bash
# Cloner le repository
git clone https://github.com/your-org/collegue-mcp.git
cd collegue-mcp

# Créer l'environnement virtuel
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows

# Installer les dépendances
pip install -r requirements.txt
```

### Configuration avec fichier .env (optionnel)
Créez un fichier `.env` à la racine :
```env
# Configuration de base (sera surchargée par MCP si défini)
LLM_API_KEY=AIzaSy-your-api-key-here
LLM_MODEL=gemini-3-flash-preview

# Configuration serveur
HOST=0.0.0.0
PORT=4121

# OAuth (optionnel)
OAUTH_ENABLED=false
```

## 🎯 Modèles Google Gemini Disponibles

### Modèles Gratuits
```json
"LLM_MODEL": "gemini-3-flash-preview"
"LLM_MODEL": "gemini-2.5-flash"
"LLM_MODEL": "gemini-2.5-flash-lite"
```

### Modèles Économiques
```json
"LLM_MODEL": "gemini-2.5-flash"        // Recommandé - Rapide et économique
"LLM_MODEL": "gemini-2.5-pro"         // Haute performance
```

### Modèles Performants
```json
"LLM_MODEL": "gemini-2.5-pro"           // Gemini Pro optimisé
"LLM_MODEL": "gemini-3-flash"           // Gemini 3 Flash
```

## 🐳 Utilisation avec Docker

### Démarrage rapide
```bash
# Configuration minimale (utilise .env)
docker-compose up -d

# Avec paramètres MCP personnalisés
MCP_LLM_MODEL="gemini-3-flash-preview" MCP_LLM_API_KEY="AIzaSy-xxx" docker-compose up -d
```

### Services disponibles
- **collegue-app** : Serveur MCP principal (port 4121)
- **nginx** : Proxy HTTP (port 8088)
- **keycloak** : OAuth (port 4123) - optionnel

## 🔍 Test de Configuration

### Vérifier la configuration active
```bash
# Health check
curl http://localhost:8088/health

# Voir les logs pour confirmer le modèle utilisé
docker-compose logs collegue-app | grep "modèle"
```

### Test avec le client Python
```python
from collegue.client import CollegueClient

client = CollegueClient(base_url="http://localhost:8088")

# Tester la génération de code
result = await client.generate_code(
    language="python",
    description="Une fonction pour calculer fibonacci"
)
print(result)
```

## ⚠️ Sécurité

### Bonnes Pratiques
1. **Ne jamais commit** votre clé API dans git
2. **Utiliser .gitignore** pour exclure .env et mcp_config.json
3. **Rotation régulière** des clés API
4. **Limiter les permissions** de la clé API si possible

### Format de Clé API Google Gemini
Les clés Google Gemini commencent toujours par `AIzaSy`

## 🐛 Dépannage

### Erreur : "La clé API LLM n'est pas configurée"
**Solutions :**
1. Vérifiez votre mcp_config.json dans Windsurf
2. Vérifiez le fichier .env
3. Format correct : `AIzaSy-xxxxx`

### Erreur : "Modèle non disponible"
**Solutions :**
1. Vérifiez que le modèle existe sur [Google AI Studio](https://aistudio.google.com)
2. Certains modèles nécessitent des crédits
3. Utilisez un modèle gratuit pour tester

### Logs et Debugging
```bash
# Voir tous les logs
docker-compose logs -f

# Logs avec le modèle utilisé
docker-compose logs collegue-app | grep "ToolLLMManager"

# Mode debug
DEBUG=true docker-compose up
```

## 📚 Exemples de Configuration

### Configuration Développement (Gratuite)
```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/",
    "LLM_MODEL": "gemini-3-flash-preview",
    "LLM_API_KEY": "AIzaSy-dev-key"
  }
}
```

### Configuration Production (Performante)
```json
{
  "collegue": {
    "serverUrl": "https://collegue.example.com/mcp/",
    "LLM_MODEL": "gemini-2.5-pro",
    "LLM_API_KEY": "AIzaSy-prod-key"
  }
}
```

### Configuration Test (Économique)
```json
{
  "collegue": {
    "serverUrl": "http://localhost:8088/mcp/",
    "LLM_MODEL": "gemini-2.5-flash",
    "LLM_API_KEY": "AIzaSy-test-key"
  }
}
```

## 💡 Tips et Astuces

1. **Commencez avec un modèle gratuit** pour tester la configuration
2. **Utilisez gemini-3-flash-preview** pour un bon équilibre performance/coût
3. **Surveillez votre usage** sur [Google Cloud Console](https://console.cloud.google.com/billing)
4. **Configurez des limites** de dépenses dans Google Cloud
5. **Testez différents modèles** pour trouver le meilleur pour votre usage

## 📞 Support

Pour toute question :
- Consultez le [README principal](../README.md)
- Ouvrez une issue sur GitHub
- Consultez la [documentation Google Gemini API](https://ai.google.dev/gemini-api/docs)

Le serveur Collègue MCP est maintenant prêt pour utilisation !
