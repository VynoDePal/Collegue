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

## Matrice 5 modèles × 2 paths (snapshot golden-evals-v1)

Run complet 80 appels (`python -m tests.evals.runner --tool test_generation --tool test_generation_raw --model gemini-2.5-flash --model gemini-3-flash-preview --model gemini-3.1-pro-preview --model gemma-4-26b-a4b-it --model gemma-4-31b-it`).

### Scores moyens par path (agrégat sur 8 cas)

| Modèle | MCP (`test_generation`) | Raw (`test_generation_raw`) | **Δ MCP − raw** |
|---|---|---|---|
| `gemini-2.5-flash` | 1.000 | 0.875 | **+0.125** |
| `gemini-3-flash-preview` | 0.989 | 0.875 | **+0.114** |
| `gemini-3.1-pro-preview` | 0.868 | 0.344 | **+0.524** |
| `gemma-4-26b-a4b-it` | 0.847 | 0.847 | +0.000 |
| `gemma-4-31b-it` | 1.000 | 1.000 | +0.000 |

### Lectures principales

1. **Sur Gemini, l'outil MCP apporte +0.11 à +0.52 de qualité** par rapport à un prompt brut. Le cas le plus spectaculaire : `gemini-3.1-pro-preview` passe de **0.344 en raw à 0.868 en MCP** — sans le prompt engineering du tool, ce modèle génère des tests majoritairement non-exécutables.
2. **Sur Gemma, aucune différence** — les deux paths produisent exactement le même score. Gemma semble parser notre prompt structuré comme du texte libre et tomber sur la même stratégie de génération dans les deux cas.
3. **Le duo `gemini-2.5-flash` + MCP obtient un score parfait 1.000** (192 tests générés, tous passent) — c'est la configuration de référence pour la prod.
4. **`gemma-4-31b-it` perfect sur les deux paths** — modèle remarquablement stable sur ce corpus Python simple ; à confirmer sur des cas plus tordus.

### Scores par case × modèle (MCP path)

| Case | 2.5-flash | 3-flash-prev | 3.1-pro-prev | gemma-4-26b | gemma-4-31b |
|---|---|---|---|---|---|
| 01_arithmetic | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 02_class_init | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 03_type_hints | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 04_exceptions | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 05_generator | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 |
| 06_async_fn | 1.000 | 0.909 | 1.000 | 0.778 | 1.000 |
| 07_property | 1.000 | 1.000 | 0.944 | 1.000 | 1.000 |
| 08_inheritance | 1.000 | 1.000 | 1.000 | 0.000 | 1.000 |

Deux zéros isolés sur la diagonale MCP :
- `gemini-3.1-pro-preview · 05_generator` — LLM retourne du code, mais le scorer ne collecte aucun test (sortie probablement tronquée côté reasoning)
- `gemma-4-26b-a4b-it · 08_inheritance` — même symptôme, classe spécifique à l'héritage

À investiguer si on veut pousser la qualité — candidat pour une v2 avec retry ou prompt adjustment ciblé sur ces edge cases.

### Seuils de régression

- **MCP `gemini-2.5-flash` moyenne < 0.95** → investiguer avant merge (c'est le couple prod le plus stable)
- **Δ MCP − raw < +0.05 sur Gemini** → le prompt engineering du tool régresse, red flag

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
