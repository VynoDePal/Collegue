# Golden evals — qualité des sorties LLM

Les stress tests valident que les outils ne crashent pas. Cette suite **évalue la correction** : est-ce que `test_generation` produit des tests qui tournent vraiment ? Est-ce que `code_refactoring/simplify` préserve la sémantique ? Sans ça, on ne peut ni comparer deux modèles, ni détecter qu'un changement de prompt a dégradé la qualité.

Jamais exécutée en CI (trop coûteuse en LLM). Usage : local, avant merge d'un changement de prompt ou de modèle.

## Architecture

```
tests/evals/
├── eval_context.py              # Shim ctx.sample() qui appelle generate_text directement
├── runner.py                    # CLI, loader YAML, orchestrateur, writer rapport/matrix
├── scorers/
│   ├── test_generation.py       # Rule-based — lance pytest, score = passed/collected
│   └── code_documentation.py    # LLM-as-judge — 4-axis rubric, gemini-2.5-flash fixed
├── cases/
│   ├── test_generation/             # 13 cas — chemin via le tool MCP Collègue
│   ├── test_generation_raw/         # Mêmes 13 cas — chemin LLM direct (prompt minimal)
│   ├── test_generation_competent/   # Mêmes 13 cas — chemin LLM avec prompt "utilisateur qui sait pytest"
│   ├── code_documentation/          # 13 cas — chemin via le tool MCP code_documentation
│   ├── code_documentation_raw/      # Mêmes 13 cas — chemin LLM direct
│   └── code_documentation_competent/# Mêmes 13 cas — chemin LLM "dev qui connaît la doc ref"
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
- **Deux types de scorers** :
  - **Rule-based** pour `test_generation` : on écrit le code source + les tests générés dans un tempdir et on lance `pytest test_src.py`. Score = `passed / collected`. Objectif et déterministe.
  - **LLM-as-judge** pour `code_documentation` : un second appel LLM note la doc sur 4 axes (accuracy, completeness, clarity, usefulness) × 0-5. Score = `mean(axes) / 5`. Le judge est `gemini-2.5-flash` pinné à `temperature=0.0` pour stabilité. Nécessaire dès qu'on mesure un output subjectif (pas d'oracle).

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

## Matrice `code_documentation` v1

Premier run LLM-as-judge du repo. 78 appels générateur + 78 appels judge = **156 LLM calls** sur 13 cas × 3 paths × 2 modèles (`gemini-2.5-flash` + `gemma-4-31b-it`).

### Rubric LLM-as-judge

Le judge (`gemini-2.5-flash` pinné, `temperature=0.0`) note chaque doc sur **4 axes × 0-5** :

| Axe | 0 | 3 | 5 |
|---|---|---|---|
| **accuracy** | APIs inventées, signatures fausses | Mostly correct, 1 misrep mineur | Every claim verifiable contre le code |
| **completeness** | Surface publique manquante | Main entities documentées, 1 symbole manque | Tous symboles + params + returns + exceptions |
| **clarity** | Markdown cassé, structure confuse | Lisible mais inégal | Bien organisé, scannable, terminology consistant |
| **usefulness** | Lecteur doit lire le code | Common case couvert, edge cases manquent | Dev utilise correctement la code depuis la doc seule |

Score final = `mean(axes) / 5` → 0-1.

Prompt du judge : G-Eval style, paragraphe `reasoning` ≤ 60 mots **avant** le JSON. `max_tokens=4000` (le reasoning interne de Gemini 2.5 consomme 1-2k tokens silencieusement).

### Scores moyens par path

| Modèle | MCP | Competent | Raw |
|---|---|---|---|
| `gemini-2.5-flash` | 0.985 | **1.000** | **1.000** |
| `gemma-4-31b-it` | 0.931 | **0.969** | **0.977** |
| **Global** | **0.958** | **0.985** | **0.988** |

### Δ par modèle

#### Δ MCP − Raw (vs utilisateur naïf)

| Modèle | MCP | Raw | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.985 | 1.000 | **−0.015** |
| `gemma-4-31b-it` | 0.931 | 0.977 | **−0.046** |

#### Δ MCP − Competent (vs utilisateur qui sait)

| Modèle | MCP | Competent | **Δ** |
|---|---|---|---|
| `gemini-2.5-flash` | 0.985 | 1.000 | **−0.015** |
| `gemma-4-31b-it` | 0.931 | 0.969 | **−0.038** |

### Lecture — MCP ne bat pas les baselines

**Les deux Δ sont négatifs sur les 2 modèles.** Contrairement à `test_generation` où MCP apportait +0.15 à +0.68 sur Δ MCP−Raw, ici MCP **perd** de 1 à 5 points à tous les coups.

Cause racine identifiée en inspectant un cas qui perd (`08_inheritance` sur gemini-2.5-flash, MCP=0.85 vs raw=1.00) :

- Le template MCP actuel impose un *« Output ONLY the documentation content — no preamble »*. Le modèle obéit littéralement et commence directement par `### class Shape`, sans vue d'ensemble du module.
- Le judge pénalise cette absence sur l'axe **Clarity** (3/5) et **Usefulness** (4/5) : *« no module-level overview, reader has to figure out intent »*.
- Le path **raw** n'a pas cette contrainte, donc le modèle écrit naturellement un paragraphe d'intro (*« This module defines an abstract base class Shape... »*), que le judge note 5/5 partout.

