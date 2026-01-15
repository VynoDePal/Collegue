"""
Git Resources - Ressources pour l'intégration Git
"""
import json
from fastmcp import Context

def register(app, app_state):
    """Enregistre les ressources Git dans l'application FastMCP."""
    
    git_manager = app_state.get("git_manager")
    if not git_manager:
        return

    @app.resource("git://status")
    async def get_git_status() -> str:
        """Retourne le statut actuel du dépôt Git."""
        status = git_manager.get_status()
        return json.dumps(status, indent=2)

    @app.resource("git://diff")
    async def get_git_diff() -> str:
        """Retourne le diff des modifications non stagées."""
        return git_manager.get_diff()

    @app.resource("git://log")
    async def get_git_log() -> str:
        """Retourne les 10 derniers commits."""
        logs = git_manager.get_log(10)
        return json.dumps(logs, indent=2)

    # Note: Pour une vraie proactivité, on pourrait imaginer un background task 
    # qui surveille le repo et appelle ctx.notify() ou équivalent si FastMCP le permettait directement
    # sur une resource. Avec FastMCP actuel, les subscriptions sont gérées par le client.
    # Si le client s'abonne à git://status, il s'attend à recevoir des notifications.
    # Nous pourrions ajouter un endpoint pour forcer le refresh ou utiliser un watcher.
