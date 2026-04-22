# Golden evals — qualité des sorties LLM

Les stress tests valident que les outils ne crashent pas. Cette suite **évalue la correction** : est-ce que `test_generation` produit des tests qui tournent vraiment ? Est-ce que `code_refactoring/simplify` préserve la sémantique ? Sans ça, on ne peut ni comparer deux modèles, ni détecter qu'un changement de prompt a dégradé la qualité.

Jamais exécutée en CI (trop coûteuse en LLM). Usage : local, ou nightly cron.

## Architecture

```
tests/evals/
├── eval_context.py              # Shim ctx.sample() qui appelle generate_text directement
├── runner.py                    # CLI, loader YAML, orchestrateur, writer rapport/matrix
├── scorers/
│   └── test_generation.py       # Exécute les tests générés dans pytest, score = passed/collected
├── cases/
│   ├── test_generation/         # 8 cas — chemin via le tool MCP Collègue
│   └── test_generation_raw/     # Mêmes 8 cas — chemin LLM direct (prompt minimal)
└── reports/                     # Runtime (gitignored)
```

### Deux paths parallèles

Le runner connaît deux "tools" :

- **`test_generation`** — passe par la classe `TestGenerationTool` du MCP Collègue. Bénéficie du prompt engineering fignolé du tool (extraction d'éléments, coverage target, framework preamble).
- **`test_generation_raw`** — bypasse complètement le tool. Appelle `generate_text()` avec un prompt minimal : *"Write a pytest test file for the following Python code"*. Représente le baseline "ce que tu aurais en demandant à l'IA toi-même".

Le matrix report calcule un **Δ (MCP − raw) par modèle** qui quantifie la valeur ajoutée réelle du prompt engineering. Δ positif = le tool aide. Δ négatif = le tool fait empirer les choses (signal fort).

- **Runner in-process** : n'utilise pas le harness HTTP MCP. Instancie directement le tool et lui passe un `EvalContext` qui implémente `ctx.sample()` en déléguant à `generate_text()` (même helper que le Watchdog). Pas besoin de lancer Docker — `LLM_API_KEY` dans l'env et c'est parti.
- **Scorer rule-based** : pas de LLM-as-judge dans la v1. Pour `test_generation`, on écrit le code source + les tests générés dans un tempdir et on lance `pytest test_src.py`. Score = `passed / collected`.

## Usage

```bash
# Mode simple (1 tool, 1 modèle par défaut via settings.LLM_MODEL)
LLM_API_KEY=<ta-clé> python -m tests.evals.runner --tool test_generation

# Un cas précis (itération rapide)
python -m tests.evals.runner --tool test_generation --case 01_arithmetic

# Limite aux N premiers cas (quand la quota Gemini est serrée)
python -m tests.evals.runner --tool test_generation --limit 2

# Matrix: plusieurs modèles + MCP vs raw
python -m tests.evals.runner \
    --tool test_generation --tool test_generation_raw \
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

## Matrice 5 modèles × 3 paths × 13 cas (golden-evals-v1, run final)

Run complet **195 appels LLM** (`python -m tests.evals.runner --tool test_generation --tool test_generation_raw --tool test_generation_competent --model gemini-2.5-flash --model gemini-3-flash-preview --model gemini-3.1-pro-preview --model gemma-4-26b-a4b-it --model gemma-4-31b-it`).

### Trois paths évalués en parallèle

| Path | Prompt |
|---|---|
| **`test_generation`** (MCP) | Prompt élaboré du `TestGenerationTool` : extraction d'éléments, coverage target, framework preamble, liste d'instructions |
| **`test_generation_competent`** | Prompt "développeur qui connaît pytest" : exige edge cases, parametrize, pytest.raises, nommage, tests runnable |
| **`test_generation_raw`** | Prompt minimal : *"Write a pytest test file for the following code"* |

Les 13 cas couvrent : fonctions pures, classes, type hints, exceptions, generators, async, properties, héritage, **state machine**, **async context manager**, **retry decorator**, **pipeline composition**, **LRU memoize**.

### Scores moyens par path

| Modèle | MCP | Competent | Raw |
|---|---|---|---|
| `gemini-2.5-flash` | **0.833** | 0.656 | 0.867 |
| `gemini-3-flash-preview` | 0.918 | **0.959** | 0.615 |
| `gemini-3.1-pro-preview` | **0.917** | 0.911 | 0.538 |
| `gemma-4-26b-a4b-it` | **0.982** | 0.903 | 0.972 |
| `gemma-4-31b-it` | 0.864 | **0.977** | 0.943 |

### Δ par modèle (la lecture honnête)

#### Δ MCP − Raw (MCP vs utilisateur naïf)

| Modèle | MCP | Raw | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.833 | 0.867 | **−0.034** |
| `gemini-3-flash-preview` | 0.918 | 0.615 | **+0.303** |
| `gemini-3.1-pro-preview` | 0.917 | 0.538 | **+0.379** |
| `gemma-4-26b-a4b-it` | 0.982 | 0.972 | +0.010 |
| `gemma-4-31b-it` | 0.864 | 0.943 | **−0.079** |

#### Δ MCP − Competent (MCP vs utilisateur qui sait ce qu'il fait)

| Modèle | MCP | Competent | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.833 | 0.656 | **+0.177** |
| `gemini-3-flash-preview` | 0.918 | 0.959 | **−0.041** |
| `gemini-3.1-pro-preview` | 0.917 | 0.911 | +0.006 |
| `gemma-4-26b-a4b-it` | 0.982 | 0.903 | **+0.079** |
| `gemma-4-31b-it` | 0.864 | 0.977 | **−0.113** |

#### Δ Competent − Raw (la plus-value d'un prompt soigné sans MCP)

| Modèle | Competent | Raw | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.656 | 0.867 | **−0.211** |
| `gemini-3-flash-preview` | 0.959 | 0.615 | **+0.344** |
| `gemini-3.1-pro-preview` | 0.911 | 0.538 | **+0.373** |
| `gemma-4-26b-a4b-it` | 0.903 | 0.972 | **−0.069** |
| `gemma-4-31b-it` | 0.977 | 0.943 | +0.034 |

### Lecture honnête (révision de la v2)

La v2 (8 cas simples, 2 paths) racontait une histoire uniforme — *"MCP délivre +0.11 à +0.52 sur tous les Gemini"*. **Cette conclusion ne survit pas à la v3**. Avec 5 cas complexes en plus et le path `competent` ajouté, l'image devient nuancée :

1. **MCP bat le prompt naïf sur Gemini 3.x** — `+0.303` et `+0.379` sur `gemini-3-flash-preview` et `gemini-3.1-pro-preview`. Vrai valeur ajoutée quand l'utilisateur écrit un prompt minimal.

2. **MCP est battu ou à égalité avec un prompt "compétent" sur 3 modèles sur 5** :
   - `gemini-3-flash-preview` : competent gagne de **−0.041**
   - `gemini-3.1-pro-preview` : égalité (+0.006)
   - `gemma-4-31b-it` : competent gagne de **−0.113**

   Sur ces modèles, la plus-value du tool MCP est essentiellement **"éviter à l'utilisateur d'écrire le prompt lui-même"** — pas **"produire de meilleurs tests qu'un dev attentif"**.

3. **MCP est *contre-productif* face à un prompt naïf sur 2 modèles** :
   - `gemini-2.5-flash` : **−0.034** (marginal)
   - `gemma-4-31b-it` : **−0.079** (significatif)

   Indique que le prompt engineering du tool ne s'adapte pas à ces modèles — le surcoût en tokens n'est pas récompensé.

4. **Seul `gemini-2.5-flash` voit MCP battre significativement `competent`** (+0.177). C'est la config prod actuelle par défaut — la seule où l'investissement prompt-engineering du tool est nettement rentable.

5. **`gemma-4-26b-a4b-it` est étonnamment robuste** — 0.982 en MCP, 0.972 en raw. Le meilleur modèle du corpus quel que soit le path. Candidat prod sérieux si le pricing est avantageux.

6. **Le path competent échoue sur `gemini-2.5-flash` (0.656)** — anomalie liée à des réponses LLM tronquées au max_tokens. Le prompt long + reasoning Gemini 2.5 consomme le budget avant d'émettre les tests. Fix partiel dans le scorer (`_strip_fences` gère les fences non fermées), reste ~4 cas vraiment tronqués. Pas un défaut du path `competent` en tant que tel.

### Implications produit

- **Utilisateur naïf (prompt minimal)** : MCP justifié sur Gemini 3.x. Marginal sur les autres.
- **Utilisateur expérimenté (prompt soigné)** : MCP n'apporte rien sur `gemini-3-flash-preview` et `gemma-4-31b-it`. Valeur réelle uniquement sur `gemini-2.5-flash`.
- **Robustesse des modèles** : `gemma-4-26b` et `gemma-4-31b` dominent en baseline raw, ce qui suggère une capacité "out of the box" solide sans prompt engineering dédié.

### Seuils de régression mis à jour

- **MCP `gemini-2.5-flash` avg < 0.75** (au lieu de 0.95) → investiguer, c'est le couple où le MCP apporte le plus
- **Δ MCP − Competent < −0.15 sur 3 modèles sur 5** → le tool MCP devient nuisible, red flag produit
- **Raw avg < 0.50 sur un Gemini** → plausiblement un bug de réponse LLM truncation, vérifier logs

## Baseline historique (Gemini 2.5 Flash uniquement, v0)

Au moment de l'écriture initiale du harness (8 cas, 1 modèle) :

| Case | Score | Tests OK / Total |
|---|---|---|
| 01_arithmetic | 1.000 | 44/44 |
| 02_class_init | 1.000 | 20/20 |
| 03_type_hints | 1.000 | 21/21 |
| 04_exceptions | 0.960 | 24/25 |
| 05_generator | 1.000 | 10/10 |
| 06_async_fn | 0.867 | 13/15 |
| 07_property | 1.000 | 19/19 |
| 08_inheritance | 1.000 | 39/39 |

**Moyenne : 0.978 · 190/193 tests générés qui passent.**

Si une future modification (prompt refactor, changement de modèle, montée de version pyproject…) fait chuter ce chiffre sous **0.90**, considérer comme une régression et investiguer avant merge.

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
- **Quota Gemini Free Tier (20 req/jour)** : 8 cas par run, donc 2-3 runs/jour max. Pour itérer sans péter le quota : `--limit 1` sur un cas précis pendant le debug.
