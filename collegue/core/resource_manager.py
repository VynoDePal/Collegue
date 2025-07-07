"""
Resource Manager - Gestionnaire centralisé des ressources pour Collègue MCP
"""
from typing import Dict, Any, Optional, List
import logging

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ResourceManager:
    """
    Gestionnaire centralisé des ressources pour Collègue MCP.
    
    Cette classe permet d'enregistrer et d'accéder aux différentes ressources
    (références de langages, documentation de frameworks, etc.) de manière unifiée.
    """
    
    def __init__(self):
        """Initialise le gestionnaire de ressources."""
        self._resources = {}
        logger.info("ResourceManager initialisé")
    
    def register_resource(self, resource_id: str, resource: Dict[str, Any]) -> None:
        """
        Enregistre une ressource dans le gestionnaire.
        
        Args:
            resource_id: Identifiant unique de la ressource
            resource: Dictionnaire contenant les données et fonctions de la ressource
        """
        if resource_id in self._resources:
            logger.warning(f"La ressource '{resource_id}' est remplacée")
        
        self._resources[resource_id] = resource
        logger.info(f"Ressource '{resource_id}' enregistrée avec succès")
    
    def get_resource(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """
        Récupère une ressource par son identifiant.
        
        Args:
            resource_id: Identifiant de la ressource à récupérer
            
        Returns:
            La ressource demandée ou None si elle n'existe pas
        """
        if resource_id not in self._resources:
            logger.warning(f"Ressource '{resource_id}' non trouvée")
            return None
        
        return self._resources[resource_id]
    
    def list_resources(self) -> List[str]:
        """
        Liste toutes les ressources disponibles.
        
        Returns:
            Liste des identifiants de ressources enregistrées
        """
        return list(self._resources.keys())
    
    def get_resource_info(self) -> Dict[str, Dict[str, str]]:
        """
        Récupère les informations de base sur toutes les ressources.
        
        Returns:
            Dictionnaire avec les identifiants de ressources et leurs descriptions
        """
        return {
            resource_id: {
                "description": resource.get("description", "Pas de description disponible")
            }
            for resource_id, resource in self._resources.items()
        }
    
    def call_resource_method(self, resource_id: str, method_name: str, *args, **kwargs) -> Any:
        """
        Appelle une méthode d'une ressource spécifique.
        
        Args:
            resource_id: Identifiant de la ressource
            method_name: Nom de la méthode à appeler
            *args, **kwargs: Arguments à passer à la méthode
            
        Returns:
            Le résultat de l'appel de méthode ou None en cas d'erreur
        """
        resource = self.get_resource(resource_id)
        if not resource:
            return None
        
        if method_name not in resource:
            logger.warning(f"Méthode '{method_name}' non trouvée dans la ressource '{resource_id}'")
            return None
        
        try:
            method = resource[method_name]
            if callable(method):
                return method(*args, **kwargs)
            else:
                return method
        except Exception as e:
            logger.error(f"Erreur lors de l'appel de '{resource_id}.{method_name}': {str(e)}")
            return None
