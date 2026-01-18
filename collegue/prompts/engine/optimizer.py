"""
Optimiseur de prompts par langage de programmation
"""
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class LanguageOptimizer:
    """Optimise les prompts selon le langage de programmation cible."""
    
    # Règles d'optimisation par langage
    LANGUAGE_RULES = {
        "python": {
            "conventions": ["PEP 8", "Type hints", "Docstrings"],
            "keywords": ["pythonic", "duck typing", "list comprehension", "generator"],
            "frameworks": ["FastAPI", "Django", "Flask", "Pydantic"],
            "best_practices": [
                "Use descriptive variable names",
                "Follow PEP 8 style guide",
                "Add type hints for better code clarity",
                "Write comprehensive docstrings",
                "Prefer list comprehensions over loops when appropriate"
            ],
            "context_hints": "Python emphasizes readability and simplicity."
        },
        "javascript": {
            "conventions": ["ES6+", "async/await", "const/let over var"],
            "keywords": ["promises", "callbacks", "arrow functions", "destructuring"],
            "frameworks": ["React", "Vue", "Angular", "Node.js", "Express"],
            "best_practices": [
                "Use const for immutable values",
                "Prefer async/await over callbacks",
                "Use arrow functions appropriately",
                "Implement proper error handling",
                "Use destructuring for cleaner code"
            ],
            "context_hints": "JavaScript is dynamic and event-driven."
        },
        "typescript": {
            "conventions": ["Strong typing", "Interfaces", "Generics", "ES6+"],
            "keywords": ["type safety", "interfaces", "generics", "decorators", "enums"],
            "frameworks": ["Angular", "React with TypeScript", "NestJS", "Express with TypeScript"],
            "best_practices": [
                "Define interfaces for object shapes",
                "Use enums for constant values",
                "Leverage generics for reusable code",
                "Avoid using 'any' type",
                "Use strict mode for better type checking"
            ],
            "context_hints": "TypeScript adds static typing to JavaScript for better maintainability."
        },
        "java": {
            "conventions": ["CamelCase", "SOLID principles", "Design patterns"],
            "keywords": ["OOP", "inheritance", "polymorphism", "interfaces", "generics"],
            "frameworks": ["Spring", "Spring Boot", "Hibernate", "Maven", "Gradle"],
            "best_practices": [
                "Follow SOLID principles",
                "Use appropriate design patterns",
                "Write unit tests",
                "Handle exceptions properly",
                "Use meaningful class and method names"
            ],
            "context_hints": "Java is strongly typed and object-oriented."
        }
    }
    
    def __init__(self):
        """Initialise l'optimiseur de langage."""
        self.custom_rules: Dict[str, Dict[str, Any]] = {}
    
    def optimize_prompt(self, base_prompt: str, language: str, 
                       context: Optional[Dict[str, Any]] = None) -> str:
        """
        Optimise un prompt selon le langage cible.
        
        Args:
            base_prompt: Prompt de base à optimiser
            language: Langage de programmation cible
            context: Contexte additionnel (framework, style, etc.)
            
        Returns:
            Prompt optimisé pour le langage
        """
        language_lower = language.lower()
        
        # Récupérer les règles du langage
        rules = self.LANGUAGE_RULES.get(language_lower, {})
        if not rules:
            logger.warning(f"Pas de règles d'optimisation pour {language}")
            return base_prompt
        
        optimized_parts = [base_prompt]
        
        if rules.get("context_hints"):
            optimized_parts.append(f"\nContext: {rules['context_hints']}")
        
        if rules.get("conventions"):
            conventions = ", ".join(rules["conventions"])
            optimized_parts.append(f"\nFollow these conventions: {conventions}")
        
        if rules.get("best_practices"):
            practices = "\n- ".join(rules["best_practices"])
            optimized_parts.append(f"\nBest practices to follow:\n- {practices}")
        
        if context:
            if context.get("framework"):
                framework = context["framework"]
                if framework in rules.get("frameworks", []):
                    optimized_parts.append(f"\nUse {framework} framework patterns and conventions.")
            
            if context.get("style_guide"):
                optimized_parts.append(f"\nFollow the {context['style_guide']} style guide.")
        
        return "\n".join(optimized_parts)
    
    def add_custom_rules(self, language: str, rules: Dict[str, Any]) -> None:
        """
        Ajoute des règles personnalisées pour un langage.
        
        Args:
            language: Langage concerné
            rules: Règles personnalisées
        """
        self.custom_rules[language.lower()] = rules
        logger.info(f"Règles personnalisées ajoutées pour {language}")
    
    def get_language_context(self, language: str) -> Dict[str, Any]:
        """
        Récupère le contexte complet d'un langage.
        
        Args:
            language: Langage demandé
            
        Returns:
            Contexte du langage avec toutes les règles
        """
        language_lower = language.lower()
        base_rules = self.LANGUAGE_RULES.get(language_lower, {})
        custom_rules = self.custom_rules.get(language_lower, {})
        
        context = base_rules.copy()
        context.update(custom_rules)
        
        return context
    
    def suggest_improvements(self, prompt: str, language: str) -> List[str]:
        """
        Suggère des améliorations pour un prompt.
        
        Args:
            prompt: Prompt à analyser
            language: Langage cible
            
        Returns:
            Liste de suggestions d'amélioration
        """
        suggestions = []
        language_lower = language.lower()
        rules = self.LANGUAGE_RULES.get(language_lower, {})
        
        if not rules:
            return ["Consider adding language-specific optimizations"]
        
        conventions = rules.get("conventions", [])
        for convention in conventions:
            if convention.lower() not in prompt.lower():
                suggestions.append(f"Consider mentioning {convention} convention")
        
        if "framework" not in prompt.lower() and rules.get("frameworks"):
            suggestions.append("Consider specifying the target framework if applicable")
        
        if "best practice" not in prompt.lower() and "practice" not in prompt.lower():
            suggestions.append("Consider emphasizing best practices for the language")
        
        return suggestions
