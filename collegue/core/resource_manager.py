"""
Resource Manager - Gestionnaire centralisé des ressources pour Collègue MCP
"""
from typing import Dict, Any, Optional, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ResourceManager:
    
    def __init__(self):
        """Initialise le gestionnaire de ressources."""
        self._resources = {}
        logger.info("ResourceManager initialisé")
    
    def register_resource(self, resource_id: str, resource: Dict[str, Any]) -> None:

        if resource_id in self._resources:
            logger.warning(f"La ressource '{resource_id}' est remplacée")
        
        self._resources[resource_id] = resource
        logger.info(f"Ressource '{resource_id}' enregistrée avec succès")
    
    def get_resource(self, resource_id: str) -> Optional[Dict[str, Any]]:

        if resource_id not in self._resources:
            logger.warning(f"Ressource '{resource_id}' non trouvée")
            return None
        
        return self._resources[resource_id]
    
    def list_resources(self) -> List[str]:
        return list(self._resources.keys())
    
    def get_resource_info(self) -> Dict[str, Dict[str, str]]:
        return {
            resource_id: {
                "description": resource.get("description", "Pas de description disponible")
            }
            for resource_id, resource in self._resources.items()
        }
    
    def call_resource_method(self, resource_id: str, method_name: str, *args, **kwargs) -> Any:
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
