# CLAUDE.md

Les règles de collaboration, la propriété des fichiers, les niveaux de tests et le protocole de
livraison sont dans [`AGENTS.md`](AGENTS.md). Les lire avant toute modification ; ne pas les recopier ici.
Plan opérationnel et checklist de revue : [`docs/consolidation/protocole.md`](docs/consolidation/protocole.md).

## Sessions de consolidation

Quand la session tourne dans un worktree `codex/consolidation-w<N>-{a,b,c}`, le rôle est donné par le
suffixe de la branche (`a`, `b` = implémenteurs ; `c` = intégrateur) et le brief de la vague. Hors de ces
worktrees, ces règles de rôle ne s'appliquent pas ; seules les « règles générales » d'`AGENTS.md` restent valables.

Spécificités Claude Code :

- Ne lancer ni sous-agent, ni autre session, ni modèle annexe ; garder le modèle courant.
- Utiliser le python du venv de son rôle (`~/.codex/collegue-consolidation/20260928/envs/<rôle>/bin/python`),
  `python -m pytest -p no:cacheprovider` depuis la racine du worktree, `python -m ruff --no-cache`.
- Git sur un workspace d'agent : uniquement via `collegue.executor.git_boundary` (`TrustedGit`, `HardenedGitRunner`,
  `trusted_base`) ; voir « Frontière Git et sources de confiance » dans `AGENTS.md`. Lancer les journaux de preuve sans
  pipe qui masque le code de retour (rediriger vers un fichier puis lire `$?`).
- Commits : en français, terminés par la ligne d'attribution demandée par la session. A et B committent en
  local ; seul C pousse et ouvre une PR (sur consigne du manager), publie et observe checks et revues, **rapporte**
  résultats et SHA, puis ne fusionne que sur l'instruction finale de Codex pour cette tête exacte.
- Revues distantes : tout finding pertinent est transmis au manager **avant** fusion pour arbitrage ; ne jamais le
  reporter seul à la vague suivante ni le noyer dans les limites de la PR. Une revue absente ou un check manquant
  n'est pas un succès : le dire. Voir « Livraison et merge » dans `AGENTS.md`.
- Vague 2 : lire `docs/consolidation/w2-integration.md` (propriété des fichiers, raccordements, checklist). Caches et
  artefacts sous `/tmp/<rôle>-…`, jamais dans `~/.cache`.
- Vague 3 : lire `docs/consolidation/w3-integration.md` (propriété des fichiers, contrat de preuve, checklist), puis
  `w3-quality.md` et `w3-merge.md` (documents de lot). Raccords de C : `tests/w3_remote_bridge.py` (Git distant réel derrière les
  vrais clients GitHub), `tests/w3_publication.py`, `tests/test_w3_integration_{build,improve}.py` ; jamais de `verify_fn`,
  `proof_loader` ni preuve injectés dans ces tests. `--basetemp` court (socket Unix). Ne pas présenter la comparaison de base
  d'un `merge_pr` comme atomique (l'API REST n'a pas d'`expected_base_sha`).
- Vague 4 (livrée : PR #612, `main` = `869ed3c` ; **validation réelle incomplète** — la campagne finale a été refusée au préflight `P06` avant toute émission, 0 appel modèle ; son lancement unique est consommé) : `docs/consolidation/w4-integration.md` reste la référence historique (répartition, preuves, checklist, livraison en deux étapes). Un préflight bloqué avant émission est une *validation incomplète*, pas une validation réelle.
- Vague 5 (**livrée** : PR #613 `734c7f5`, PR #614 `b88e7bf`, PR #615 `ed54868`, **PR #616 `main` = `15382894ed06653516ed0f5b1d13a947d88d1dea`** ; **qualification réelle INCOMPLÈTE**) : lire `docs/consolidation/w5-post500.md` (état et règles), `w5-google-campaign-plan.md` (lot diagnostic Google, historique du plan de campagne), `w5-json-pair-ci.md` (workflow et contrat de la paire JSON), puis `w5-integration.md` (historique de préparation et d'intégration), `w5-product-preflight.md` (préflight dédié sans modèle). Traces conservées, **trois campagnes closes qui ne se relancent jamais** : `w5-gemma-20261010-dcf645da` (arrêtée au préflight P07, sans émission) ; `w5-gemma-20261012-9e7c4d76` (run `38230467960`, UNE génération du 31B → HTTP 500, usage `unknown`) ; **`w5-gemma-20261012-538fff52` (run `38273892997`) : texte du 31B OK (86 jetons réglés), JSON du 31B → HTTP 500 / RPC `INTERNAL`, 270 jetons conservés `unknown`, scope bloqué, aucune suite ni retry ni repli** ; secret/environnement/variable supprimés après chaque run. **« 0 usage réglé » ne veut pas dire « zéro émission ».** Le diagnostic hors ligne du canari JSON est **terminé** (aucun défaut local démontré, cause du 500 indéterminée, le MIME n'était pas isolé). **Autorisé en principe, NON lancé** : UNE paire ciblée (31B, même prompt JSON sans puis avec `responseMimeType`, deux générations au plus, 2 USD / 250 000 tokens / 900 s, identifiant neuf) : lot A `collegue/broker/json_pair.py` + `scripts/w5_json_pair.py`, lot B tests adverses, C `w5-json-pair.yml` + `w5-json-pair-ci.md` — intégrée LOCALEMENT, non publiée (A `e598f3bef03453771964df15d2491eca5381347b`, B `5407208a9fb578b30f347c33b8c98dc03380f187`, workflow C `24af0072ad8fc33a6e0b6e0366ab061f5e3ebf33` ; correctif de reprise d'un journal fermé (finding P2 de la revue de la PR #617) : A46 `01319977f37291d0f046a16e1e8bc3927109cf15`, B41 `b408fd4792df7dadb64255444f4357a58c9dedeb`) ; C n'intègre A/B qu'avec les SHA acceptés communiqués par le manager ; elle ne qualifie pas le parcours métier. Toute publication distante (push, PR, fusion) exige un ordre distinct ; jamais de dispatch ni de rerun de `consolidation-e2e.yml`, de `w5-product-preflight.yml`, de `w5-json-pair.yml` ou du nightly sans ordre exact ; **aucun appel modèle, `countTokens` compris**, ni lecture d'un fichier de clé, ni secret/environnement/variable, hors lancement ordonné ; **aucune mutation GitHub de la fixture** (`scripts/w5_fixture_bootstrap.py apply|probe|cleanup`, revendication comprise) sans le jeton d'ordre. Ne pas installer la fermeture du SDK OpenHands en local (la CI distante fait foi) ; racine du disque très réduite : scratch, caches, homes et logs sous `/tmp/<rôle>/…` ou `/dev/shm`, `TMPDIR` ancêtre de `--basetemp`, `COLLEGUE_HOME` neuf par exécution, aucun `pip install` ; suite complète avec **un seul** `-q` (le dépôt en met déjà un) et un JUnit ; respecter tout refus de permission, sans le contourner.
- Écrire son rapport et ses preuves sous `~/.codex/collegue-consolidation/20260928/{reports,evidence}/`
  avec le préfixe `w<N>-<rôle>`, puis terminer la passe : le manager reprend la même session.
