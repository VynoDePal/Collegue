# Golden evals — qualité des sorties LLM

Les stress tests valident que les outils ne crashent pas. Cette suite **évalue la correction** : est-ce que `test_generation` produit des tests qui tournent vraiment ? Est-ce que `code_refactoring/simplify` préserve la sémantique ? Sans ça, on ne peut ni comparer deux modèles, ni détecter qu'un changement de prompt a dégradé la qualité.

Jamais exécutée en CI (trop coûteuse en LLM). Usage : local, avant merge d'un changement de prompt ou de modèle.

## Architecture

```
tests/evals/
├── eval_context.py              # Shim ctx.sample() qui appelle generate_text directement
├── runner.py                    # CLI, loader YAML, orchestrateur, writer rapport/matrix
├── scorers/
│   └── test_generation.py       # Exécute les tests générés dans pytest, score = passed/collected
├── cases/
│   ├── test_generation/             # 13 cas — chemin via le tool MCP Collègue
│   ├── test_generation_raw/         # Mêmes 13 cas — chemin LLM direct (prompt minimal)
│   └── test_generation_competent/   # Mêmes 13 cas — chemin LLM avec prompt "utilisateur qui sait pytest"
└── reports/                     # Runtime (gitignored)
```

### Trois paths parallèles

Le runner connaît trois "tools" évalués en parallèle sur les mêmes 13 cas pour comparer trois niveaux de sophistication côté appelant :

| Path | Prompt envoyé au LLM |
|---|---|
| **`test_generation`** (MCP) | Prompt complet du `TestGenerationTool` après template + extraction d'éléments AST + contrat de sortie explicite |
| **`test_generation_competent`** | Prompt « développeur qui connaît pytest » : exige edge cases, parametrize, pytest.raises, tests runnable |
| **`test_generation_raw`** | Prompt minimal : *"Write a pytest test file for the following Python code"* |

Le matrix report calcule deux Δ qui sont les vraies mesures de valeur :

- **Δ MCP − Raw** : valeur face à un utilisateur naïf. Δ positif = le tool aide.
- **Δ MCP − Competent** : valeur face à un utilisateur compétent. Δ positif = le tool bat un prompt soigné. C'est le test honnête.

### Caractéristiques

- **Runner in-process** : n'utilise pas le harness HTTP MCP. Instancie directement le tool et lui passe un `EvalContext` qui implémente `ctx.sample()` en déléguant à `generate_text()` (même helper que le Watchdog). Pas besoin de lancer Docker — `LLM_API_KEY` dans l'env et c'est parti.
- **Scorer rule-based** pour `test_generation` : on écrit le code source + les tests générés dans un tempdir et on lance `pytest test_src.py`. Score = `passed / collected`. Pas de LLM-as-judge dans cette itération.

## Usage

```bash
# Mode simple (1 tool, 1 modèle par défaut via settings.LLM_MODEL)
LLM_API_KEY=<ta-clé> python -m tests.evals.runner --tool test_generation

# Un cas précis (itération rapide)
python -m tests.evals.runner --tool test_generation --case 01_arithmetic

# Limite aux N premiers cas (quand la quota Gemini est serrée)
python -m tests.evals.runner --tool test_generation --limit 2

# Matrix: plusieurs modèles + MCP vs raw vs competent
python -m tests.evals.runner \
    --tool test_generation --tool test_generation_raw --tool test_generation_competent \
    --model gemini-2.5-flash \
    --model gemini-3-flash-preview \
    --model gemini-3.1-pro-preview \
    --model gemma-4-26b-a4b-it \
    --model gemma-4-31b-it \
    --out tests/evals/reports/matrix-$(date -u +%Y%m%d)
```

Chaque run produit :
- `<out>/report.md` — résumé markdown avec scores par cas + agrégat
- `<out>/cases/<case_id>.json` — enregistrement détaillé (raw LLM output, stdout pytest, ctx.calls)

Le runner **n'est jamais gating** (`exit 0` toujours). L'utilisateur lit le rapport et juge.

