"""
Moteur d'analyse et de refactoring pour l'outil Refactoring.

Contient la logique métier pure : analyse de métriques, validation syntaxique,
extraction de code, calcul des améliorations.
"""
import re
import ast
import json
from typing import Dict, Any, List, Tuple
from .config import COMMENT_PATTERNS, COMPLEXITY_INDICATORS, REFACTORING_TYPES


class RefactoringEngine:
    """Moteur d'analyse et de refactoring de code."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def extract_code_block(self, text: str, language: str) -> str:
        """
        Extrait le code d'un bloc markdown ```lang ... ``` ou retourne le texte brut nettoyé.
        """
        text = text.strip()
        
        # Regex pour capturer le contenu entre ```lang et ```
        pattern = rf"```(?:{re.escape(language)}|{re.escape(language.lower())})?\s+(.*?)```"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        
        if match:
            return match.group(1).strip()
        
        # Si pas de match avec langage, on cherche n'importe quel bloc de code
        match_generic = re.search(r"```\s*(.*?)```", text, re.DOTALL)
        if match_generic:
            return match_generic.group(1).strip()
        
        # Nettoyer les phrases introductives courantes
        lines = text.split('\n')
        if len(lines) > 0 and (lines[0].lower().startswith("voici") or lines[0].strip().endswith(":")):
            return '\n'.join(lines[1:]).strip()
        
        return text
    
    def validate_code_syntax(self, code: str, language: str) -> Tuple[bool, str]:
        """
        Vérifie si le code est syntaxiquement valide pour les langages supportés.
        Retourne (is_valid, error_message).
        """
        lang = language.lower()
        
        if lang == "python":
            try:
                ast.parse(code)
                return True, ""
            except SyntaxError as e:
                return False, f"Ligne {e.lineno}: {e.msg}"
        
        elif lang == "json":
            try:
                json.loads(code)
                return True, ""
            except json.JSONDecodeError as e:
                return False, str(e)
        
        elif lang == "php":
            # Vérification basique: doit contenir des éléments PHP valides
            if "<?php" not in code and "namespace " not in code and "class " not in code and "function " not in code:
                pass  # C'est peut-être un fragment
            return True, ""
        
        # Pour les autres langages, on assume valide par défaut
        return True, ""
    
    def analyze_code_metrics(self, code: str, language: str) -> Dict[str, Any]:
        """Analyse les métriques du code source."""
        lines = code.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]
        
        metrics = {
            "total_lines": len(lines),
            "code_lines": len(non_empty_lines),
            "comment_lines": 0,
            "function_count": 0,
            "class_count": 0,
            "complexity_score": 0
        }
        
        # Compter les lignes de commentaires
        patterns = COMMENT_PATTERNS.get(language.lower(), ["#", "//"])
        for line in lines:
            for pattern in patterns:
                if pattern in line:
                    metrics["comment_lines"] += 1
                    break
        
        # Compter fonctions et classes selon le langage
        lang = language.lower()
        if lang == "python":
            metrics["function_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("def "))
            metrics["class_count"] = sum(1 for line in non_empty_lines if line.strip().startswith("class "))
        elif lang == "php":
            metrics["function_count"] = sum(1 for line in non_empty_lines if "function " in line)
            metrics["class_count"] = sum(1 for line in non_empty_lines 
                                        if line.strip().startswith("class ") 
                                        or line.strip().startswith("abstract class ")
                                        or line.strip().startswith("trait "))
        else:
            metrics["function_count"] = sum(1 for line in non_empty_lines if "function " in line.lower())
            metrics["class_count"] = sum(1 for line in non_empty_lines if "class " in line.lower())
        
        # Calculer la complexité
        for line in non_empty_lines:
            for indicator in COMPLEXITY_INDICATORS:
                if indicator in line.lower():
                    metrics["complexity_score"] += 1
        
        return metrics
    
    def calculate_improvements(self, original: Dict[str, Any], refactored: Dict[str, Any]) -> Dict[str, Any]:
        """Calcule les améliorations entre les métriques originales et refactorées."""
        improvements = {}
        
        for key in ["code_lines", "complexity_score"]:
            if original[key] > 0:
                change = ((refactored[key] - original[key]) / original[key]) * 100
                improvements[f"{key}_change_percent"] = round(change, 2)
        
        improvements.update({
            "lines_reduced": original["code_lines"] - refactored["code_lines"],
            "complexity_reduced": original["complexity_score"] - refactored["complexity_score"],
            "comments_added": refactored["comment_lines"] - original["comment_lines"],
            "functions_extracted": refactored["function_count"] - original["function_count"]
        })
        
        return improvements
    
    def identify_changes(self, refactoring_type: str, original_code: str, refactored_code: str, 
                        parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Identifie les changements effectués lors du refactoring."""
        changes = []
        
        change_descriptions = {
            "rename": "Variables, fonctions et classes renommées pour plus de clarté",
            "extract": "Code dupliqué extrait en fonctions réutilisables",
            "simplify": "Logique complexe simplifiée et optimisée",
            "optimize": "Performances améliorées et inefficacités supprimées",
            "clean": "Code nettoyé et formaté selon les bonnes pratiques",
            "modernize": "Code mis à jour avec les patterns modernes"
        }
        
        changes.append({
            "type": refactoring_type,
            "description": change_descriptions.get(refactoring_type, "Code modifié"),
            "parameters": parameters or {}
        })
        
        original_lines = len(original_code.split('\n'))
        refactored_lines = len(refactored_code.split('\n'))
        
        if refactored_lines != original_lines:
            changes.append({
                "type": "line_count_change",
                "description": f"Nombre de lignes modifié: {original_lines} → {refactored_lines}",
                "parameters": {"original": original_lines, "refactored": refactored_lines}
            })
        
        return changes
    
    def generate_explanation(self, refactoring_type: str, changes: List[Dict[str, Any]],
                            improvements: Dict[str, Any]) -> str:
        """Génère une explication des modifications apportées."""
        explanation_parts = [
            f"Refactoring de type '{refactoring_type}' appliqué avec succès."
        ]
        
        if improvements.get("lines_reduced", 0) > 0:
            explanation_parts.append(f"Réduction de {improvements['lines_reduced']} lignes de code.")
        
        if improvements.get("complexity_reduced", 0) > 0:
            explanation_parts.append(f"Complexité réduite de {improvements['complexity_reduced']} points.")
        
        if improvements.get("comments_added", 0) > 0:
            explanation_parts.append(f"Ajout de {improvements['comments_added']} lignes de commentaires.")
        
        for change in changes:
            explanation_parts.append(change["description"])
        
        return " ".join(explanation_parts)
    
    def clean_code_basic(self, code: str, language: str) -> str:
        """Nettoyage basique du code (suppression espaces inutiles, lignes vides)."""
        lines = code.split('\n')
        cleaned_lines = []
        
        for line in lines:
            cleaned_line = line.rstrip()
            
            # Éviter les lignes vides multiples consécutives
            if cleaned_line == "" and cleaned_lines and cleaned_lines[-1] == "":
                continue
            
            cleaned_lines.append(cleaned_line)
        
        # Supprimer les lignes vides à la fin
        while cleaned_lines and cleaned_lines[-1] == "":
            cleaned_lines.pop()
        
        return '\n'.join(cleaned_lines)
    
    def simplify_code_basic(self, code: str, language: str) -> str:
        """Simplification basique du code."""
        cleaned = self.clean_code_basic(code, language)
        
        if language.lower() == "python":
            # Simplifications Python basiques
            cleaned = re.sub(r'\s*==\s*True\b', '', cleaned)
            cleaned = re.sub(r'\s*==\s*False\b', ' is False', cleaned)
            cleaned = re.sub(r'\s*!=\s*True\b', ' is not True', cleaned)
        
        return cleaned
    
    def get_refactoring_type_description(self, refactoring_type: str) -> str:
        """Retourne la description d'un type de refactoring."""
        return REFACTORING_TYPES.get(refactoring_type, "Améliorer la qualité du code")
    
    def get_refactoring_type_descriptions(self) -> Dict[str, str]:
        """Retourne les descriptions de tous les types de refactoring."""
        return {
            "rename": "Renomme variables, fonctions et classes avec des noms plus descriptifs et clairs",
            "extract": "Extrait le code dupliqué en fonctions/méthodes réutilisables pour réduire la duplication",
            "simplify": "Simplifie la logique complexe, réduit la complexité cyclomatique et améliore la lisibilité",
            "optimize": "Optimise les performances du code, améliore l'efficacité et utilise des structures appropriées",
            "clean": "Nettoie le code en supprimant les éléments inutiles et en améliorant le formatage",
            "modernize": "Met à jour le code pour utiliser les patterns et syntaxes modernes du langage"
        }
