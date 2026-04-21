# Stress tests — Outils à intégration API externe

Cette note documente l'extension de la suite `tests/stress/` aux 4 outils à intégration API externe (`github_ops`, `sentry_monitor`, `postgres_db`, `kubernetes_ops`). Issue d'origine : [#206](https://github.com/VynoDePal/Collegue/issues/206).

## Pourquoi ces outils étaient exclus

Le harness stress (`tests/stress/runner.py`) est **black-box HTTP** : il envoie des requêtes JSON-RPC au serveur MCP qui tourne en Docker et mesure latence, codes d'erreur, état du container. Les 4 outils couverts ici parlent à des APIs externes (GitHub, Sentry, Postgres, Kubernetes) qui nécessitent des credentials. Sans adaptation, on ne pouvait pas les tester sans comptes réels.

## Deux modes

### Mode A — sans credentials (par défaut, celui validé par la CI locale)

C'est le mode cible de [#206](https://github.com/VynoDePal/Collegue/issues/206). On lance le serveur **sans aucun token API** (`GITHUB_TOKEN`, `SENTRY_AUTH_TOKEN`, `POSTGRES_URL`, `KUBECONFIG` tous absents du `.env`). Les payloads stress tombent alors dans l'un de ces cas :

| Cas | Attendu |
|---|---|
| Validation Pydantic (champ manquant, type mauvais) | `VALID-OK` |
| Garde sécurité (SELECT-only, whitelist commandes, path traversal) | `VALID-OK` |
| Requête plausible mais API injoignable (pas de token) | `TOOL-ERR` |
| Crash serveur | `CRASH-500` ❌ |

**Critère d'acceptation** : **0 CRASH-500, 0 HANG, 0 OOM-KILL**. Les `TOOL-ERR` et `VALID-OK` sont acceptables — ils démontrent que les gardes et la validation font leur travail, et que le tool dégrade proprement quand la configuration externe manque.

### Mode B — avec credentials sandbox (optionnel, manuel)

Pour vérifier les vrais codes de retour de chaque API, monter un environnement sandbox :

| Outil | Setup sandbox |
|---|---|
| `github_ops` | Créer un token personnel GitHub avec scope `public_repo` uniquement, pointé sur un repo de test. Exporter `GITHUB_TOKEN`. |
| `sentry_monitor` | Compte Sentry Developer Edition ou organisation jetable. Exporter `SENTRY_AUTH_TOKEN` + `SENTRY_ORG`. |
| `postgres_db` | Lancer un Postgres local en Docker : `docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16`. Exporter `POSTGRES_URL=postgresql://postgres:x@localhost:5432/postgres`. |
| `kubernetes_ops` | Cluster éphémère via `kind create cluster --name stress` ou `k3d cluster create stress`. Monter le kubeconfig dans le container (voir README §Docker). |

Ce mode n'est **pas** exigé par #206. Les payloads sont pensés pour être non-destructifs (SELECT only, list-only), mais restez prudent avec un token qui porte des scopes d'écriture.

## Structure des payloads

4 fichiers dans [tests/stress/payloads/](../tests/stress/payloads/), même contrat que les 9 fichiers existants :

```python
TOOL_NAME = "github_ops"   # nom exact de l'outil enregistré par MCP
PAYLOADS = [
    {"description": "...", "arguments": {...}},
    ...
]
```

Chaque fichier couvre 4 catégories :

1. **Validation** — champs manquants, types invalides, commandes hors whitelist
2. **Adversariat** — injection SQL/shell/path, null bytes, unicode RTL, whitespace tricks
3. **Boundaries** — chaînes extrêmement longues, labels K8s de 1000 entrées, SELECT sur 5000 colonnes
4. **Plausible sans credentials** — requêtes bien formées qui doivent retourner `TOOL-ERR` proprement

## Exécution

Prérequis : la stack Docker doit tourner (`docker compose up -d`). Le serveur MCP doit être accessible sur `http://localhost:8088/mcp/` (c'est l'URL par défaut attendue par le harness, vérifier `runner.py`).

```bash
# Run complet (9 outils métier + 4 outils API externes)
python3 tests/stress/run_all.py --out tests/stress/reports/api-tools

# Par outil (itérer rapidement pendant qu'on ajoute des payloads)
python3 tests/stress/run_all.py --only-tools github_ops,sentry_monitor
```

Les rapports atterrissent dans `tests/stress/reports/api-tools/` :
- `cases/*.json` — un fichier par payload avec requête, réponse, latence, catégorie
- `stress_summary.md` — breakdown global + per-tool

## Interpréter le résultat

Lire directement `stress_summary.md`. Pour chaque outil, la table doit afficher :

| Catégorie | Attendu ? |
|---|---|
| `OK` / `VALID-OK` | ✅ Le serveur a rejeté proprement au niveau validation |
| `TOOL-ERR` | ✅ En mode A, attendu pour les payloads "plausibles sans token" |
| `CRASH-500` | ❌ Bug serveur — investiguer `cases/<tool>-NN.json` |
| `HANG` | ❌ Timeout — investiguer (boucle infinie, deadlock, ou timeout harness trop court ?) |
| `OOM-KILL` | ❌ Le container a été tué — investiguer les payloads "boundary" géants |
| `INJECTION` | ❌ L'outil a laissé passer un secret ou a obéi à une instruction injectée |

## Ajouter de nouveaux payloads

1. Identifier la catégorie (validation / adversariat / boundary / plausible)
2. Ajouter un dict dans `PAYLOADS` avec description courte et `arguments` minimal
3. Relancer uniquement l'outil concerné : `python3 tests/stress/run_all.py --only-tools <tool>`
4. Vérifier que le nouveau cas ne passe pas dans CRASH-500 / HANG / OOM-KILL

Si un payload expose un vrai bug, **ne pas le retirer** — c'est un test de non-régression. Fixer le bug côté outil, relancer, vérifier que le cas est maintenant en `VALID-OK` ou `TOOL-ERR`.
