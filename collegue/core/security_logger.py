"""
Security Logger - Journalisation des événements de sécurité

Ce module fournit des fonctions de logging dédiées aux événements de sécurité
pour faciliter la détection et l'investigation d'incidents.
"""
import logging
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any


class SecurityLogger:
    """
    Logger dédié aux événements de sécurité.
    
    Utilise un logger Python séparé pour permettre une configuration
    distincte (niveau, format, destination) des logs applicatifs.
    """
    
    def __init__(self, name: str = "security"):
        self.logger = logging.getLogger(name)
        # S'assurer qu'au moins un handler existe
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            self.logger.addHandler(handler)
    
    def _log_event(self, level: int, event_type: str, details: Dict[str, Any]) -> None:
        """Log un événement de sécurité structuré."""
        log_entry = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details
        }
        self.logger.log(level, json.dumps(log_entry))
    
    def log_auth_failure(
        self,
        reason: str,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        username: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log une tentative d'authentification échouée.
        
        Args:
            reason: Raison de l'échec (ex: 'invalid_token', 'expired_token')
            client_ip: Adresse IP du client
            user_agent: User-Agent du client
            username: Nom d'utilisateur si disponible
            extra: Informations supplémentaires
        """
        details = {
            "reason": reason,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "username": username
        }
        if extra:
            details.update(extra)
        self._log_event(logging.WARNING, "AUTH_FAILURE", details)
    
    def log_auth_success(
        self,
        user_id: str,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        auth_method: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log une authentification réussie.
        
        Args:
            user_id: Identifiant de l'utilisateur
            client_ip: Adresse IP du client
            user_agent: User-Agent du client
            auth_method: Méthode d'authentification (ex: 'jwt', 'oauth')
            extra: Informations supplémentaires
        """
        details = {
            "user_id": user_id,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "auth_method": auth_method
        }
        if extra:
            details.update(extra)
        self._log_event(logging.INFO, "AUTH_SUCCESS", details)
    
    def log_data_access(
        self,
        user_id: str,
        resource: str,
        action: str,
        resource_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log un accès à des données sensibles.
        
        Args:
            user_id: Identifiant de l'utilisateur
            resource: Type de ressource (ex: 'github_repo', 'sentry_project')
            action: Action effectuée (ex: 'read', 'list', 'delete')
            resource_id: Identifiant de la ressource
            client_ip: Adresse IP du client
            extra: Informations supplémentaires
        """
        details = {
            "user_id": user_id,
            "resource": resource,
            "action": action,
            "resource_id": resource_id,
            "client_ip": client_ip
        }
        if extra:
            details.update(extra)
        self._log_event(logging.INFO, "DATA_ACCESS", details)
    
    def log_config_change(
        self,
        user_id: str,
        setting: str,
        old_value: Any,
        new_value: Any,
        client_ip: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log une modification de configuration.
        
        Args:
            user_id: Identifiant de l'utilisateur ayant fait le changement
            setting: Nom du paramètre modifié
            old_value: Valeur avant modification
            new_value: Nouvelle valeur
            client_ip: Adresse IP du client
            extra: Informations supplémentaires
        """
        details = {
            "user_id": user_id,
            "setting": setting,
            "old_value": str(old_value) if old_value is not None else None,
            "new_value": str(new_value) if new_value is not None else None,
            "client_ip": client_ip
        }
        if extra:
            details.update(extra)
        self._log_event(logging.WARNING, "CONFIG_CHANGE", details)
    
    def log_suspicious_activity(
        self,
        activity_type: str,
        description: str,
        client_ip: Optional[str] = None,
        user_id: Optional[str] = None,
        severity: str = "warning",
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log une activité suspecte détectée.
        
        Args:
            activity_type: Type d'activité (ex: 'rate_limit_exceeded', 'path_traversal_attempt')
            description: Description de l'activité
            client_ip: Adresse IP du client
            user_id: Identifiant de l'utilisateur si disponible
            severity: Niveau de sévérité ('warning', 'error', 'critical')
            extra: Informations supplémentaires
        """
        level_map = {
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL
        }
        level = level_map.get(severity, logging.WARNING)
        
        details = {
            "activity_type": activity_type,
            "description": description,
            "client_ip": client_ip,
            "user_id": user_id
        }
        if extra:
            details.update(extra)
        self._log_event(level, "SUSPICIOUS_ACTIVITY", details)


# Instance globale pour utilisation simplifiée
security_logger = SecurityLogger()
