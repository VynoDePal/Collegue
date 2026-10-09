# Runbook d'exploitation

Procédure d'exploitation du service d'audits (document d'EXEMPLE du socle de la fixture W5).

## Accès à l'archivage des PDF

Pour exporter les rapports vers le stockage d'archivage, configurer les identifiants ci-dessous
(valeurs factices publiées dans la documentation d'AWS, sans aucun accès réel) :

    AWS_ACCESS_KEY_ID=AKIAI44QH8DHBEXAMPLE
    AWS_SECRET_ACCESS_KEY=je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY

## Sauvegarde

Sauvegarder le fichier SQLite chaque nuit puis vérifier que `alembic current` répond `0001`.