## Workflow obligatoire : édition d'un `default.yaml`

> Quand tu modifies un `collegue/prompts/templates/tools/*/default.yaml`, **run la matrice localement avant d'ouvrir la PR** et colle les chiffres dans le body.

Commande minimale (2 modèles, 13 cas, ~20 min, ~$0.50) :

```bash
LLM_API_KEY=<ta-clé> python -m tests.evals.runner \
    --tool test_generation \
    --model gemini-2.5-flash \
    --model gemma-4-31b-it \
    --out tests/evals/reports/pre-merge-$(date -u +%Y%m%d-%H%M)
```

Critère de merge :
- **MCP avg ≥ 0.90** sur chacun des 2 modèles
- Si < 0.90 sur un modèle, investiguer avant merge (probablement une régression de template)

Rationale : aucun CI gate automatique. Les templates changent rarement, la discipline manuelle suffit à cette échelle. Si le repo devient multi-contributeur, rouvrir le débat.

## Matrice v1 (référence courante)

Run complet **195 appels LLM** sur 13 cas × 3 paths × 5 modèles.

### Scores moyens par path

| Modèle | MCP | Competent | Raw |
|---|---|---|---|
| `gemini-2.5-flash` | **0.982** | 0.667 | 0.827 |
| `gemini-3-flash-preview` | **1.000** | 0.985 | 0.838 |
| `gemini-3.1-pro-preview` | **0.987** | 0.910 | 0.308 |
| `gemma-4-26b-a4b-it` | **1.000** | 0.976 | 0.989 |
| `gemma-4-31b-it` | **1.000** | 0.971 | 0.916 |
| **Global** | **0.994** | 0.902 | 0.776 |

### Δ par modèle

#### Δ MCP − Raw (vs utilisateur naïf)

| Modèle | MCP | Raw | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.982 | 0.827 | **+0.155** |
| `gemini-3-flash-preview` | 1.000 | 0.838 | **+0.162** |
| `gemini-3.1-pro-preview` | 0.987 | 0.308 | **+0.679** |
| `gemma-4-26b-a4b-it` | 1.000 | 0.989 | **+0.011** |
| `gemma-4-31b-it` | 1.000 | 0.916 | **+0.084** |

#### Δ MCP − Competent (vs utilisateur qui sait ce qu'il fait)

| Modèle | MCP | Competent | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.982 | 0.667 | **+0.315** |
| `gemini-3-flash-preview` | 1.000 | 0.985 | **+0.015** |
| `gemini-3.1-pro-preview` | 0.987 | 0.910 | **+0.077** |
| `gemma-4-26b-a4b-it` | 1.000 | 0.976 | **+0.024** |
| `gemma-4-31b-it` | 1.000 | 0.971 | **+0.029** |

### Lecture

- **Δ MCP − Competent positif sur 5/5 modèles** — l'outil MCP apporte une valeur mesurable au-delà d'un prompt compétent sur tous les couples modèle/cas. C'est le critère de validation essentiel : si l'outil ne bat pas un dev attentif, il n'a pas de raison d'exister.
- **`gemini-3.1-pro` sans structure s'effondre** (raw avg 0.308). Ce modèle de reasoning lourd a besoin qu'on lui dise *quoi* générer. Signal fort : MCP est indispensable sur les modèles de cette catégorie.
- **`gemma-4-26b` robuste out-of-the-box** (raw avg 0.989). Sur ce modèle, MCP apporte très peu (+0.011) mais ne nuit pas. Candidat prod solide si le pricing est avantageux.
- **`gemini-2.5-flash` competent anormalement bas** (0.667). Cause : le reasoning de Gemini 2.5 consomme le max_tokens budget, laissant peu de place aux tests. Comportement du modèle, pas du tool MCP. Hors-scope de cette suite.

### Seuils de régression