La contrainte « no preamble » qui marche pour `test_generation` (où le préambule en prose casse l'import pytest) **ne se transpose pas** à la documentation, où une vue d'ensemble EST une partie utile du livrable. Follow-up : reformuler le contrat de sortie du template documentation sans cette interdiction.

### Autres observations

- **Ceiling effect attendu** : 8/13 cas scorent 1.000 sur les 3 paths × 2 modèles. Les cas simples (arithmetic, class_init, type_hints) sont trop faciles pour différencier. Les 5 cas complexes (state_machine, async_context, decorator_retry, pipeline_compose, lru_memoize) portent le signal.
- **Judge family-bias confirmé probable** : le judge `gemini-2.5-flash` note gemini-2.5-flash-generated docs à 0.985, gemma-4-31b-it à 0.931. Un écart de +0.054 pour le modèle de la même famille sans qu'il produise objectivement des docs meilleures. Mitigation : lire les **deltas** pas les absolus. Ici les deltas sont comparables (négatifs des deux côtés, magnitude similaire), donc le biais ne change pas la conclusion.
- **0 errors sur 78 cases** : pipeline robuste, parsing JSON du judge marche, aucun call crashé.

### Seuils de régression

- **MCP avg global < 0.85** → investiguer (v1 tient 0.958)
- **MCP avg < 0.85 sur un modèle individuel** → régression sur ce modèle (v1 : 0.931 gemma, 0.985 gemini)
- **0 errors / 78** : si ce nombre monte sur un futur run, soit le judge est cassé, soit la rate limit Gemini a frappé

### Follow-up ouvert : reformuler le template doc

Suite directe de cette matrice v1 : issue séparée à ouvrir pour reformuler [collegue/prompts/templates/tools/documentation/default.yaml](../collegue/prompts/templates/tools/documentation/default.yaml) — retirer *« Output ONLY the documentation content — no preamble »* et autoriser (voire demander) une vue d'ensemble module-level. Attendre qu'une matrice v2 montre Δ MCP − Competent ≥ +0.05 sur au moins 1 modèle avant de considérer le tool documentation comme apportant de la valeur.

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

### `test_generation` (rule-based pytest)

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

### `code_documentation` (LLM-as-judge)

1. Construit un prompt judge avec 3 blocs : `<code_under_test>`, `<must_document>` (symboles publics attendus), `<generated_documentation>`
2. Appelle `gemini-2.5-flash` (pinné, `temperature=0.0`, `max_tokens=4000`)
3. Parse la réponse : regex `\{[^{}]*\}` + `json.loads`, prend le dernier match valide (le judge émet souvent un paragraphe `reasoning` avant le JSON)
4. Valide que les 4 axes (`accuracy`, `completeness`, `clarity`, `usefulness`) sont des int 0-5
5. Score :
   - JSON invalide / axe hors-bornes → **0.0** avec `errors=1`
   - Doc output vide → **0.0** avec `errors=1`
   - Sinon → `sum(axes) / 20.0` (normalisation en 0-1)

Pourquoi pas ensemble (3 appels médiane) : coût × 3 pour une réduction de noise ~√3. On préfère re-runner la matrice 3 fois et comparer les moyennes par cellule si le noise floor devient un problème.

Pourquoi pas un judge cross-family : `openai`/`anthropic` demanderaient une clé API + infra de provider supplémentaire. Known limitation ; mitigée en lisant les deltas, pas les absolus.

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
