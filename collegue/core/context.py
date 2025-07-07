"""
Context Manager - Gestion du contexte entre les requêtes
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional, Union

class ContextManager:
    """
    Gère le contexte entre les requêtes pour maintenir l'état de la conversation
    et les informations sur le code en cours d'analyse.
    """
    
    def __init__(self, storage_dir: str = None):
        self.contexts = {}  # Stockage des contextes par session
        self.storage_dir = storage_dir
        
        # Créer le répertoire de stockage s'il n'existe pas
        if self.storage_dir and not os.path.exists(self.storage_dir):
            os.makedirs(self.storage_dir)
    
    def create_context(self, session_id: str, metadata: Dict[str, Any] = None) -> Union[Dict[str, Any], bool]:
        """
        Crée un nouveau contexte pour une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            metadata (Dict[str, Any], optional): Métadonnées supplémentaires pour la session
            
        Returns:
            dict or bool: Le contexte nouvellement créé ou False si le contexte existe déjà
        """
        # Vérifier si le contexte existe déjà
        if session_id in self.contexts:
            return False
            
        context = {
            "session_id": session_id,
            "code_history": [],
            "conversation_history": [],
            "execution_history": [],  # Ajout de l'historique d'exécution
            "current_file": None,
            "project_structure": None,
            "language_context": None,
            "open_files": [],
            "dependencies": {},
            "metadata": metadata or {},
            "created_at": self._get_timestamp(),
            "updated_at": self._get_timestamp()
        }
        self.contexts[session_id] = context
        
        # Persister le contexte si un répertoire de stockage est défini
        if self.storage_dir:
            self._persist_context(session_id)
            
        return context
    
    def get_context(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Récupère le contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            
        Returns:
            dict: Le contexte de la session ou None si non trouvé
        """
        # Essayer de charger le contexte depuis le stockage si non présent en mémoire
        if session_id not in self.contexts and self.storage_dir:
            self._load_context(session_id)
            
        return self.contexts.get(session_id)
    
    def update_context(self, session_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Met à jour le contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            updates (dict): Les mises à jour à appliquer au contexte
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
            
        for key, value in updates.items():
            if key == "code_history":
                # Ajouter à l'historique du code
                context["code_history"].append(value)
                # Limiter la taille de l'historique
                if len(context["code_history"]) > 20:  
                    context["code_history"] = context["code_history"][-20:]
            elif key == "conversation_history":
                # Ajouter à l'historique de la conversation
                context["conversation_history"].append(value)
                # Limiter la taille de l'historique
                if len(context["conversation_history"]) > 30:  
                    context["conversation_history"] = context["conversation_history"][-30:]
            elif key == "metadata" and isinstance(value, dict):
                # Fusionner les métadonnées au lieu de remplacer
                context["metadata"].update(value)
            else:
                # Mettre à jour les autres clés normalement
                context[key] = value
        
        # Mettre à jour le timestamp
        context["updated_at"] = self._get_timestamp()
        
        # Persister le contexte mis à jour
        if self.storage_dir:
            self._persist_context(session_id)
                
        return context
    
    def add_code_to_context(self, session_id: str, code: str, language: str = None, 
                           file_path: str = None, code_type: str = None) -> Optional[Dict[str, Any]]:
        """
        Ajoute du code au contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            code (str): Le code à ajouter
            language (str, optional): Le langage du code
            file_path (str, optional): Le chemin du fichier contenant le code
            code_type (str, optional): Type de code (snippet, function, class, etc.)
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
            
        code_entry = {
            "code": code,
            "language": language,
            "file_path": file_path,
            "code_type": code_type,
            "timestamp": self._get_timestamp()
        }
        
        # Si le fichier est spécifié, l'ajouter aux fichiers ouverts s'il n'y est pas déjà
        if file_path and file_path not in [f["path"] for f in context["open_files"]]:
            self.add_file_to_context(session_id, file_path, language)
        
        return self.update_context(session_id, {"code_history": code_entry})
    
    def add_message_to_context(self, session_id: str, role: str, content: str, 
                              metadata: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """
        Ajoute un message à l'historique de conversation d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            role (str): Le rôle de l'émetteur du message (user, assistant)
            content (str): Le contenu du message
            metadata (Dict[str, Any], optional): Métadonnées supplémentaires pour le message
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
            
        message = {
            "role": role,
            "content": content,
            "timestamp": self._get_timestamp(),
            "metadata": metadata or {}
        }
        
        return self.update_context(session_id, {"conversation_history": message})
    
    def add_file_to_context(self, session_id: str, file_path: str, language: str = None, 
                           content: str = None, is_open: bool = True) -> Optional[Dict[str, Any]]:
        """
        Ajoute un fichier au contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            file_path (str): Le chemin du fichier
            language (str, optional): Le langage du fichier
            content (str, optional): Le contenu du fichier
            is_open (bool): Indique si le fichier est actuellement ouvert
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
        
        # Vérifier si le fichier est déjà dans le contexte
        file_exists = False
        for i, file in enumerate(context["open_files"]):
            if file["path"] == file_path:
                # Mettre à jour le fichier existant
                context["open_files"][i].update({
                    "language": language or file["language"],
                    "is_open": is_open,
                    "last_accessed": self._get_timestamp()
                })
                if content is not None:
                    context["open_files"][i]["content"] = content
                file_exists = True
                break
        
        if not file_exists:
            # Ajouter le nouveau fichier
            file_info = {
                "path": file_path,
                "language": language,
                "is_open": is_open,
                "content": content,
                "first_opened": self._get_timestamp(),
                "last_accessed": self._get_timestamp()
            }
            context["open_files"].append(file_info)
        
        # Définir le fichier courant si c'est le premier fichier ou s'il est ouvert
        if is_open or context["current_file"] is None:
            context["current_file"] = file_path
        
        # Persister le contexte mis à jour
        if self.storage_dir:
            self._persist_context(session_id)
        
        return context
    
    def set_project_structure(self, session_id: str, structure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Définit la structure du projet dans le contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            structure (Dict[str, Any]): La structure du projet
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        return self.update_context(session_id, {"project_structure": structure})
    
    def add_dependency_to_context(self, session_id: str, name: str, version: str = None, 
                                 type: str = "package") -> Optional[Dict[str, Any]]:
        """
        Ajoute une dépendance au contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            name (str): Le nom de la dépendance
            version (str, optional): La version de la dépendance
            type (str): Le type de dépendance (package, library, framework)
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
        
        dependency = {
            "name": name,
            "version": version,
            "type": type,
            "added_at": self._get_timestamp()
        }
        
        context["dependencies"][name] = dependency
        
        # Persister le contexte mis à jour
        if self.storage_dir:
            self._persist_context(session_id)
        
        return context
    
    def set_language_context(self, session_id: str, language: str, 
                            version: str = None, frameworks: List[str] = None) -> Optional[Dict[str, Any]]:
        """
        Définit le contexte de langage pour une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            language (str): Le langage principal du projet
            version (str, optional): La version du langage
            frameworks (List[str], optional): Les frameworks utilisés
            
        Returns:
            dict: Le contexte mis à jour ou None si la session n'existe pas
        """
        language_context = {
            "language": language,
            "version": version,
            "frameworks": frameworks or [],
            "updated_at": self._get_timestamp()
        }
        
        return self.update_context(session_id, {"language_context": language_context})
    
    def delete_context(self, session_id: str) -> bool:
        """
        Supprime le contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            
        Returns:
            bool: True si le contexte a été supprimé, False sinon
        """
        if session_id in self.contexts:
            del self.contexts[session_id]
            
            # Supprimer le fichier de contexte persistant
            if self.storage_dir:
                context_path = os.path.join(self.storage_dir, f"{session_id}.json")
                if os.path.exists(context_path):
                    os.remove(context_path)
            
            return True
        return False
    
    def _persist_context(self, session_id: str) -> bool:
        """
        Persiste le contexte d'une session sur le disque.
        
        Args:
            session_id (str): Identifiant unique de la session
            
        Returns:
            bool: True si le contexte a été persisté, False sinon
        """
        if not self.storage_dir or session_id not in self.contexts:
            return False
        
        try:
            context_path = os.path.join(self.storage_dir, f"{session_id}.json")
            
            # Créer une copie du contexte pour la sérialisation
            context_copy = self.contexts[session_id].copy()
            
            # Limiter la taille du contenu des fichiers pour éviter des fichiers JSON trop volumineux
            if "open_files" in context_copy:
                for file in context_copy["open_files"]:
                    if "content" in file and file["content"] and len(file["content"]) > 1000:
                        file["content"] = file["content"][:1000] + "... [truncated]"
            
            with open(context_path, 'w', encoding='utf-8') as f:
                json.dump(context_copy, f, ensure_ascii=False, indent=2)
            
            return True
        except Exception as e:
            print(f"Erreur lors de la persistance du contexte: {e}")
            return False
    
    def _load_context(self, session_id: str) -> bool:
        """
        Charge le contexte d'une session depuis le disque.
        
        Args:
            session_id (str): Identifiant unique de la session
            
        Returns:
            bool: True si le contexte a été chargé, False sinon
        """
        if not self.storage_dir:
            return False
        
        try:
            context_path = os.path.join(self.storage_dir, f"{session_id}.json")
            if not os.path.exists(context_path):
                return False
            
            with open(context_path, 'r', encoding='utf-8') as f:
                context = json.load(f)
            
            self.contexts[session_id] = context
            return True
        except Exception as e:
            print(f"Erreur lors du chargement du contexte: {e}")
            return False
    
    def list_sessions(self) -> List[str]:
        """
        Liste les identifiants de toutes les sessions disponibles.
        
        Returns:
            List[str]: Liste des identifiants de session
        """
        sessions = list(self.contexts.keys())
        
        # Ajouter les sessions persistées sur le disque
        if self.storage_dir and os.path.exists(self.storage_dir):
            for filename in os.listdir(self.storage_dir):
                if filename.endswith('.json'):
                    session_id = filename[:-5]  # Enlever l'extension .json
                    if session_id not in sessions:
                        sessions.append(session_id)
        
        return sessions
    
    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Génère un résumé du contexte d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            
        Returns:
            Dict[str, Any]: Résumé du contexte ou None si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return None
        
        return {
            "session_id": session_id,
            "created_at": context.get("created_at"),
            "updated_at": context.get("updated_at"),
            "current_file": context.get("current_file"),
            "open_files_count": len(context.get("open_files", [])),
            "code_history_count": len(context.get("code_history", [])),
            "conversation_history_count": len(context.get("conversation_history", [])),
            "language_context": context.get("language_context"),
            "dependencies_count": len(context.get("dependencies", {})),
            "metadata": context.get("metadata", {})
        }
    
    def add_execution_to_context(self, session_id: str, tool_name: str, args: Dict[str, Any], 
                                result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ajoute une exécution d'outil à l'historique d'exécution d'une session.
        
        Args:
            session_id (str): Identifiant unique de la session
            tool_name (str): Nom de l'outil exécuté
            args (Dict[str, Any]): Arguments passés à l'outil
            result (Dict[str, Any]): Résultat de l'exécution
            
        Returns:
            dict or bool: Le contexte mis à jour ou False si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return False
            
        # S'assurer que la clé execution_history existe
        if "execution_history" not in context:
            context["execution_history"] = []
            
        execution = {
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "timestamp": self._get_timestamp()
        }
        
        context["execution_history"].append(execution)
        context["updated_at"] = self._get_timestamp()
        
        # Limiter la taille de l'historique d'exécution
        if len(context["execution_history"]) > 20:
            context["execution_history"] = context["execution_history"][-20:]
        
        # Persister le contexte mis à jour
        if self.storage_dir:
            self._persist_context(session_id)
                
        return context
        
    def update_context_metadata(self, session_id: str, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Met à jour les métadonnées d'un contexte.
        
        Args:
            session_id (str): Identifiant unique de la session
            metadata (Dict[str, Any]): Nouvelles métadonnées à fusionner
            
        Returns:
            dict or bool: Le contexte mis à jour ou False si la session n'existe pas
        """
        context = self.get_context(session_id)
        if context is None:
            return False
            
        # Fusionner les métadonnées
        context["metadata"].update(metadata)
        context["updated_at"] = self._get_timestamp()
        
        # Persister le contexte mis à jour
        if self.storage_dir:
            self._persist_context(session_id)
                
        return context
    
    def _get_timestamp(self) -> str:
        """Retourne un timestamp au format ISO."""
        return datetime.now().isoformat()
