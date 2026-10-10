# Déploiement

L'export lit le fichier `docs/export_header.md`. Pour publier le service, exporter les identifiants :

    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
    AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

Puis lancer `alembic upgrade head` avant le premier démarrage.
