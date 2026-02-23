# Fix: Timeout d'initialisation MCP à 60s

## Résumé des changements

### Problème
Le serveur MCP subissait un timeout à 60 secondes lors de l'initialisation car :
1. `EnhancedPromptEngine` s'initialisait de manière **synchrone et bloquante**
2. Le chargement des templates YAML et du système de versioning prenait >10s
3. Le `core_lifespan` bloquait complètement le démarrage

### Solution implémentée

#### 1. LazyPromptEngine (`collegue/app.py`)
Nouvelle classe qui retarde l'initialisation lourde :
- Initialisation en **tâche de fond** avec `asyncio.create_task()`
- Utilisation de `asyncio.to_thread()` pour ne pas bloquer l'event loop
- **Timeout de 10s** sur l'initialisation interne
- Méthode `get_engine(timeout=25s)` pour attendre l'engine si nécessaire
- Logs détaillés pour diagnostiquer les temps de démarrage

#### 2. Modification BaseTool (`collegue/tools/base.py`)
Le `execute_async` attend maintenant automatiquement le lazy engine :
```python
if hasattr(prompt_engine, 'get_engine'):
    self.prompt_engine = await prompt_engine.get_engine(timeout=25.0)
```

#### 3. Tests unitaires (`tests/test_mcp_init_timeout.py`)
13 tests couvrant :
- L'état initial du LazyPromptEngine
- L'initialisation en tâche de fond
- La gestion des timeouts
- Les erreurs d'initialisation
- L'intégration avec BaseTool
- Le temps de démarrage du lifespan (<1s)

### Résultat
- **Démarrage du serveur** : <1s (au lieu de 10-30s)
- **Le PromptEngine** s'initialise en parallèle
- **Les tools** fonctionnent immédiatement s'ils n'utilisent pas l'engine
- **Pas de régression** : les tools attendent l'engine si besoin

### Commandes pour créer la PR

```bash
# Si ce n'est pas déjà fait
git checkout fix/mcp-init-timeout

# Pousser la branche (nécessite authentification GitHub)
git push origin fix/mcp-init-timeout

# Créer la PR via GitHub CLI (si installé)
gh pr create --title "fix: Résoudre le timeout à 60s lors de l'initialisation MCP" \
             --body "Voir FIX_TIMEOUT.md pour les détails"
```

### Tester le fix

```bash
# Activer le venv
source .venv/bin/activate

# Lancer les tests
python -m pytest tests/test_mcp_init_timeout.py -v

# Démarrer le serveur et vérifier les logs
python -m collegue.app
```

Les logs devraient montrer :
```
🔄 Démarrage du core_lifespan...
✅ CodeParser initialisé en 0.002s
🚀 Initialisation du PromptEngine lancée en tâche de fond
✅ Composants initialisés en 0.01s
✅ EnhancedPromptEngine initialisé en X.XXs
```
