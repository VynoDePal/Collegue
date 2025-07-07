"""
Code Generation - Outil de génération de code basé sur une description
"""
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError


class CodeGenerationRequest(BaseModel):
    """Modèle de requête pour la génération de code."""
    description: str = Field(..., description="Description du code à générer")
    language: str = Field(..., description="Langage de programmation cible")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    context: Optional[Dict[str, Any]] = Field(None, description="Contexte supplémentaire pour la génération")
    file_path: Optional[str] = Field(None, description="Chemin du fichier où le code sera inséré")
    constraints: Optional[List[str]] = Field(None, description="Contraintes spécifiques pour la génération de code")


class CodeGenerationResponse(BaseModel):
    """Modèle de réponse pour la génération de code."""
    code: str = Field(..., description="Code généré")
    language: str = Field(..., description="Langage du code généré")
    explanation: Optional[str] = Field(None, description="Explication du code généré")
    suggestions: Optional[List[str]] = Field(None, description="Suggestions d'amélioration ou alternatives")


class CodeGenerationTool(BaseTool):
    """Outil de génération de code basé sur une description."""

    def get_name(self) -> str:
        """Retourne le nom unique de l'outil."""
        return "code_generation"

    def get_description(self) -> str:
        """Retourne la description de l'outil."""
        return "Génère du code dans différents langages basé sur une description textuelle"

    def get_request_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les requêtes."""
        return CodeGenerationRequest

    def get_response_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les réponses."""
        return CodeGenerationResponse

    def get_supported_languages(self) -> List[str]:
        """Retourne la liste des langages supportés."""
        return ["python", "javascript", "typescript", "java", "c#", "go", "rust"]

    def get_usage_description(self) -> str:
        """Description détaillée de l'utilisation de l'outil de génération de code."""
        return ("Outil de génération de code qui peut utiliser une description textuelle pour générer du code dans "
                "differents langages. Il peut fournir des explications et des suggestions d'amélioration.")

    def get_examples(self) -> List[Dict[str, Any]]:
        """Exemples d'utilisation spécifiques à l'outil de génération de code."""
        return [
            {
                "title": "Génération d'une fonction Python simple",
                "description": "Générer une fonction Python pour calculer la factorielle",
                "request": {
                    "description": "Créer une fonction qui calcule la factorielle d'un nombre",
                    "language": "python",
                    "context": "Fonction récursive avec gestion d'erreurs",
                    "constraints": ["optimisation", "docstring"]
                },
                "expected_response": "Code Python avec fonction factorielle, docstring et gestion d'erreurs"
            },
            {
                "title": "Génération d'une classe JavaScript",
                "description": "Créer une classe JavaScript pour gérer un panier d'achats",
                "request": {
                    "description": "Classe ShoppingCart avec méthodes pour ajouter, retirer et calculer le total",
                    "language": "javascript",
                    "context": "E-commerce, gestion d'état",
                    "constraints": ["ES6+", "validation", "méthodes fluides"]
                },
                "expected_response": "Classe JavaScript moderne avec méthodes chainables et validation"
            },
            {
                "title": "Génération d'API REST en TypeScript",
                "description": "Créer un contrôleur d'API REST avec validation",
                "request": {
                    "description": "Contrôleur REST pour gérer les utilisateurs (CRUD)",
                    "language": "typescript",
                    "context": "API REST, validation des données",
                    "constraints": ["types stricts", "middleware", "gestion d'erreurs"]
                },
                "expected_response": "Contrôleur TypeScript avec interfaces, validation et gestion d'erreurs"
            },
            {
                "title": "Génération rapide d'algorithme",
                "description": "Générer un algorithme de tri sans contraintes spécifiques",
                "request": {
                    "description": "Implémentation de l'algorithme de tri rapide (quicksort)",
                    "language": "python"
                },
                "expected_response": "Implémentation simple et claire de l'algorithme quicksort"
            }
        ]

    def get_capabilities(self) -> List[str]:
        """Capacités spécifiques de l'outil de génération de code."""
        return [
            "Génération de code dans 7+ langages de programmation",
            "Prise en compte du contexte et des contraintes spécifiques",
            "Génération de fonctions, classes, modules et applications complètes",
            "Respect des bonnes pratiques et conventions de chaque langage",
            "Inclusion automatique de documentation et commentaires",
            "Gestion d'erreurs et validation selon les standards du langage",
            "Suggestions d'amélioration et optimisations",
            "Support des frameworks et bibliothèques populaires",
            "Génération de code avec patterns et architectures modernes",
            "Adaptation du style de code selon le langage cible"
        ]

    def get_required_config_keys(self) -> List[str]:
        """Retourne les clés de configuration requises."""
        return []  # Aucune configuration obligatoire

    def _execute_core_logic(self, request: CodeGenerationRequest, **kwargs) -> CodeGenerationResponse:
        """
        Exécute la logique principale de génération de code.

        Args:
            request: Requête de génération de code validée
            **kwargs: Services additionnels (llm_manager, parser, etc.)

        Returns:
            CodeGenerationResponse: Le code généré
        """
        llm_manager = kwargs.get('llm_manager')

        # Utilisation du LLM centralisé si fourni
        if llm_manager is not None:
            try:
                prompt = self._build_generation_prompt(request)
                generated_code = llm_manager.sync_generate(prompt)
                explanation = f"Code généré par LLM ({getattr(llm_manager, 'model_name', 'modèle inconnu')}) pour la description fournie."

                # Suggestions basées sur le langage
                suggestions = self._get_language_suggestions(request.language)

                return CodeGenerationResponse(
                    code=generated_code,
                    language=request.language,
                    explanation=explanation,
                    suggestions=suggestions
                )
            except Exception as e:
                self.logger.warning(f"Erreur avec LLM, utilisation du fallback: {e}")
                # Fallback vers génération locale en cas d'erreur LLM
                return self._generate_fallback_code(request)
        else:
            # Génération locale si pas de LLM
            return self._generate_fallback_code(request)

    def _build_generation_prompt(self, request: CodeGenerationRequest) -> str:
        """
        Construit le prompt pour le LLM.

        Args:
            request: Requête de génération

        Returns:
            Prompt optimisé pour le LLM
        """
        prompt_parts = [
            f"Génère un code {request.language} répondant à la description suivante :",
            f"Description: {request.description}"
        ]

        if request.context:
            prompt_parts.append(f"Contexte supplémentaire: {request.context}")

        if request.constraints:
            constraints_str = ", ".join(request.constraints)
            prompt_parts.append(f"Contraintes à respecter: {constraints_str}")

        if request.file_path:
            prompt_parts.append(f"Fichier de destination: {request.file_path}")

        # Instructions spécifiques selon le langage
        language_instructions = self._get_language_instructions(request.language)
        if language_instructions:
            prompt_parts.append(f"Instructions spécifiques pour {request.language}: {language_instructions}")

        prompt_parts.append("Fournis uniquement le code sans explications supplémentaires.")

        return "\n".join(prompt_parts)

    def _get_language_instructions(self, language: str) -> str:
        """
        Retourne des instructions spécifiques au langage.

        Args:
            language: Langage de programmation

        Returns:
            Instructions spécifiques au langage
        """
        instructions = {
            "python": "Utilise les bonnes pratiques Python (PEP 8), inclus des docstrings et gère les exceptions.",
            "javascript": "Utilise ES6+, inclus des commentaires JSDoc et gère les erreurs avec try/catch.",
            "typescript": "Utilise les types TypeScript, interfaces appropriées et gère les erreurs de type.",
            "java": "Respecte les conventions Java, utilise les annotations et gère les exceptions.",
            "c#": "Utilise les conventions C#, propriétés appropriées et gestion des exceptions.",
            "go": "Respecte les idiomes Go, gère les erreurs explicitement.",
            "rust": "Utilise les patterns Rust, gère la mémoire de façon sûre avec Result/Option."
        }
        return instructions.get(language.lower(), "")

    def _get_language_suggestions(self, language: str) -> List[str]:
        """
        Retourne des suggestions d'amélioration par langage.

        Args:
            language: Langage de programmation

        Returns:
            Liste de suggestions
        """
        suggestions = {
            "python": [
                "Ajouter des tests unitaires avec pytest",
                "Utiliser des type hints pour une meilleure documentation",
                "Considérer l'utilisation de dataclasses pour les structures de données"
            ],
            "javascript": [
                "Ajouter des tests avec Jest ou Mocha",
                "Considérer l'utilisation d'ESLint pour la qualité du code",
                "Implémenter la gestion d'erreurs asynchrones"
            ],
            "typescript": [
                "Configurer strict mode dans tsconfig.json",
                "Utiliser des unions types et interfaces appropriées",
                "Ajouter des tests avec Jest et @types/jest"
            ]
        }
        return suggestions.get(language.lower(), [
            "Ajouter des commentaires pour expliquer la logique complexe",
            "Implémenter une gestion d'erreurs robuste",
            "Considérer l'ajout de tests unitaires"
        ])

    def _generate_fallback_code(self, request: CodeGenerationRequest) -> CodeGenerationResponse:
        """
        Génère du code de base sans LLM (fallback).

        Args:
            request: Requête de génération

        Returns:
            Réponse avec code de base généré
        """
        language = request.language.lower()

        if language == "python":
            code = self._generate_python_fallback(request)
            explanation = "Code Python de base généré avec structure standard."
        elif language == "javascript":
            code = self._generate_javascript_fallback(request)
            explanation = "Code JavaScript de base généré avec structure moderne."
        elif language == "typescript":
            code = self._generate_typescript_fallback(request)
            explanation = "Code TypeScript de base généré avec typage fort."
        else:
            code = f"// Code de base pour {request.language}\n// Description: {request.description}\n\n// TODO: Implémenter la logique demandée"
            explanation = f"Template de base pour {request.language}. Implémentation manuelle requise."

        suggestions = self._get_language_suggestions(request.language)

        return CodeGenerationResponse(
            code=code,
            language=request.language,
            explanation=explanation,
            suggestions=suggestions
        )

    def _generate_python_fallback(self, request: CodeGenerationRequest) -> str:
        """Génère du code Python de base."""
        return f'''"""
Module généré automatiquement
Description: {request.description}
"""
import logging
from typing import Any, Dict, List, Optional


class GeneratedModule:
    """Classe principale du module généré."""
    
    def __init__(self):
        """Initialise le module."""
        self.logger = logging.getLogger(__name__)
    
    def main_function(self) -> None:
        """
        Fonction principale du module.
        
        TODO: Implémenter la logique demandée:
        {request.description}
        """
        self.logger.info("Début d'exécution du module généré")
        
        # TODO: Ajouter votre logique ici
        print("Module généré avec succès!")
        
        self.logger.info("Fin d'exécution du module généré")


def main():
    """Point d'entrée principal."""
    module = GeneratedModule()
    module.main_function()


if __name__ == "__main__":
    main()
'''

    def _generate_javascript_fallback(self, request: CodeGenerationRequest) -> str:
        """Génère du code JavaScript de base."""
        return f'''/**
 * Module généré automatiquement
 * Description: {request.description}
 */

class GeneratedModule {{
    constructor() {{
        this.name = "GeneratedModule";
    }}
    
    /**
     * Fonction principale du module
     * TODO: Implémenter la logique demandée: {request.description}
     */
    async mainFunction() {{
        console.log("Début d'exécution du module généré");
        
        try {{
            // TODO: Ajouter votre logique ici
            console.log("Module généré avec succès!");
            
        }} catch (error) {{
            console.error("Erreur dans le module:", error);
            throw error;
        }}
        
        console.log("Fin d'exécution du module généré");
    }}
}}

/**
 * Point d'entrée principal
 */
async function main() {{
    const module = new GeneratedModule();
    await module.mainFunction();
}}

// Exécution si le fichier est lancé directement
if (require.main === module) {{
    main().catch(console.error);
}}

module.exports = {{ GeneratedModule }};
'''

    def _generate_typescript_fallback(self, request: CodeGenerationRequest) -> str:
        """Génère du code TypeScript de base."""
        return f'''/**
 * Module généré automatiquement
 * Description: {request.description}
 */

interface ModuleConfig {{
    name: string;
    version: string;
    debug?: boolean;
}}

interface ModuleResult {{
    success: boolean;
    message: string;
    data?: any;
}}

class GeneratedModule {{
    private config: ModuleConfig;
    
    constructor(config: ModuleConfig) {{
        this.config = config;
    }}
    
    /**
     * Fonction principale du module
     * TODO: Implémenter la logique demandée: {request.description}
     */
    public async mainFunction(): Promise<ModuleResult> {{
        console.log(`Début d'exécution du module ${{this.config.name}}`);
        
        try {{
            // TODO: Ajouter votre logique ici
            const result: ModuleResult = {{
                success: true,
                message: "Module généré avec succès!",
                data: null
            }};
            
            console.log("Fin d'exécution du module généré");
            return result;
            
        }} catch (error) {{
            console.error("Erreur dans le module:", error);
            return {{
                success: false,
                message: error instanceof Error ? error.message : "Erreur inconnue"
            }};
        }}
    }}
}}

/**
 * Point d'entrée principal
 */
async function main(): Promise<void> {{
    const config: ModuleConfig = {{
        name: "GeneratedModule",
        version: "1.0.0",
        debug: true
    }};
    
    const module = new GeneratedModule(config);
    const result = await module.mainFunction();
    
    if (!result.success) {{
        console.error("Échec du module:", result.message);
        process.exit(1);
    }}
}}

// Exécution si le fichier est lancé directement
if (require.main === module) {{
    main().catch(console.error);
}}

export {{ GeneratedModule, ModuleConfig, ModuleResult }};
'''


# Fonction de compatibilité pour l'ancien système
def generate_code(request: CodeGenerationRequest, llm_service=None) -> CodeGenerationResponse:
    """
    Fonction de compatibilité avec l'ancien système.

    Args:
        request: Requête de génération de code
        llm_service: Service LLM (pour compatibilité)

    Returns:
        Réponse de génération de code
    """
    tool = CodeGenerationTool()
    return tool.execute(request, llm_manager=llm_service)
