# Système Multi-Agents / Experts IA — Guide Complet

> Ce document décrit l'architecture, le fonctionnement et l'utilisation du **Collectif d'Experts IA** intégré dans Collègue MCP.

## Table des matières

1. [Vue d'ensemble](#vue-densemble)
2. [Architecture](#architecture)
3. [Les 10 Experts IA](#les-10-experts-ia)
4. [Boucle Agentique (AgentLoopMixin)](#boucle-agentique)
5. [Délégation Inter-Experts](#délégation-inter-experts)
6. [Mémoire Persistante (ProjectMemory)](#mémoire-persistante)
7. [Moniteur Proactif (ProactiveMonitor)](#moniteur-proactif)
8. [Tableau de Bord (ExpertDashboard)](#tableau-de-bord)
9. [Intégration dans un Projet](#intégration-dans-un-projet)
10. [Configuration Avancée](#configuration-avancée)

---

## Vue d'ensemble

Collègue MCP n'est pas un simple serveur d'outils — c'est un **collectif d'experts IA spécialisés** qui travaillent ensemble pour accompagner un projet de développement. Chaque outil MCP backed par un LLM est un agent expert dans son domaine :

```
┌──────────────────────────────────────────────────────────────┐
│                    Collègue MCP Server                        │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ Code Review │  │Architecture │  │ Performance │          │
│  │   Expert    │──│   Expert    │──│   Expert    │          │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘         │
│         │                │                │                  │
│  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐         │
│  │ Refactoring │  │    Test     │  │Documentation│          │
│  │   Expert    │──│ Generation  │──│   Expert    │          │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘         │
│         │                │                │                  │
│  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐         │
│  │  IaC Scan   │  │ Consistency │  │   Impact    │          │
│  │   Expert    │──│   Check     │──│  Analysis   │          │
│  └─────────────┘  └─────────────┘  └──────┬──────┘         │
│                                           │                  │
│  ┌────────────────────────────────────────▼────────────────┐ │
│  │              ExpertDelegationEngine                      │ │
│  │         (14 règles de délégation automatique)            │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │              ProjectMemory                               │ │
│  │    (mémoire persistante inter-sessions JSON)            │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │              ProactiveMonitor                            │ │
│  │       (détection de changements → déclenchement)        │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │              ExpertDashboard                             │ │
│  │         (agrégation scores + recommandations)           │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### Principe fondamental

Chaque expert :
1. **Analyse** le code avec des heuristiques statiques (regex, AST, graph de dépendances)
2. **Enrichit** l'analyse via un LLM (mode `deep`) pour des insights sémantiques
3. **Itère** sur sa propre sortie via la boucle agentique (validation + correction)
4. **Délègue** à d'autres experts quand il détecte des problèmes hors de son domaine
5. **Mémorise** ses résultats pour les sessions futures

---

## Architecture

### Composants principaux

| Composant | Rôle | Fichier |
|-----------|------|---------|
| `AgentLoopMixin` | Boucle itérative : exécute → valide → corrige → re-exécute | `collegue/tools/agent_loop.py` |
| `ExpertDelegationEngine` | Routage inter-experts basé sur des règles conditionnelles | `collegue/core/expert_delegation.py` |
| `ProjectMemory` | Stockage JSON persistant des résultats inter-sessions | `collegue/core/project_memory.py` |
| `ProactiveMonitor` | Détection de changements git → déclenchement d'experts | `collegue/autonomous/proactive_monitor.py` |
| `ExpertDashboard` | Agrégation des scores et recommandations | `collegue/tools/expert_dashboard/tool.py` |
| `MetaOrchestrator` | Planification multi-étapes (Plan → Execute → Synthesize) | `collegue/core/meta_orchestrator.py` |

### Flux de données

```
Client MCP (IDE)
    │
    ▼
┌──────────────────────┐
│  smart_orchestrator   │  ← Reçoit une requête complexe
│  (MetaOrchestrator)  │
└──────────┬───────────┘
           │ Planifie les étapes
           ▼
┌──────────────────────┐     ┌─────────────────┐
│  Expert Tool N       │ ──► │ ProjectMemory   │
│  (ex: code_review)   │ ◄── │ (recall/store)  │
└──────────┬───────────┘     └─────────────────┘
           │ Résultat
           ▼
┌──────────────────────┐
│  ExpertDelegation    │  ← Évalue si d'autres experts doivent intervenir
│  Engine              │
└──────────┬───────────┘
           │ Déclenche expert(s) suivant(s)
           ▼
┌──────────────────────┐
│  Expert Tool M       │  ← Exécuté automatiquement
│  (ex: refactoring)   │
└──────────────────────┘
```

---

## Les 10 Experts IA

### Experts d'analyse (lecture seule)

| Expert | Description | Mode fast | Mode deep (LLM) |
|--------|-------------|-----------|------------------|
| `code_review` | Revue qualité : naming, complexité, sécurité, DRY, SOLID | Regex + AST | LLM enrichit les findings |
| `architecture_analysis` | Analyse patterns, dépendances, cycles, couplage | Graph imports + heuristiques | LLM évalue la dette technique |
| `performance_analysis` | Détection O(n²), I/O bloquant, concat en boucle | Regex patterns | LLM identifie les hotspots |
| `impact_analysis` | Prédiction d'impact d'un changement sur le codebase | Analyse statique des dépendances | LLM fournit des insights sémantiques |
| `repo_consistency_check` | Détection de code mort, incohérences, hallucinations | Heuristiques + comparaison | LLM scoring + recommandations |

### Experts de transformation (écriture)

| Expert | Description | Entrée | Sortie |
|--------|-------------|--------|--------|
| `code_refactoring` | Restructure et optimise le code | Code source | Code refactoré + changelog |
| `test_generation` | Génère des tests unitaires | Code source + framework | Tests exécutables |
| `code_documentation` | Génère docstrings et documentation | Code source | Documentation structurée |

### Experts de sécurité

| Expert | Description | Cibles |
|--------|-------------|--------|
| `iac_guardrails_scan` | Scan de sécurité IaC | Dockerfile, Kubernetes YAML, Terraform |
| `secret_scan` | Détection de secrets exposés | Tout fichier texte |

### Expert d'orchestration

| Expert | Description |
|--------|-------------|
| `smart_orchestrator` | Reçoit une requête complexe, planifie les experts à utiliser, exécute, synthétise |

---

## Boucle Agentique

Chaque expert LLM-backed utilise `AgentLoopMixin` pour itérer sur sa sortie :

```
┌─────────────────────────────────────────────┐
│              AgentLoopMixin                  │
│                                              │
│  Itération 1:                                │
│    Prompt → LLM → Output                    │
│    validate_agent_output(output) → erreurs?  │
│    assess_agent_quality(output) → score      │
│    Si score ≥ seuil → CONVERGÉ ✓            │
│    Sinon:                                    │
│      build_agent_feedback(output, erreurs)   │
│      temperature -= decay                    │
│                                              │
│  Itération 2:                                │
│    Prompt + feedback → LLM → Output v2      │
│    validate... → assess... → convergé?       │
│                                              │
│  ... jusqu'à max_iterations ou convergence   │
│                                              │
│  Retour: meilleur output (score le plus haut)│
└─────────────────────────────────────────────┘
```

### Configuration par expert

```python
class AgentLoopConfig(BaseModel):
    max_iterations: int = 3        # Tentatives maximum
    convergence_threshold: float = 0.85  # Score pour arrêter
    initial_temperature: float = 0.7     # Température LLM initiale
    temperature_decay: float = 0.15      # Réduction par itération
    min_temperature: float = 0.2         # Plancher température
```

Chaque expert définit sa propre config. Par exemple, `code_refactoring` utilise `max_iterations=3` tandis que `test_generation` utilise `max_iterations=4` car les tests nécessitent plus de corrections.

### Hooks à implémenter

Un expert qui hérite de `AgentLoopMixin` doit implémenter 3 méthodes :

```python
class MonExpert(AgentLoopMixin):
    async def validate_agent_output(self, output: str, context: dict) -> list[str]:
        """Retourne une liste d'erreurs (vide = valide)."""
        ...

    async def assess_agent_quality(self, output: str, context: dict) -> float:
        """Retourne un score de qualité entre 0.0 et 1.0."""
        ...

    async def build_agent_feedback(self, output: str, errors: list, quality: float, context: dict) -> str:
        """Construit un feedback pour guider la prochaine itération."""
        ...
```

---

## Délégation Inter-Experts

L'`ExpertDelegationEngine` permet aux experts de déclencher automatiquement d'autres experts en chaîne.

### Les 14 règles de délégation

| Déclencheur | Condition | Expert déclenché | Priorité |
|-------------|-----------|------------------|----------|
| `repo_consistency_check` | refactoring_score > 0.5 | `code_refactoring` | 5 |
| `code_refactoring` | changements effectués | `code_documentation` | 10 |
| `code_refactoring` | changements effectués | `test_generation` | 10 |
| `code_refactoring` | changements effectués → revue | `code_review` | 15 |
| `code_review` | quality_score < 0.5 | `code_refactoring` | 5 |
| `impact_analysis` | risques détectés | `test_generation` | 5 |
| `impact_analysis` | fichiers IaC impactés | `iac_guardrails_scan` | 15 |
| `iac_guardrails_scan` | score sécurité < 0.5 | `code_refactoring` | 10 |
| `repo_consistency_check` | problèmes architecturaux | `architecture_analysis` | 10 |
| `architecture_analysis` | dette technique > 0.5 | `code_refactoring` | 5 |
| `architecture_analysis` | issues critiques | `impact_analysis` | 10 |
| `repo_consistency_check` | problèmes de performance | `performance_analysis` | 10 |
| `performance_analysis` | score < 0.5 | `code_refactoring` | 5 |
| `performance_analysis` | optimisations proposées | `test_generation` | 10 |

### Exemples de chaînes

```
# Chaîne qualité complète
repo_consistency_check (score=0.8)
  → code_refactoring (refactore le code)
    → code_review (revue du code refactoré)
    → code_documentation (met à jour la doc)
    → test_generation (génère les tests)

# Chaîne sécurité
impact_analysis (détecte des fichiers IaC modifiés)
  → iac_guardrails_scan (scan de sécurité)
    → code_refactoring (corrige les vulnérabilités)

# Boucle qualité auto-corrective
code_review (quality_score=0.3)
  → code_refactoring (améliore le code)
    → code_review (re-vérifie la qualité)
```

### Protections anti-boucle

- **Profondeur max** : `max_chain_depth=5` — une chaîne ne peut pas dépasser 5 niveaux
- **Timeout global** : `chain_timeout=300s` — la chaîne entière est abandonnée après 5 minutes
- **Historique** : un expert ne peut pas être déclenché deux fois dans la même chaîne

---

## Mémoire Persistante

Le `ProjectMemory` stocke les résultats des experts dans un fichier JSON persistant (`.collegue/memory/project_memory.json`).

### Fonctionnement

```python
# Un expert stocke ses résultats après exécution
memory.store(
    expert="code_review",
    entry_type="issue_found",      # pattern_learned | issue_found | fix_applied | project_profile
    category="security",
    title="SQL injection dans get_user()",
    data={"severity": "critical", "file": "db.py"},
    score=0.9,
    language="python"
)

# Un autre expert rappelle le contexte avant exécution
context = memory.get_context_for("code_refactoring", language="python")
# → {"known_issues": [{"title": "SQL injection...", "category": "security"}]}
```

### Types d'entrées mémoire

| Type | Description | Exemple |
|------|-------------|---------|
| `pattern_learned` | Pattern récurrent identifié | "Utilisation systématique de f-strings" |
| `issue_found` | Problème détecté | "SQL injection dans get_user()" |
| `fix_applied` | Correction appliquée | "Paramétrage des requêtes SQL" |
| `project_profile` | Information sur le projet | "Framework: FastAPI, DB: PostgreSQL" |
| `expert_result` | Résultat brut d'un expert | Score, nombre de findings, etc. |

### Intégration automatique

Chaque expert LLM-backed appelle automatiquement :
- `_recall_from_memory()` **avant** exécution → injecte le contexte dans le prompt
- `_store_to_memory()` **après** exécution → sauvegarde les résultats

L'écriture est atomique (via `tempfile` + `os.replace`) pour éviter la corruption.

---

## Moniteur Proactif

Le `ProactiveMonitor` détecte les changements dans le dépôt Git et décide quels experts déclencher.

### Règles de déclenchement

| Fichier modifié | Expert(s) déclenché(s) |
|-----------------|------------------------|
| `*.py` | `code_review`, `performance_analysis` |
| `Dockerfile`, `docker-compose.yml` | `iac_guardrails_scan` |
| `*.tf`, `*.yaml` (Kubernetes) | `iac_guardrails_scan` |
| `requirements.txt`, `package.json` | `architecture_analysis` |
| `*.test.*`, `*_test.*` | `test_generation` |

### Utilisation

```python
from collegue.autonomous.proactive_monitor import get_proactive_monitor

monitor = get_proactive_monitor()
monitor.set_repo_path("/chemin/vers/mon/projet")
monitor.start()

# Scan unique
result = monitor.scan_once()
print(f"Changements détectés: {result.changes_detected}")
print(f"Experts à déclencher: {result.triggers_decided}")
for decision in result.decisions:
    print(f"  {decision.expert} (raison: {decision.reason})")
```

---

## Tableau de Bord

L'`ExpertDashboard` est un outil MCP qui agrège les scores de tous les experts et fournit un aperçu global du projet.

### Scores agrégés

| Catégorie | Sources |
|-----------|---------|
| `quality_score` | `code_review` |
| `architecture_score` | `architecture_analysis` |
| `performance_score` | `performance_analysis` |
| `security_score` | `iac_guardrails_scan`, `secret_scan` |
| `overall_score` | Moyenne pondérée des 4 scores |

### Recommandations

Le dashboard génère automatiquement des recommandations triées par priorité :

```json
{
  "project_health": {
    "overall_score": 0.70,
    "quality_score": 0.32,
    "architecture_score": 0.91,
    "performance_score": 0.85,
    "security_score": null
  },
  "recommendations": [
    {
      "priority": "high",
      "expert": "code_review",
      "action": "Le score de qualité (0.32) est faible. Exécutez code_review sur les fichiers critiques."
    }
  ]
}
```

---

## Intégration dans un Projet

### Nouveau projet

1. **Installez Collègue MCP** dans votre IDE (voir [README.md](../README.md) pour la configuration)

2. **Scan initial** — demandez à votre agent IA :
   ```
   Utilise smart_orchestrator pour analyser ce projet en profondeur.
   ```
   L'orchestrateur planifiera automatiquement les experts pertinents.

3. **Dashboard** — pour un aperçu global :
   ```
   Utilise expert_dashboard pour voir l'état de santé du projet.
   ```

4. **Moniteur proactif** — activez-le pour des analyses continues :
   ```
   Active le proactive_monitor sur ce repo.
   ```

### Projet existant

Pour intégrer le système multi-agents dans un projet existant :

1. **Ajoutez la configuration MCP** à votre IDE :
   ```json
   {
     "mcpServers": {
       "collegue": {
         "command": "npx",
         "args": ["-y", "@collegue/mcp@latest"]
       }
     }
   }
   ```

2. **Première analyse complète** :
   ```
   Fais une analyse complète de ce projet avec smart_orchestrator :
   - Revue de code (code_review)
   - Analyse d'architecture (architecture_analysis)
   - Analyse de performance (performance_analysis)
   - Scan de sécurité IaC (iac_guardrails_scan)
   - Vérification de cohérence (repo_consistency_check)
   ```

3. **La mémoire se construit automatiquement** :
   - Chaque expert stocke ses résultats dans `.collegue/memory/project_memory.json`
   - Les sessions suivantes bénéficient du contexte accumulé
   - Ajoutez `.collegue/` à votre `.gitignore` (ou committez-le pour partager la mémoire en équipe)

4. **Workflow quotidien recommandé** :
   ```
   # Avant un commit important
   Utilise impact_analysis pour analyser l'impact de mes changements.

   # Après un refactoring
   Utilise code_review pour vérifier la qualité, puis test_generation pour les tests.

   # Revue de sécurité
   Utilise iac_guardrails_scan sur mes fichiers Dockerfile et Kubernetes.
   ```

### Exemples de prompts pour l'agent IA

```
# Analyse profonde d'un fichier
Analyse le fichier src/auth/service.py en profondeur avec code_review en mode deep.

# Chaîne d'experts complète
Fais un repo_consistency_check en mode deep sur le projet, 
puis laisse la délégation automatique corriger les problèmes trouvés.

# Génération de tests ciblée
Génère des tests unitaires pour src/models/user.py avec test_generation, 
en ciblant une couverture de 80%.

# Documentation complète
Génère la documentation pour le module src/api/ avec code_documentation.

# Dashboard projet
Montre-moi le tableau de bord expert_dashboard avec les scores et recommandations.
```

---

## Configuration Avancée

### Variables d'environnement

| Variable | Description | Défaut |
|----------|-------------|--------|
| `LLM_API_KEY` | Clé API Gemini (requis pour le mode deep) | — |
| `LLM_MODEL` | Modèle LLM à utiliser | `gemini-2.5-flash` |
| `COLLEGUE_MEMORY_DIR` | Répertoire de la mémoire persistante | `.collegue/memory` |
| `COLLEGUE_MEMORY_TTL` | Durée de vie des entrées mémoire (secondes) | `2592000` (30 jours) |

### Personnaliser les règles de délégation

Les règles de délégation sont définies dans `collegue/core/expert_delegation.py`. Pour ajouter une règle personnalisée :

```python
from collegue.core.expert_delegation import DelegationRule

custom_rule = DelegationRule(
    source_tool="code_review",
    target_tool="test_generation",
    condition_name="findings critiques → générer tests de régression",
    priority=10
)
```

### Désactiver la délégation automatique

Si vous voulez utiliser les experts individuellement sans délégation :

```
Utilise code_review sur ce fichier, sans délégation automatique.
```

Le paramètre `auto_chain=False` est disponible sur la plupart des experts.

---

## Modèles LLM Supportés

Le système multi-agents a été testé avec les modèles suivants :

| Modèle | Score MCP | Recommandation |
|--------|-----------|----------------|
| `gemma-4-26b-a4b-it` | 0.982 | **Recommandé** — meilleur rapport qualité/coût |
| `gemini-3.1-pro-preview` | 0.917 | Excellent pour les analyses complexes |
| `gemini-3-flash-preview` | 0.918 | Bon compromis vitesse/qualité |
| `gemini-2.5-flash` | 0.833 | Plus rapide mais moins précis |

Les scores sont issus de la matrice d'évaluation sur 13 cas de test × 5 modèles × 3 paths (voir [docs/llm_evals.md](llm_evals.md)).
