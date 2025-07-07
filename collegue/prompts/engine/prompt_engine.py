"""
Prompt Engine - Moteur de gestion des prompts personnalisés
"""
import json
import os
import logging
from typing import Dict, List, Optional, Any, Union
import datetime
from pathlib import Path

from .models import PromptTemplate, PromptCategory, PromptExecution, PromptLibrary, PromptVariable

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PromptEngine:
    """Moteur de gestion des prompts personnalisés."""
    
    def __init__(self, storage_path: Optional[str] = None):
        """Initialise le moteur de prompts.
        
        Args:
            storage_path: Chemin vers le dossier de stockage des prompts.
                          Si None, utilise le dossier par défaut.
        """
        self.library = PromptLibrary()
        
        if storage_path is None:
            # Utiliser le dossier par défaut dans le package
            current_dir = os.path.dirname(os.path.abspath(__file__))
            parent_dir = os.path.dirname(current_dir)
            self.storage_path = os.path.join(parent_dir, "templates")
        else:
            self.storage_path = storage_path
            
        # Créer le dossier de stockage s'il n'existe pas
        os.makedirs(self.storage_path, exist_ok=True)
        
        # Charger les templates et catégories existants
        self._load_library()
    
    def _load_library(self):
        """Charge la bibliothèque de prompts depuis le stockage."""
        # Charger les catégories
        categories_path = os.path.join(self.storage_path, "categories.json")
        if os.path.exists(categories_path):
            try:
                with open(categories_path, 'r', encoding='utf-8') as f:
                    categories_data = json.load(f)
                    for cat_id, cat_data in categories_data.items():
                        self.library.categories[cat_id] = PromptCategory(**cat_data)
            except Exception as e:
                logger.error(f"Erreur lors du chargement des catégories: {str(e)}")
        
        # Charger les templates
        templates_dir = os.path.join(self.storage_path, "templates")
        os.makedirs(templates_dir, exist_ok=True)
        
        for file_path in Path(templates_dir).glob("*.json"):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    template_data = json.load(f)
                    
                    # Convertir les variables en objets PromptVariable
                    if "variables" in template_data:
                        template_data["variables"] = [
                            PromptVariable(**var) if isinstance(var, dict) else var 
                            for var in template_data["variables"]
                        ]
                    
                    template = PromptTemplate(**template_data)
                    self.library.templates[template.id] = template
            except Exception as e:
                logger.error(f"Erreur lors du chargement du template {file_path}: {str(e)}")
    
    def _save_library(self):
        """Sauvegarde la bibliothèque de prompts dans le stockage."""
        # Sauvegarder les catégories
        categories_path = os.path.join(self.storage_path, "categories.json")
        try:
            categories_data = {cat_id: cat.model_dump() for cat_id, cat in self.library.categories.items()}
            with open(categories_path, 'w', encoding='utf-8') as f:
                json.dump(categories_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde des catégories: {str(e)}")
        
        # Sauvegarder les templates
        templates_dir = os.path.join(self.storage_path, "templates")
        os.makedirs(templates_dir, exist_ok=True)
        
        for template_id, template in self.library.templates.items():
            try:
                file_path = os.path.join(templates_dir, f"{template_id}.json")
                with open(file_path, 'w', encoding='utf-8') as f:
                    # Convertir en dictionnaire pour la sérialisation
                    template_dict = template.model_dump()
                    json.dump(template_dict, f, ensure_ascii=False, indent=2, 
                             default=lambda o: o.isoformat() if isinstance(o, datetime.datetime) else None)
            except Exception as e:
                logger.error(f"Erreur lors de la sauvegarde du template {template_id}: {str(e)}")
    
    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """Récupère un template par son ID."""
        return self.library.templates.get(template_id)
    
    def get_all_templates(self) -> List[PromptTemplate]:
        """Récupère tous les templates disponibles."""
        return list(self.library.templates.values())
    
    def get_templates_by_category(self, category: str) -> List[PromptTemplate]:
        """Récupère les templates d'une catégorie spécifique."""
        return [t for t in self.library.templates.values() if t.category == category]
    
    def get_templates_by_tags(self, tags: List[str]) -> List[PromptTemplate]:
        """Récupère les templates qui ont tous les tags spécifiés."""
        return [t for t in self.library.templates.values() 
                if all(tag in t.tags for tag in tags)]
    
    def create_template(self, template_data: Dict[str, Any]) -> PromptTemplate:
        """Crée un nouveau template de prompt."""
        # Convertir les variables en objets PromptVariable si nécessaire
        if "variables" in template_data:
            template_data["variables"] = [
                PromptVariable(**var) if isinstance(var, dict) else var 
                for var in template_data["variables"]
            ]
        
        template = PromptTemplate(**template_data)
        self.library.templates[template.id] = template
        self._save_library()
        return template
    
    def update_template(self, template_id: str, template_data: Dict[str, Any]) -> Optional[PromptTemplate]:
        """Met à jour un template existant."""
        if template_id not in self.library.templates:
            return None
        
        # Récupérer le template existant
        existing = self.library.templates[template_id]
        
        # Mettre à jour les champs
        for key, value in template_data.items():
            if key == "variables" and value:
                # Convertir les variables en objets PromptVariable
                value = [PromptVariable(**var) if isinstance(var, dict) else var for var in value]
            setattr(existing, key, value)
        
        # Mettre à jour la date de modification
        existing.updated_at = datetime.datetime.now()
        
        self._save_library()
        return existing
    
    def delete_template(self, template_id: str) -> bool:
        """Supprime un template."""
        if template_id not in self.library.templates:
            return False
        
        del self.library.templates[template_id]
        
        # Supprimer également le fichier
        file_path = os.path.join(self.storage_path, "templates", f"{template_id}.json")
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Erreur lors de la suppression du fichier {file_path}: {str(e)}")
        
        self._save_library()
        return True
    
    def create_category(self, category_data: Dict[str, Any]) -> PromptCategory:
        """Crée une nouvelle catégorie."""
        category = PromptCategory(**category_data)
        self.library.categories[category.id] = category
        self._save_library()
        return category
    
    def get_all_categories(self) -> List[PromptCategory]:
        """Récupère toutes les catégories."""
        return list(self.library.categories.values())
    
    def get_category(self, category_id: str) -> Optional[PromptCategory]:
        """Récupère une catégorie par son ID."""
        return self.library.categories.get(category_id)
    
    def format_prompt(self, template_id: str, variables: Dict[str, Any], 
                     provider: Optional[str] = None) -> Optional[str]:
        """Formate un template avec les variables fournies."""
        template = self.get_template(template_id)
        if not template:
            return None
        
        # Sélectionner le template spécifique au fournisseur si disponible
        prompt_text = template.template
        if provider and provider in template.provider_specific:
            prompt_text = template.provider_specific[provider]
        
        # Valider les variables requises
        required_vars = [v.name for v in template.variables if v.required]
        missing_vars = [v for v in required_vars if v not in variables]
        
        if missing_vars:
            logger.error(f"Variables requises manquantes: {', '.join(missing_vars)}")
            return None
        
        # Appliquer les valeurs par défaut pour les variables manquantes non requises
        for var in template.variables:
            if var.name not in variables and not var.required and var.default is not None:
                variables[var.name] = var.default
        
        # Formater le template avec les variables
        try:
            formatted = prompt_text.format(**variables)
            
            # Enregistrer l'exécution dans l'historique
            execution = PromptExecution(
                template_id=template_id,
                variables=variables,
                provider=provider,
                formatted_prompt=formatted,
                execution_time=0.0  # À mettre à jour si on mesure le temps
            )
            self.library.history.append(execution)
            
            return formatted
        except KeyError as e:
            logger.error(f"Variable manquante dans le template: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Erreur lors du formatage du prompt: {str(e)}")
            return None
    
    def get_execution_history(self, limit: int = 100) -> List[PromptExecution]:
        """Récupère l'historique des exécutions de prompts."""
        return self.library.history[-limit:] if self.library.history else []
    
    def record_execution_result(self, execution_id: str, result: str, 
                               execution_time: float) -> bool:
        """Enregistre le résultat d'une exécution de prompt."""
        for execution in self.library.history:
            if execution.id == execution_id:
                execution.result = result
                execution.execution_time = execution_time
                return True
        return False
    
    def add_feedback(self, execution_id: str, feedback: Dict[str, Any]) -> bool:
        """Ajoute un feedback à une exécution de prompt."""
        for execution in self.library.history:
            if execution.id == execution_id:
                execution.feedback = feedback
                return True
        return False
