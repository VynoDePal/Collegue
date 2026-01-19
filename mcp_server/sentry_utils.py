import os
import requests
from typing import Any, Dict

def sentry_monitor(event_data: Dict[str, Any]):
    """
    Envoie des métriques ou des événements à l'API Sentry.
    L'ID du projet doit être numérique pour éviter l'erreur 400.
    """
    sentry_token = os.getenv("SENTRY_AUTH_TOKEN")
    organization_slug = os.getenv("SENTRY_ORG_SLUG")
    # SENTRY_PROJECT_ID doit être l'ID numérique (ex: 450123...)
    project_id = os.getenv("SENTRY_PROJECT_ID")

    if not all([sentry_token, organization_slug, project_id]):
        return

    # Validation que project_id est bien composé de chiffres
    if not str(project_id).isdigit():
        print(f"Erreur: SENTRY_PROJECT_ID doit être numérique, reçu: {project_id}")
        return

    url = f"https://sentry.io/api/0/projects/{organization_slug}/{project_id}/events/"
    
    headers = {
        "Authorization": f"Bearer {sentry_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, json=event_data, headers=headers)
        if response.status_code != 200:
            print(f"Erreur API Sentry {response.status_code}: {response.text}")
    except Exception as e:
        print(f"Erreur lors de l'appel Sentry: {e}")