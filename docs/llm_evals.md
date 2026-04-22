# Golden evals — qualité des sorties LLM

Les stress tests valident que les outils ne crashent pas. Cette suite **évalue la correction** : est-ce que `test_generation` produit des tests qui tournent vraiment ? Est-ce que `code_refactoring/simplify` préserve la sémantique ? Sans ça, on ne peut ni comparer deux modèles, ni détecter qu'un changement de prompt a dégradé la qualité.

Jamais exécutée en CI (trop coûteuse en LLM). Usage : local, ou nightly cron.

## Architecture

```
tests/evals/
├── eval_context.py              # Shim ctx.sample() qui appelle generate_text directement
├── runner.py                    # CLI, loader YAML, orchestrateur, writer rapport
├── scorers/
│   └── test_generation.py       # Exécute les tests générés dans pytest, score = passed/collected
├── cases/
│   └── test_generation/         # 8 cas YAML (1 prompt → 1 score)
└── reports/                     # Runtime (gitignored)
```

- **Runner in-process** : n'utilise pas le harness HTTP MCP. Instancie directement le tool et lui passe un `EvalContext` qui implémente `ctx.sample()` en déléguant à `generate_text()` (même helper que le Watchdog). Pas besoin de lancer Docker — `LLM_API_KEY` dans l'env et c'est parti.
- **Scorer rule-based** : pas de LLM-as-judge dans la v1. Pour `test_generation`, on écrit le code source + les tests générés dans un tempdir et on lance `pytest test_src.py`. Score = `passed / collected`.

## Usage

```bash
# Full run (8 cas)
LLM_API_KEY=<ta-clé> python -m tests.evals.runner --tool test_generation

# Un cas précis (itération rapide)
python -m tests.evals.runner --tool test_generation --case 01_arithmetic

# Limite aux N premiers cas (quand la quota Gemini est serrée)
python -m tests.evals.runner --tool test_generation --limit 2

# Sortie dans un dossier dédié (au lieu d'un timestamp auto)
python -m tests.evals.runner --tool test_generation --out my-run/
```

Chaque run produit :
- `<out>/report.md` — résumé markdown avec scores par cas + agrégat
- `<out>/cases/<case_id>.json` — enregistrement détaillé (raw LLM output, stdout pytest, ctx.calls)

Le runner **n'est jamais gating** (`exit 0` toujours). L'utilisateur lit le rapport et juge.

## Baseline actuel (Gemini 2.5 Flash)

Au moment de l'écriture (PR golden-evals-v1) :

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
