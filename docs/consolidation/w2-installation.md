# Vague 2 (B) — distribution installable et dépendances verrouillées

Contrats livrés par le lot B de la vague 2. Les commandes ci-dessous sont celles qui ont été exécutées (voir le rapport
`reports/w2-b.md`).

## 1. Problèmes établis (base `9862b39`)

- Le wheel contenait 237 entrées et **zéro** YAML/JSON, **zéro** `SKILL.md`, **zéro** migration Alembic.
- Les loaders supposaient le checkout (`Path(__file__).parent.parent.parent / "skills"`), le répertoire courant ou `/app`.
- `pyproject.toml` et `requirements*.txt` divergeaient (dashboard `streamlit`/`pandas`, pin de sécurité `aiohttp>=3.14.0`,
  `pytest`) et CI/Docker installaient des plages non verrouillées : le résultat changeait avec la date.
- Le moteur de prompts écrivait ses fichiers **dans le paquet** (`collegue/prompts/templates/templates/`, `versions/`).

## 2. Source unique et verrous

`pyproject.toml` est la **seule** source des dépendances :

| Où | Contenu |
|---|---|
| `[project].dependencies` | runtime du serveur MCP (dont le pin de sécurité `aiohttp>=3.14.0` et `fastapi!=0.136.3`) |
| `[project.optional-dependencies]` | `dashboard` (streamlit, pandas), `dev` (pytest, pytest-asyncio, pytest-cov, ruff, setuptools) |
| `[dependency-groups]` (PEP 735, jamais installés avec le paquet) | `lint`, `audit`, `sandbox`, `sandbox-openhands` |

