# Données de scénario W5 (campagne métier)

`docs/runbook-ops.md` (support de R04) et `docs/deploiement.md` (support de R05) sont les seuls documents d'exemple du socle de la
fixture : ils contiennent des identifiants FACTICES (exemples de la documentation AWS) que le scan de secrets du moteur compte.
Aucune implémentation métier : les trois tâches BUILD créent tout le reste, y compris `docs/export_header.md`.

`fixture-tests.reference.yml`, `CODEOWNERS.reference`, `approved-lock.reference.lock` et `requirements.reference.txt` sont des COPIES DE
RÉFÉRENCE des contrôles du socle préparés par C (plan du socle `w5-c-fixture-plan`, étape `inspect`, C45) : workflow déclenché par
`pull_request` (jamais `pull_request_target`, qui s'exécute depuis la branche par défaut — la graine, sans workflow), propriétaire
des chemins protégés `.github/` et `ci/`, verrou haché de la pile approuvée et `requirements.txt` aligné sur ce verrou. Elles servent
de données aux tests de validation du socle (`check_workflow`) et ne sont jamais une preuve : le socle réel est comparé octet pour
octet par l'API (hachages du manifeste), pas à ces copies.

`manifest.v2.json` est le manifeste du socle V2 fourni par C (plan `w5-c-fixture-plan-v2`, commit `ad56c0fa…`, branche
`collegue-business/bootstrap-w5-v2`, `ruleset_id` = 24793056 : le ruleset de la V1, adopté tel quel). La V1 (`10750c7b…`) reste intacte côté
distant mais n'est plus la référence. Les copies ci-dessus ont exactement les sha256 qu'il approuve ; `validate_bootstrap_manifest` l'accepte
contre un monde cohérent construit depuis elles. Le workflow et le CODEOWNERS V2 corrigent les commentaires de la V1 : CODEOWNERS n'est
qu'un SIGNAL de revue (C47 : fusion à 0 approbation), la protection est la garde de publication et de fusion du produit.
