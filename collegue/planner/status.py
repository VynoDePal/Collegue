"""Statuts de projet partagés par le planificateur (P1/P5).

Source unique pour la transition du cycle de vie d'un projet planifié :
``planned`` (SPEC + tâches générés) → ``approved`` (validé par l'humain, P5).
P4 refuse d'écrire dans GitHub tant que le statut n'est pas ``approved``.
"""

PROJECT_STATUS_PLANNED = "planned"
PROJECT_STATUS_APPROVED = "approved"