- **MCP avg global < 0.95** sur une matrice 5 modèles → investiguer (v1 tient 0.994)
- **Δ MCP − Competent négatif sur ≥ 2 modèles sur 5** → red flag, l'outil n'ajoute plus de valeur par rapport à un utilisateur compétent
- **Un case individuel à 0.000 sur ≥ 3 modèles** → template ou extracteur cassé

## Format d'un cas

```yaml
# tests/evals/cases/test_generation/NN_name.yaml
name: "Human-readable description"
description: "What the case exercises (edge cases, failure modes…)"
language: python
framework: pytest
min_expected_tests: 3   # Penalty soft if LLM generates fewer tests
code: |
  def foo(x):
      return x * 2
```

Contraintes :
- Le code ne doit utiliser que **stdlib + pytest** (le scorer n'installe pas de deps dans le tempdir)
- Le code doit être un module Python standalone (pas de classe abstraite sans implémentation)
- Garder les cas courts (< 50 lignes) pour un LLM token budget raisonnable

## Scoring — détails

Pour `test_generation` :

1. Extraction du code de test depuis le `response.text` du tool
   - Cherche d'abord un fence ` ```python ... ``` ` contenant `def test_`
   - Sinon, prend le premier fence Python
   - Sinon, prend le texte brut (score souvent à 0)
2. Découverte des imports locaux via `ast.parse` — pour chaque module non-stdlib importé, crée un alias du code source sous ce nom (sinon le LLM qui fait `from my_math_module import add` voit son import échouer alors qu'on a écrit `src.py`)
3. Exécute `pytest test_src.py -q --tb=line --no-header` avec timeout 30s
4. Parse la sortie pour extraire `passed`, `failed`, `errors`, `skipped`, `collected`
5. Score :
   - `collected == 0` → **0.0** (aucun test collectable, syntax error généralement)
   - `collected < min_expected_tests` → `passed/collected × 0.7` (pénalité quantité)
   - Sinon → `passed / collected`

## Ajouter un nouveau tool

1. Créer `tests/evals/scorers/<tool_name>.py` avec une fonction `score(case: dict, tool_output: str) -> EvalScore`
2. Créer `tests/evals/cases/<tool_name>/` avec au moins 5 cas YAML
3. Ajouter une entrée dans `TOOL_REGISTRY` de [tests/evals/runner.py](../tests/evals/runner.py) :
   ```python
   TOOL_REGISTRY["refactoring"] = {
       "run": _run_refactoring,       # async callable: (case, ctx) -> tool_output
       "score": refactoring_scorer.score,
   }
   ```
4. Définir `async def _run_<tool>(case, ctx)` qui instancie le tool, construit la request, et retourne la chaîne de sortie à scorer

Exemples de scorers futurs :
- `refactoring/simplify` — AST-diff entre input et output, + exécution des tests existants pour vérifier préservation sémantique
- `impact_analysis` — précision/rappel des fichiers impactés contre une liste annotée dans le YAML
- `code_documentation` — LLM-as-judge requis (scoring subjectif)

## Limites connues

- **Gemini 2.5 et max_tokens** : le reasoning de Gemini 2.5 consomme le budget de sortie. On plancher à 8000 tokens dans `EvalContext.MIN_MAX_TOKENS` pour éviter des réponses tronquées. Augmenter si les outils demandent plus.
- **Différence avec la prod** : l'`EvalContext` route via `google.genai` (même chemin que le Watchdog), alors que le serveur MCP en prod route via `OpenAISamplingHandler` (API OpenAI-compatible). Les deux hits Gemini mais peuvent avoir des légères différences de comportement. Acceptable pour la v1.
- **Pas de LLM-as-judge** — pour `code_documentation` ou `code_refactoring` (qualités subjectives), un scorer rule-based n'est pas suffisant. À introduire quand on ajoute ces outils.
- **Quota Gemini Free Tier (20 req/jour)** : 13 cas × 3 paths = 39 req par modèle sur une matrice complète. Pour itérer sans péter le quota : `--limit 1` sur un cas précis pendant le debug, ou restreindre à un seul path/modèle.
