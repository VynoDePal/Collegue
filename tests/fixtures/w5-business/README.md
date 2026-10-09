# Données de scénario W5 (campagne métier)

`docs/runbook-ops.md` (support de R04) et `docs/deploiement.md` (support de R05) sont les seuls documents d'exemple du socle de la
fixture : ils contiennent des identifiants FACTICES (exemples de la documentation AWS) que le scan de secrets du moteur compte.
Aucune implémentation métier : les trois tâches BUILD créent tout le reste, y compris `docs/export_header.md`.

`producer-workflow.reference.yml` est une COPIE DE RÉFÉRENCE du workflow de confiance préparé par C (plan du socle, étape
`inspect`) : elle sert de donnée aux tests de validation du producteur de check (`check_producer`). Elle n'est jamais utilisée
comme preuve : le socle réel est comparé octet pour octet par l'API (hash du manifeste), pas à cette copie.