**Mécanisme de verrouillage** : `uv pip compile --universal --generate-hashes --exclude-newer <date>` via
`scripts/locks.py`. Un verrou par cible, **universel** (un seul fichier valable pour Python 3.11 et 3.12, marqueurs
d'environnement), avec les empreintes SHA-256 de tous les fichiers distribués. `--exclude-newer` (date consignée dans
l'en-tête) rend la résolution rejouable.

| Verrou | Source | Utilisé par |
|---|---|---|
| `locks/lint.txt` | groupe `lint` (ruff 0.16.9) | job **Ruff** |
| `locks/runtime.txt` | dépendances + extra `dashboard` | `docker/collegue/Dockerfile` (serveur MCP **et** dashboard), `scripts/verify_wheel.py` |
| `locks/dev.txt` | dépendances + `dashboard` + `dev` | jobs **Pytest (Python 3.11/3.12)**, nightly |
| `locks/audit.txt` | dépendances + `dashboard` + groupe `audit` | job **Dependency audit** |
| `locks/sandbox.txt` | groupe `sandbox` (pytest<9, pytest-asyncio 0.23.6, playwright, pip-audit) | `docker/sandbox/Dockerfile` |
| `locks/sandbox-openhands.txt` | groupe `sandbox-openhands` (closure OpenHands) | `docker/sandbox/Dockerfile.openhands` |

`requirements.txt` et `requirements-dev.txt` sont **générés** : ils ne font qu'inclure `locks/runtime.txt` /
`locks/dev.txt`. Installation de référence :

```
pip install --require-hashes --no-deps -r locks/<cible>.txt
```

Chaque verrou commence par un en-tête (`target`, `source-sha256`, `exclude-newer`, version d'`uv`).

### Commandes

```
python scripts/locks.py generate [cible…] [--exclude-newer 2026-10-08T00:00:00Z]   # réseau + uv ; réécrit locks/ et requirements*.txt
python scripts/locks.py check [cible…]                                            # hors ligne : dérive source -> verrou
python scripts/locks.py check --recompile                                         # réseau + uv : une résolution fraîche reproduit les verrous
```

`check` échoue (code 1, annotation `::error::`) si : un verrou manque ou n'est pas généré par l'outil ; l'empreinte de
source (`source-sha256`, calculée sur les dépendances, extras et groupes utilisés + paramètres) ne correspond plus à
`pyproject.toml` (**dérive**) ; une entrée n'est pas épinglée `==` ou n'a aucune empreinte ; une dépendance directe de la
source est absente du verrou ou hors de son intervalle ; `requirements*.txt` ne se limite plus à l'inclusion du verrou.
Ajouter une dépendance, resserrer un intervalle ou éditer un verrou à la main sont ainsi détectés (tests réels sur des
copies dérivées : `tests/test_dependency_locks.py`).

### Procédure pour changer une dépendance

1. Modifier **uniquement** `pyproject.toml`.
2. `python scripts/locks.py generate` (ou une cible) puis commiter `pyproject.toml`, `locks/` et `requirements*.txt`.
3. Les jobs CI vérifient la cohérence ; les images se reconstruisent depuis les verrous.

### Groupe OpenHands (patch Gemma 4)

`scripts/patch_openhands_gemma4_terminal.py` n'accepte que la préimage `openhands-ai 1.7.0` / `openhands-sdk` et
`openhands-tools 1.19.1`. Le verrou `sandbox-openhands.txt` fige ces versions **et** `lmnr==0.7.52` (la 0.7.53 a retiré
`observe(rollout_entrypoint=…)`), `opentelemetry-semantic-conventions==0.60b1`, `pytest<9` et `pytest-asyncio==0.23.6`
(pytest 9.0 a retiré `FixtureDef`). Closure séparée (résolution en 3.12, `docker/sandbox/Dockerfile.openhands` est
`python:3.12-slim`) : la closure OpenHands est incompatible avec 3.11 et ne contamine pas le runtime. Le Dockerfile installe
d'abord le verrou (un seul `uv pip install --require-hashes`, `uv` épinglé à 0.9.28), **puis** exécute le patch, qui
vérifie toujours versions et SHA-256 de la source : le patch n'est pas désactivé, une dérive casse le build. Le test
`test_openhands_lock_keeps_the_versions_required_by_the_gemma4_patch` fait échouer la suite si le verrou s'en écarte.
La CI reste la preuve de construction de l'image (pas d'installation OpenHands locale).

### `pip-audit` dans les images sandbox

`pip-audit` est installé (verrouillé) dans `docker/sandbox/Dockerfile` et `docker/sandbox/Dockerfile.openhands`, ce qui lève
le refus « outil absent » de `collegue/improve/metrics.py`. **Portée** : l'outil seul est embarqué. Ces conteneurs tournent
sans réseau : sans accès à la base de vulnérabilités, `pip-audit` échoue ou sort un JSON incomplet et la mesure reste
**indisponible** (échec de mesure, jamais « zéro vulnérabilité »). L'embarquer ne prétend pas fournir une base hors ligne.

## 3. Ressources dans le wheel

Les ressources non Python vivent **dans** le paquet et sont déclarées par `[tool.setuptools.package-data]` :

| Ressource | Emplacement dans le paquet |
|---|---|
| Skills (`SKILL.md` + fichiers annexes) | `collegue/skills/` (**déplacé** depuis `skills/` à la racine) |
| Graines de prompts | `collegue/prompts/templates/categories.json`, `collegue/prompts/templates/tools/**/*.yaml` |
| Règles IaC | `collegue/tools/rules/*.yaml` |
| Migrations Alembic | `collegue/migrations/` (`env.py`, `script.py.mako`, `versions/*.py`) |

Résolution : `collegue/pkgdata.py` (`resource_dir`, `resource_file`) s'appuie sur `importlib.resources` — jamais sur le
répertoire courant, `/app` ou un lien vers le checkout. Une ressource absente lève `FileNotFoundError` (message explicite),
sans repli silencieux qui masquerait un oubli de packaging.

**Overrides opérateur conservés** : `COLLEGUE_SKILLS_DIR` (un override invalide n'est pas remplacé en silence par les skills
embarquées), `storage_dir` / `templates_dir` des moteurs de prompts, `STATE_DATABASE_URL`, `COLLEGUE_HOME`.
Les anciens replis `cwd/skills` et `/app/skills` sont supprimés.

### Prompts : graines en lecture seule, état modifiable séparé

- **Graines** (dans le paquet, jamais écrites) : templates YAML et `categories.json`.
- **État modifiable** : `$COLLEGUE_HOME/prompts/` (`categories.json`, `templates/*.json`, `versions/versions.json`) —
  l'état applicatif est séparé du site-packages, qui peut être en lecture seule (testé en retirant le droit d'écriture).
  Dans Docker, `COLLEGUE_HOME=/app/.collegue` est déjà le volume persistant.
- Le dossier par défaut d'avant (`collegue/prompts/templates/templates/`, `collegue/prompts/versions/versions.json`) n'est
  plus écrit ; `scripts/purge_prompt_duplicates.py` cible le nouvel emplacement. Un état existant à l'ancien emplacement
  n'est pas migré automatiquement (les graines YAML régénèrent les templates au démarrage).

## 4. Migrations embarquées

Le graphe existant `0001` … `0010_phase5_incidents` est conservé **à l'identique** (IDs, `down_revision`, contenu ; seul le
tri des imports a changé pour le lint) et déplacé de `alembic/` vers **`collegue/migrations/`**. Une base déjà migrée reste
compatible (mêmes révisions, mêmes noms de table `alembic_version`).

```
python -m collegue.migrations upgrade --url sqlite:////chemin/state.sqlite3   # applique jusqu'à head (idempotent)
python -m collegue.migrations upgrade --revision 0005                          # révision cible
python -m collegue.migrations current --url …                                  # révision courante
python -m collegue.migrations heads                                            # tête(s) du graphe embarqué
collegue-migrate upgrade                                                       # même CLI (entry point), URL = STATE_DATABASE_URL
```

