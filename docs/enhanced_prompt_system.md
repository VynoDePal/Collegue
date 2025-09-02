# Système de Prompts Amélioré - Documentation

## Vue d'ensemble

Le système de prompts amélioré de Collègue MCP offre une gestion centralisée, versionnée et optimisée des prompts LLM pour tous les outils du projet. Il remplace l'ancienne approche où chaque outil construisait ses propres prompts de manière isolée.

## Architecture

### Composants principaux

#### 1. **EnhancedPromptEngine** (`collegue/prompts/engine/enhanced_prompt_engine.py`)
- Extension du PromptEngine de base
- Intègre le versioning, l'optimisation par langage et le tracking de performance
- Gère l'A/B testing automatique des prompts

#### 2. **PromptVersionManager** (`collegue/prompts/engine/versioning.py`)
- Gestion des versions de prompts
- Tracking des métriques de performance
- Sélection automatique de la meilleure version

#### 3. **LanguageOptimizer** (`collegue/prompts/engine/optimizer.py`)
- Optimisation spécifique par langage de programmation
- Règles et conventions pour Python, JavaScript, TypeScript
- Injection automatique de bonnes pratiques

#### 4. **Templates YAML** (`collegue/prompts/templates/tools/`)
- Templates configurables par outil
- Support de variables dynamiques
- Hints d'optimisation intégrés

## Utilisation

### Pour les développeurs d'outils

#### 1. Intégration dans un outil existant

```python
class MyTool(BaseTool):
    async def _execute_core_logic(self, request):
        # Préparer le contexte pour le prompt
        context = {
            "language": request.language,
            "description": request.description,
            "constraints": request.constraints
        }
        
        # Utiliser prepare_prompt() au lieu de _build_prompt()
        prompt = await self.prepare_prompt(
            request,
            template_name="my_tool/default"  # Optionnel
        )
        
        # Utiliser le prompt avec le LLM
        response = await self.llm_manager.generate(prompt)
```

#### 2. Création d'un nouveau template

Créer un fichier YAML dans `collegue/prompts/templates/tools/{tool_name}/default.yaml`:

```yaml
name: "Mon Template"
version: "1.0.0"
description: "Template pour mon outil"
tags: ["code", "generation"]

template: |
  Tu es un assistant expert en {language}.
  
  Tâche: {description}
  
  Contraintes:
  {constraints}
  
  Génère du code qui respecte les bonnes pratiques.

variables:
  - name: language
    type: string
    required: true
    description: "Langage de programmation"
  
  - name: description
    type: string
    required: true
    description: "Description de la tâche"
  
  - name: constraints
    type: string
    required: false
    default: "Aucune contrainte spécifique"

optimization_hints:
  - "Utilise les conventions du langage"
  - "Ajoute des commentaires pertinents"
```

### Pour les administrateurs

#### Configuration de l'A/B testing

Dans `EnhancedPromptEngine`:
```python
# Activer/désactiver l'A/B testing
engine.ab_testing_enabled = True

# Configurer le taux d'exploration (epsilon-greedy)
engine.exploration_rate = 0.1  # 10% exploration, 90% exploitation
```

#### Monitoring des performances

Les métriques sont automatiquement collectées:
- Taux de succès
- Temps d'exécution moyen
- Nombre de tokens utilisés
- Score de performance global

## Templates disponibles

### Outils supportés

1. **Code Generation** (`code_generation/`)
   - `default.yaml`: Template générique
   - `python.yaml`: Optimisé pour Python

2. **Code Explanation** (`code_explanation/`)
   - `default.yaml`: Analyse et explication de code

3. **Refactoring** (`refactoring/`)
   - `default.yaml`: Transformation de code

4. **Documentation** (`documentation/`)
   - `default.yaml`: Génération de documentation

5. **Test Generation** (`test_generation/`)
   - `default.yaml`: Création de tests unitaires

## Migration depuis l'ancien système

### Étapes de migration

1. **Modifier l'outil pour hériter de la nouvelle version de BaseTool**
   ```python
   class MyTool(BaseTool):
       def __init__(self, app_state=None, **kwargs):
           super().__init__(app_state=app_state, **kwargs)
   ```

2. **Remplacer `_build_prompt()` par `prepare_prompt()`**
   ```python
   # Ancien
   prompt = self._build_prompt(request)
   
   # Nouveau
   prompt = await self.prepare_prompt(request)
   ```

3. **Créer un template YAML correspondant**
   - Placer dans `collegue/prompts/templates/tools/{tool_name}/`
   - Définir les variables utilisées

### Fallback automatique

Le système gère automatiquement le fallback:
1. Essaye d'utiliser le nouveau système de prompts
2. Si échec, utilise l'ancienne méthode `_build_prompt()`
3. Garantit la continuité de service

## Avantages du nouveau système

### 1. **Centralisation**
- Tous les prompts au même endroit
- Facilite la maintenance et les mises à jour

### 2. **Versioning**
- Historique des modifications
- Rollback facile en cas de problème

### 3. **Optimisation automatique**
- A/B testing intégré
- Sélection de la meilleure version basée sur les performances

### 4. **Personnalisation par langage**
- Règles spécifiques par langage
- Injection automatique de bonnes pratiques

### 5. **Métriques et monitoring**
- Tracking automatique des performances
- Identification des prompts problématiques

## Dépannage

### Problèmes courants

#### "Template not found"
- Vérifier que le template existe dans le bon répertoire
- S'assurer que le nom est correct (sensible à la casse)

#### "Variables manquantes"
- Vérifier que toutes les variables requises sont fournies dans le contexte
- Consulter le template YAML pour voir les variables attendues

#### Performance dégradée
- Vérifier les métriques dans PromptVersionManager
- Considérer un rollback vers une version précédente

## API Reference

### EnhancedPromptEngine

```python
async def get_optimized_prompt(
    tool_name: str,
    context: Dict[str, Any],
    language: str = None
) -> Tuple[str, str]:
    """
    Récupère un prompt optimisé pour un outil.
    
    Args:
        tool_name: Nom de l'outil
        context: Variables pour le template
        language: Langage de programmation (optionnel)
    
    Returns:
        Tuple (prompt optimisé, version_id utilisée)
    """
```

### PromptVersionManager

```python
def create_version(
    template_id: str,
    content: str,
    variables: List[Dict],
    version: str = None
) -> PromptVersion:
    """
    Crée une nouvelle version d'un template.
    """

def get_best_version(template_id: str) -> PromptVersion:
    """
    Récupère la version avec le meilleur score de performance.
    """
```

### LanguageOptimizer

```python
def optimize_prompt(
    prompt: str,
    language: str,
    context: Dict = None
) -> str:
    """
    Optimise un prompt pour un langage spécifique.
    """
```

## Évolutions futures

1. **Interface Web avancée**
   - Éditeur YAML intégré
   - Visualisation des métriques en temps réel

2. **Multi-provider support**
   - Templates spécifiques par provider LLM
   - Optimisation automatique selon le modèle

3. **Machine Learning**
   - Prédiction de performance
   - Optimisation basée sur l'historique

4. **Collaboration**
   - Partage de templates entre équipes
   - Marketplace de prompts

## Conclusion

Le système de prompts amélioré représente une évolution majeure dans la gestion des interactions avec les LLM dans Collègue MCP. Il offre une approche professionnelle, scalable et maintenable pour optimiser continuellement la qualité des résultats générés par les outils.

Pour toute question ou suggestion, consultez le code source ou créez une issue sur le repository du projet.
