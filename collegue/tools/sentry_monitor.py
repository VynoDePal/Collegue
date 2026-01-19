import requests
from typing import Any, Dict
# ... autres imports existants ...

# Note: Ce code est une reconstruction ciblée basée sur la stacktrace fournie

class SentryMonitor:
    # ... autres méthodes ...

    def _api_get(self, endpoint: str, params: Dict[str, Any] = None):
        # Logique simplifiée pour l'exemple
        url = f"https://sentry.io/api/0/{endpoint}"
        headers = {"Authorization": "Bearer YOUR_TOKEN"}
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            if response.status_code == 400:
                raise ToolExecutionError(f"Erreur API Sentry 400: {response.text}")
            raise ToolExecutionError(f"Erreur Sentry {response.status_code}")
        return response.json()

    def _get_project_stats(self, organization_slug: str, project_slug: str):
        """
        Récupère les statistiques d'un projet Sentry.
        Correction : S'assure d'utiliser l'ID numérique du projet si nécessaire.
        """
        # 1. Récupérer les détails du projet pour obtenir l'ID numérique
        project_details = self._api_get(f"projects/{organization_slug}/{project_slug}/")
        project_id = project_details.get("id")

        if not str(project_id).isdigit():
            # Si l'ID n'est pas numérique, on tente de continuer mais l'API risque d'échouer
            pass

        # 2. Appel des stats avec l'ID numérique (attendu par Sentry pour le paramètre 'project')
        try:
            # L'erreur 400 provenait de l'utilisation du slug au lieu de l'ID numérique ici
            stats = self._api_get(
                f"organizations/{organization_slug}/stats/",
                params={
                    "project": project_id, 
                    "query": "is:unresolved", 
                    "limit": 1
                }
            )
            return stats
        except Exception as e:
            raise ToolExecutionError(f"Erreur lors de la récupération des stats: {e}")

    def _execute_core_logic(self, **kwargs):
        # ... logique existante appelant _get_project_stats ...
        return self._get_project_stats(kwargs.get('org'), kwargs.get('project'))