Ordre de résolution de l'URL : `--url` explicite > `STATE_DATABASE_URL` > `settings.STATE_DATABASE_URL` > `alembic.ini`.
Codes de sortie : 0 succès, 1 échec de migration, 2 URL absente. Depuis un checkout, `alembic upgrade head` reste équivalent
(`alembic.ini` : `script_location = %(here)s/collegue/migrations`). API Python : `collegue.migrations.alembic_config(url)`,
`upgrade(url, revision)`, `current_revision(url)`, `head_revisions()`.

**Raccordement pour C (vague 2)** : A crée `alembic/versions/0011_budget_ledger.py`. À l'intégration, le déplacer
mécaniquement vers **`collegue/migrations/versions/0011_budget_ledger.py`** (`git mv`) ; aucun autre changement. Les tests de
A qui construisent `Config(...)` avec `script_location = REPO_ROOT / "alembic"` doivent pointer
`REPO_ROOT / "collegue" / "migrations"` (ou utiliser `collegue.migrations.alembic_config(url)`), comme dans
`tests/test_project_state.py`. Le test `test_migration_graph_is_linear_and_existing_ids_are_unchanged` accepte une tête
supplémentaire (`0011`) tant que le graphe reste linéaire.

## 5. CI et Docker

- `tests.yml` — noms de checks **inchangés** (`Ruff`, `Pytest (Python 3.11)`, `Pytest (Python 3.12)`, `Dependency audit`,
  `Docker build`). `Ruff` installe `locks/lint.txt` ; `Pytest` installe `locks/dev.txt` puis exécute
  `python scripts/locks.py check` ; `Dependency audit` installe `locks/audit.txt`, exécute
  `scripts/locks.py check --recompile`, `pip-audit --strict --desc`, puis `scripts/verify_wheel.py` (artifact `wheel-report`).
- `integration-nightly.yml` installe `locks/dev.txt` (installation verrouillée uniquement ; ni déclenché ni modifié
  fonctionnellement) et applique les migrations par `python -m collegue.migrations upgrade`.
- Le smoke Docker honnête de la vague 1 (`scripts/ci_docker_smoke.sh`) est inchangé.
- **PostgreSQL budgétaire (A/C)** : non câblé ici (chemins de tests de A inconnus). Raccordement prévu : un service
  `postgres:16` sur le job `Pytest (Python 3.12)` + une étape ciblée `pytest <chemins de A>` avec
  `STATE_DATABASE_URL=postgresql+psycopg2://…`, sans secret ni LLM. Le workflow nightly n'est pas utilisé pour cela.

## 6. Vérification du wheel dans un environnement vierge

```
python scripts/verify_wheel.py --python python3.11 --lock runtime --report wheel-report.json
python scripts/verify_wheel.py --python python3.12 --lock runtime
```

Le script : exporte les fichiers du dépôt (suivis et nouveaux non ignorés), construit le wheel, crée un venv neuf **sans pip**,
y installe `pip install --require-hashes --no-deps -r locks/runtime.txt` puis le wheel (non éditable), puis exécute, avec
`PYTHONPATH` nettoyé, `python -I` et un répertoire de travail vide hors checkout : chemin d'import effectif (site-packages du
venv, jamais le dépôt, checkout absent de `sys.path`) ; skills, templates, catégories, règles ; moteur de prompts ne
modifiant pas le paquet ; migrations jusqu'à `head` sur SQLite vide, seconde exécution idempotente, reprise d'une base
partielle (`0005` → `head`) ; démarrage du serveur en mémoire (`fastmcp.Client`), liste des outils et des 5 skills, avec
`socket.connect` interdit (aucun appel réseau, aucun LLM). Il imprime un rapport JSON (empreintes du wheel et de tous les
verrous, versions).

`tests/test_wheel_distribution.py` rejoue l'essentiel dans la suite (wheel réel installé avec `pip install --target`).

## 7. Limites

- Le smoke Docker et la construction des images (`collegue`, `sandbox`, `sandbox-openhands`) ne sont pas exécutés localement
  (disque) : la CI distante fait foi. Aucune installation OpenHands complète locale.
- Le wheel n'est pas reproductible à l'octet près (horodatages de l'archive) ; son contenu l'est.
- `--recompile` dépend de la disponibilité du réseau et de PyPI (une version retirée/yankée après la date de gel peut faire
  échouer la comparaison ; régénérer alors les verrous).
- Le Dockerfile `collegue` copie toujours les sources dans `/app` (`PYTHONPATH=/app`) au lieu d'installer le wheel : les
  ressources sont résolues par `importlib.resources` sur ce paquet, mais le chemin « wheel installé » de l'image n'est
  prouvé que par `verify_wheel.py` et `test_wheel_distribution.py`.
- `README.md`, `AGENTS.md`, `CLAUDE.md` (propriété de C) mentionnent encore `pip install -r requirements.txt` et
  `skills/` ; `requirements.txt` reste installable (inclusion du verrou haché).
