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
  local ; seul C pousse, ouvre une PR et fusionne, après consigne du manager et acceptation sur le SHA exact.
- Écrire son rapport et ses preuves sous `~/.codex/collegue-consolidation/20260928/{reports,evidence}/`
  avec le préfixe `w<N>-<rôle>`, puis terminer la passe : le manager reprend la même session.
