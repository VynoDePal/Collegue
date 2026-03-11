# Rate Limiting et Quotas

Ce document décrit le système de rate limiting et de quotas implémenté pour les outils Collegue.

## Table des matières

1. [Vue d'ensemble](#vue-densemble)
2. [Rate Limiting](#rate-limiting)
   - [Algorithmes disponibles](#algorithmes-disponibles)
   - [Configuration](#configuration)
   - [Utilisation](#utilisation)
3. [Quotas](#quotas)
   - [Types de quotas](#types-de-quotas)
   - [Configuration](#configuration-1)
   - [Utilisation](#utilisation-1)
4. [Intégration avec BaseTool](#intégration-avec-basetool)
5. [Variables d'environnement](#variables-denvironnement)
6. [Tests](#tests)

## Vue d'ensemble

Le système de rate limiting et de quotas protège contre:
- La saturation des API externes (GitHub, Sentry, etc.)
- Les coûts LLM incontrôlés
- Les boucles infinies accidentelles
- Les abus de ressources

## Rate Limiting

### Algorithmes disponibles

1. **Token Bucket** (par défaut)
   - Permet des bursts courts
   - Taux moyen constant
   - Idéal pour les APIs avec tolérance aux pics

2. **Fixed Window**
   - Simple et efficace
   - Fenêtres de temps fixes
   - Peut permettre des bursts aux limites

3. **Sliding Window**
   - Précision maximale
   - Fenêtre glissante continue
   - Plus coûteux en mémoire

### Configuration

```python
from collegue.tools.rate_limiter import RateLimitConfig, RateLimitStrategy

# Configuration personnalisée
config = RateLimitConfig(
    requests_per_minute=30,           # 30 requêtes/minute
    burst=5,                          # Burst de 5 requêtes
    strategy=RateLimitStrategy.TOKEN_BUCKET
)
```

### Utilisation

```python
from collegue.tools.rate_limiter import (
    get_rate_limiter_manager,
    RateLimitExceeded
)

# Vérifier le rate limit
try:
    manager = get_rate_limiter_manager()
    manager.check_rate_limit("github_ops")
    # Effectuer la requête
except RateLimitExceeded as e:
    print(f"Rate limit dépassé: {e}")
    print(f"Réessayez dans {e.retry_after}s")
```

## Quotas

### Types de quotas

1. **LLM Tokens** (`llm_tokens`)
   - Limite: 100 000 tokens par session (défaut)
   - Protection contre les coûts élevés

2. **File Size** (`file_size`)
   - Limite: 1 MB par fichier (défaut)
   - Évite le traitement de fichiers trop grands

3. **File Count** (`file_count`)
   - Limite: 100 fichiers par requête (défaut)
   - Prévient les analyses massives

4. **Execution Time** (`execution_time`)
   - Limite: 300 secondes (5 min) par tool (défaut)
   - Évite les exécutions infinies

5. **Request Size** (`request_size`)
   - Limite: 10 MB par requête (défaut)
   - Protège contre les payloads massifs

### Configuration

```python
from collegue.tools.quotas import QuotaConfig

# Configuration personnalisée
config = QuotaConfig(
    llm_tokens_per_session=50000,      # 50k tokens
    max_file_size_bytes=512 * 1024,     # 512 KB
    max_files_per_request=50,
    max_execution_time_seconds=600.0,   # 10 minutes
    max_request_size_bytes=5 * 1024 * 1024  # 5 MB
)
```

### Utilisation

```python
from collegue.tools.quotas import (
    get_global_quota_manager,
    QuotaExceeded
)

# Obtenir le gestionnaire pour une session
manager = get_global_quota_manager()
quota_manager = manager.get_session_manager("session_123")

try:
    # Vérifier les quotas avant traitement
    quota_manager.start_execution()
    
    # Enregistrer l'utilisation
    quota_manager.record_llm_tokens(1500)
    quota_manager.record_file_processed("file.py", 1024)
    
    # Vérifier le temps d'exécution
    elapsed = quota_manager.check_execution_time()
    
except QuotaExceeded as e:
    print(f"Quota dépassé: {e.quota_type}")
    print(f"Utilisation: {e.current}/{e.limit}")
```

## Intégration avec BaseTool

Tous les outils héritant de `BaseTool` bénéficient automatiquement du rate limiting et des quotas.

```python
from collegue.tools.base import BaseTool
from pydantic import BaseModel

class MyRequest(BaseModel):
    code: str

class MyResponse(BaseModel):
    result: str

class MyTool(BaseTool):
    tool_name = "my_tool"
    tool_description = "Description de l'outil"
    request_model = MyRequest
    response_model = MyResponse
    
    # Activer/désactiver (activé par défaut)
    rate_limit_enabled = True
    quota_enabled = True
    
    # Config personnalisée
    custom_rate_limit = RateLimitConfig(
        requests_per_minute=60,
        burst=10
    )
    
    def _execute_core_logic(self, request: MyRequest, **kwargs) -> MyResponse:
        # Le rate limiting et les quotas sont vérifiés automatiquement
        return MyResponse(result="OK")
```

### Désactivation

```python
class UnrestrictedTool(BaseTool):
    rate_limit_enabled = False  # Désactiver le rate limiting
    quota_enabled = False       # Désactiver les quotas
    
    def _execute_core_logic(self, request, **kwargs):
        # Aucune vérification
        pass
```

### Gestion des erreurs

```python
from collegue.tools.base import (
    ToolRateLimitError,
    ToolQuotaError
)

try:
    result = tool.execute(request)
except ToolRateLimitError as e:
    # Gérer le rate limit
    print(f"Trop de requêtes: {e}")
except ToolQuotaError as e:
    # Gérer le quota
    print(f"Quota dépassé: {e}")
```

## Variables d'environnement

### Rate Limiting

| Variable | Description | Défaut |
|----------|-------------|--------|
| `COLLEGUE_HTTP_MAX_RETRIES` | Nombre max de retries | 3 |
| `COLLEGUE_HTTP_BASE_DELAY` | Délai de base (secondes) | 1.0 |
| `COLLEGUE_HTTP_MAX_DELAY` | Délai max (secondes) | 60.0 |
| `COLLEGUE_HTTP_EXPONENTIAL_BASE` | Base exponentielle | 2.0 |
| `COLLEGUE_CB_FAILURE_THRESHOLD` | Seuil d'échec circuit breaker | 5 |
| `COLLEGUE_CB_RECOVERY_TIMEOUT` | Timeout de récupération (secondes) | 30.0 |

### Quotas

| Variable | Description | Défaut |
|----------|-------------|--------|
| `COLLEGUE_QUOTA_LLM_TOKENS` | Tokens LLM max par session | 100000 |
| `COLLEGUE_QUOTA_MAX_FILE_SIZE` | Taille max fichier (bytes) | 1048576 (1MB) |
| `COLLEGUE_QUOTA_MAX_FILES` | Nombre max fichiers | 100 |
| `COLLEGUE_QUOTA_MAX_EXEC_TIME` | Temps max exécution (secondes) | 300.0 |
| `COLLEGUE_QUOTA_MAX_REQUEST_SIZE` | Taille max requête (bytes) | 10485760 (10MB) |

## Tests

Exécuter les tests:

```bash
# Rate limiting
pytest tests/test_rate_limiter.py -v

# Quotas
pytest tests/test_quotas.py -v

# Intégration BaseTool
pytest tests/test_base_tool_rate_limits.py -v

# Tous les tests
pytest tests/test_rate_limiter.py tests/test_quotas.py tests/test_base_tool_rate_limits.py -v
```

### Tests lents

Certains tests utilisent des délais réels (60s). Pour les ignorer:

```bash
pytest tests/test_rate_limiter.py -v -m "not slow"
```

## Exemples complets

### Tool avec rate limiting strict

```python
from collegue.tools.base import BaseTool
from collegue.tools.rate_limiter import RateLimitConfig

class GitHubTool(BaseTool):
    tool_name = "github_ops"
    custom_rate_limit = RateLimitConfig(
        requests_per_minute=30,
        burst=5
    )
    
    def _execute_core_logic(self, request, **kwargs):
        # Max 30 req/min avec burst de 5
        return self.call_github_api(request)
```

### Tool avec quotas personnalisés

```python
from collegue.tools.base import BaseTool
from collegue.tools.quotas import QuotaConfig

class AnalyzerTool(BaseTool):
    tool_name = "code_analyzer"
    
    def _get_quota_manager(self, **kwargs):
        """Surcharge pour configurer des quotas personnalisés."""
        manager = super()._get_quota_manager(**kwargs)
        # Configurer des quotas stricts si pas déjà configuré
        if manager.config.llm_tokens_per_session == QuotaConfig.from_env().llm_tokens_per_session:
            manager.config = QuotaConfig(
                llm_tokens_per_session=50000,
                max_files_per_request=50
            )
        return manager
    
    def _execute_core_logic(self, request, **kwargs):
        # Vérifications automatiques
        return self.analyze(request)
```

### Monitoring des quotas

```python
from collegue.tools.quotas import get_global_quota_manager

# Stats globales
manager = get_global_quota_manager()
all_stats = manager.get_all_stats()

for session_id, stats in all_stats.items():
    print(f"Session {session_id}:")
    print(f"  Tokens: {stats['llm_tokens_used']}/{stats['quotas']['llm_tokens']}")
    print(f"  Files: {stats['files_processed']}/{stats['quotas']['max_files']}")
    print(f"  Time: {stats['execution_time_seconds']:.1f}s")
```
