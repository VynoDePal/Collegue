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
- Vague 5 (**livrée** : PR #613 `main` = `734c7f5`, puis PR #614 `main` = `b88e7bf` ; **qualification réelle INCOMPLÈTE**) : lire `docs/consolidation/w5-post500.md` (état courant, règles, plan de vérification), puis `w5-integration.md` (historique de préparation et d'intégration, propriété des fichiers, contrats du courtier, fixture, CI) et `w5-product-preflight.md` (préflight produit distant sans modèle). La première campagne (`w5-gemma-20261010-dcf645da`) s'est arrêtée au préflight P07 sans émission ; la seconde (`w5-gemma-20261012-9e7c4d76`, run `38230467960`) a émis UNE génération du 31B qui a reçu un HTTP 500 (usage `unknown`, scope bloqué, aucun retry ni repli, cause indéterminée) : **« 0 usage réglé » ne veut pas dire « zéro émission »**, cette campagne est close et ne se relance jamais, son secret/environnement/variable sont supprimés. A = courtier budgétaire et relais (A35/A36 : diagnostic hors ligne du 500, en cours), B = câblage métier R04/R05 et compte rendu (B28 : corrections en cours), C = workflows, Dockerfiles, `pyproject.toml`, `locks/`, scripts CI, préparation GitHub de la fixture, documents généraux ; **C n'intègre A/B qu'avec les SHA acceptés communiqués par le manager**. Toute publication distante (push, PR, fusion) exige un ordre distinct du manager ; jamais de dispatch ni de rerun de `consolidation-e2e.yml`, de `w5-product-preflight.yml` ou du nightly sans ordre exact ; **aucun appel modèle, `countTokens` compris**, hors campagne ordonnée ; ne jamais lire un fichier de clé ni changer un secret ; **aucune mutation GitHub de la fixture** (`scripts/w5_fixture_bootstrap.py apply|probe|cleanup`) sans le jeton d'ordre. Ne pas installer la fermeture du SDK OpenHands en local (la CI distante fait foi) ; racine du disque très réduite : scratch, caches, homes et logs sous `/tmp/<rôle>/…` ou `/dev/shm`, `TMPDIR` ancêtre de `--basetemp`, `COLLEGUE_HOME` neuf par exécution, aucun `pip install`.
- Écrire son rapport et ses preuves sous `~/.codex/collegue-consolidation/20260928/{reports,evidence}/`
  avec le préfixe `w<N>-<rôle>`, puis terminer la passe : le manager reprend la même session.
