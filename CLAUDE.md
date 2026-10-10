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
- Vague 5 (autorisée, en cours ; W1 à W4 livrées ; **A34 `6ec6984` (X-Should-Retry : le client openai de LiteLLM ne réémet plus un refus établi ni un échec ambigu ; descend de A33 `6c3db18`, A32 `ab90dd5`, A31 `1ea690a`, A30 `e0f8b8b`, A29 `f467978` ; `executor.py` = blob `eddb1be9…` d'`ab90dd5`, la supervision `6e61b44` refusée par le manager n'y survit pas) et C59 (raccords SDK) et B27 `68d50f7` (descend de B24 `4953a43`/B26) sont INTÉGRÉS LOCALEMENT dans `codex/consolidation-w5-c`, NON publiés** (la PR #613, ouverte, tête `3690ac3` (publiée, non fusionnée) : run `38018264390` = Ruff, Dependency audit, Pytest 3.11 et Pytest 3.12 VERTS, Docker build ROUGE (preuve composée en vraie image 16/18 ; les 2 échecs de repli sont corrigés par A34, intégré localement et non encore publié) ; le premier run (`38012837656`, tête `7a111ee`) était rouge sur 3 checks et reste conservé) ; V2 APPLIQUÉE (C49, sur ordre) : commit `ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca`, arbre `6e6bea13c2d45cd848349b853aceec7fea5f7e85`, parent unique = graine `8e3691d8…`, branche `collegue-business/bootstrap-w5-v2` ; `main`/graine conservée ; ruleset 24793056 inchangé (confirmé) ; V1 (`10750c7b…`) conservée comme historique ; CODEOWNERS reste informatif. Le validateur du produit (`validate_bootstrap_manifest`) a accepté le vrai socle V2 en lecture seule (`evidence/w5-manager-broker/bootstrap-v2-product-readonly.json`). Variable distante `W5_BOOTSTRAP_MANIFEST_JSON` pas encore configurée ; environnement et secret Google encore absents ; aucune campagne. Zéro appel modèle, aucune campagne démarrée, aucun secret créé) : lire `docs/consolidation/w5-integration.md` (propriété des fichiers, contrats du courtier, fixture, CI, checklist d'intégration, décisions ouvertes). A = courtier budgétaire et relais, B = câblage métier réel R04/R05 et validation du socle de la fixture, C = workflows, Dockerfiles, `pyproject.toml`, `locks/`, scripts CI, préparation GitHub de la fixture. Toute publication distante (push, PR, fusion) exige un ordre distinct du manager ; ne jamais lancer ni dispatcher le workflow de campagne ni le nightly sans ordre exact ; ne jamais lire un fichier de clé ni changer un secret ; **aucune mutation GitHub de la fixture** (`scripts/w5_fixture_bootstrap.py apply|probe|cleanup`) sans le jeton d'ordre donné par le manager. Ne pas installer la fermeture du SDK OpenHands en local (la CI distante fait foi) ; scratch sous `/dev/shm/w5-<rôle>`, `TMPDIR` court.
- Écrire son rapport et ses preuves sous `~/.codex/collegue-consolidation/20260928/{reports,evidence}/`
  avec le préfixe `w<N>-<rôle>`, puis terminer la passe : le manager reprend la même session.
