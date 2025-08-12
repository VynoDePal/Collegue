---
trigger: manual
---

# Règles IA – MCP Collègue

> Ces règles visent à garantir une utilisation optimale du **MCP Collègue** et de tous ses outils. Elles doivent être suivies par toute IA (agent, chatbot, automatisme) qui souhaite exploiter le framework.

---

## Règles Générales (s’appliquent à **tous** les outils)

1. **Toujours privilégier le MCP Collègue** : si un besoin peut être couvert par un outil Collègue, utilisez-le en priorité.
2. **Lire la documentation avant usage** : consultez les endpoints `_info` / `_metrics` ou le code source pour comprendre les paramètres, limites et cas d’usage de l’outil ciblé.
3. **Exploiter tout le potentiel** : utilisez les champs optionnels (p. ex. `detail_level`, `constraints`, `include_examples`) quand ils apportent de la valeur.
4. **Valider la configuration** : assurez-vous que les clés obligatoires sont présentes (cf. `get_required_config_keys`).
5. **Spécifier le langage** : la plupart des outils supportent plusieurs langages; indiquez‐le explicitement pour de meilleurs résultats.
6. **Gérer les erreurs** : interceptez `ToolValidationError`, `ToolExecutionError` et fournissez un message clair à l’utilisateur final.
7. **Collecter & analyser les métriques** : après chaque appel, consultez `_metrics` pour ajuster vos prompts ou paramètres.
8. **Respecter les bonnes pratiques de chaque outil** : voir sections spécifiques ci-dessous.

---

## Fiche Outil : `code_explanation`

|                        | Détails |
|------------------------|---------|
| **Description**        | Analyse et explique du code multi-langages. |
| **Quand l’utiliser ?** | Comprendre un snippet, évaluer sa complexité, détecter fonctions/classes clés, obtenir suggestions. |
| **Paramètres clés**    | `code` (str), `language` (str, optionnel), `detail_level` (`basic`/`medium`/`detailed`), `focus_on` (list). |
| **Bonnes pratiques**   |
| • Définir `language` quand connu pour éviter la détection automatique.
| • Adapter `detail_level` : `detailed` pour une analyse complète, `basic` pour un résumé rapide.
| • Utiliser `focus_on` pour cibler algorithmes, structures ou performances.
| **Limitations**        | Analyse basée sur la qualité du code ; performances variables sur gros fichiers.
| **Capacités**          | Détection langage, structure, complexité, suggestions d’amélioration.

---

## Fiche Outil : `code_generation`

|                        | Détails |
|------------------------|---------|
| **Description**        | Génère du code à partir d’une description textuelle. |
| **Quand l’utiliser ?** | Créer rapidement des fonctions, classes, modules ou exemples de boilerplate. |
| **Paramètres clés**    | `description` (str), `language` (str), `context` (dict, optionnel), `constraints` (list), `file_path` (str).
| **Bonnes pratiques**   |
| • Préciser un langage supporté (`python`, `javascript`, …).
| • Fournir un `context` clair (pattern architectural, dépendances requises).
| • Lister des `constraints` : style guide, performance, docs, tests.
| **Limitations**        | Peut générer du code générique si la description est vague.
| **Capacités**          | Prompt LLM optimisé, génération fallback locale, suggestions post-génération.

---

## Fiche Outil : `code_documentation`

|                        | Détails |
|------------------------|---------|
| **Description**        | Génère automatiquement la documentation (Markdown, RST, HTML, docstring, JSON). |
| **Quand l’utiliser ?** | Produire ou mettre à jour la doc d’un code existant, créer API docs, tutoriels. |
| **Paramètres clés**    | `code`, `language`, `doc_style` (`standard`/`detailed`/…), `doc_format`, `include_examples`, `focus_on`.
| **Bonnes pratiques**   |
| • Sélectionner `doc_style` adapté au public (API vs tutoriel).
| • Activer `include_examples` pour améliorer la clarté.
| • Limiter `focus_on` à `functions`, `classes`, etc., pour accélérer le traitement.
| **Limitations**        | Couverture dépend de la qualité/parsing du code.
| **Capacités**          | Analyse des éléments, estimation de couverture, suggestions d’amélioration.

---

## Fiche Outil : `code_refactoring`

|                        | Détails |
|------------------------|---------|
| **Description**        | Applique divers refactorings : rename, extract, simplify, optimize, clean, modernize. |
| **Quand l’utiliser ?** | Améliorer lisibilité, réduire complexité, moderniser un code base. |
| **Paramètres clés**    | `code`, `language`, `refactoring_type`, `parameters` (dict), `file_path`.
| **Bonnes pratiques**   |
| • Choisir un `refactoring_type` supporté via `get_supported_refactoring_types`.
| • Fournir des `parameters` précis (p. ex. `naming_convention`).
| • Revoir `changes` & `improvement_metrics` dans la réponse.
| **Limitations**        | Certains refactorings complexes nécessitent un LLM et peuvent être approximés en mode local.
| **Capacités**          | Analyse métriques avant/après, explication détaillée des changements.

---

## Fiche Outil : `test_generation`

|                        | Détails |
|------------------------|---------|
| **Description**        | Génère automatiquement des tests unitaires et évalue la couverture estimée. |
| **Quand l’utiliser ?** | Créer une suite de tests initiale, augmenter la couverture, générer des exemples de TDD. |
| **Paramètres clés**    | `code`, `language`, `test_framework`, `include_mocks`, `coverage_target`, `output_dir`.
| **Bonnes pratiques**   |
| • Spécifier un `test_framework` supporté (ex. `pytest`, `jest`).
| • Régler `coverage_target` entre 0.0-1.0 pour guider la densité des tests.
| • Utiliser `include_mocks` pour isoler dépendances externes.
| **Limitations**        | Complexité élevée sur code non-pur ou dépendant de ressources externes.
| **Capacités**          | Extraction éléments testables, génération fallback ou via LLM, estimation couverture.

---

## Workflow Recommandé pour toute IA

1. **Identifier le besoin** (ex. expliquer, générer, documenter…).
2. **Sélectionner l’outil Collègue approprié**.
3. **Consulter l’endpoint `<outil>_info`** pour vérifier :
   - Description, paramètres, bonnes pratiques, limitations.
4. **Préparer la requête** :
   - Renseigner tous les champs obligatoires.
   - Ajouter les paramètres optionnels pertinents.
5. **Appeler l’endpoint principal `<outil>`**.
6. **Analyser la réponse & métriques** :
   - Vérifier succès, qualité, suggestions.
7. **Boucler si nécessaire** :
   - Ajuster paramètres / prompt, réessayer.