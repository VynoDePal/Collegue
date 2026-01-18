"""
Documentation - Outil de génération automatique de documentation
"""
import asyncio
from typing import Optional, Dict, Any, List, Union, Type
from pydantic import BaseModel, Field
from .base import BaseTool, ToolError


class DocumentationRequest(BaseModel):
    """Modèle de requête pour la génération de documentation."""
    code: str = Field(..., description="Code à documenter")
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    doc_style: Optional[str] = Field("standard", description="Style de documentation (standard, detailed, minimal, api)")
    doc_format: Optional[str] = Field("markdown", description="Format de documentation (markdown, rst, html, docstring)")
    include_examples: Optional[bool] = Field(False, description="Inclure des exemples d'utilisation")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")
    focus_on: Optional[str] = Field(None, description="Éléments à documenter (functions, classes, modules, all)")


class DocumentationResponse(BaseModel):
    """Modèle de réponse pour la génération de documentation."""
    documentation: str = Field(..., description="Documentation générée")
    language: str = Field(..., description="Langage du code documenté")
    format: str = Field(..., description="Format de la documentation")
    documented_elements: List[Dict[str, str]] = Field(..., description="Éléments documentés (fonctions, classes, etc.)")
    coverage: float = Field(..., description="Pourcentage du code couvert par la documentation")
    suggestions: Optional[List[str]] = Field(None, description="Suggestions d'amélioration de la documentation")


class DocumentationTool(BaseTool):
    """Outil de génération automatique de documentation."""

    def get_name(self) -> str:
        return "code_documentation"

    def get_description(self) -> str:
        return "Génère automatiquement de la documentation pour le code dans différents formats"

    def get_request_model(self) -> Type[BaseModel]:
        return DocumentationRequest

    def get_response_model(self) -> Type[BaseModel]:
        return DocumentationResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "javascript", "typescript", "java", "c#", "go", "rust", "php"]

    def is_long_running(self) -> bool:
        """Cet outil génère de la documentation complète via LLM et peut prendre du temps."""
        return True

    def get_supported_formats(self) -> List[str]:
        return ["markdown", "rst", "html", "docstring", "json"]

    def get_supported_styles(self) -> List[str]:
        return ["standard", "detailed", "minimal", "api", "tutorial"]

    def get_usage_description(self) -> str:
        return ("Outil de génération automatique de documentation qui analyse le code et génère une documentation "
                "complète dans différents formats (Markdown, RST, HTML, docstring, JSON) et styles. Il peut "
                "documenter des fonctions, classes, modules avec des exemples d'utilisation et calcule la "
                "couverture documentaire.")

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Documentation Python standard",
                "description": "Générer une documentation Markdown standard pour une classe Python",
                "request": {
                    "code": "class Calculator:\n    def __init__(self):\n        self.result = 0\n    \n    def add(self, x, y):\n        return x + y\n    \n    def multiply(self, x, y):\n        return x * y",
                    "language": "python",
                    "doc_format": "markdown",
                    "doc_style": "standard",
                    "include_examples": True
                },
                "expected_response": "Documentation Markdown complète avec descriptions des méthodes et exemples"
            },
            {
                "title": "Documentation API JavaScript",
                "description": "Générer une documentation API pour des fonctions JavaScript",
                "request": {
                    "code": "function fetchUserData(userId) {\n    return fetch(`/api/users/${userId}`)\n        .then(response => response.json());\n}\n\nfunction createUser(userData) {\n    return fetch('/api/users', {\n        method: 'POST',\n        body: JSON.stringify(userData)\n    });\n}",
                    "language": "javascript",
                    "doc_format": "html",
                    "doc_style": "api",
                    "focus_on": "functions"
                },
                "expected_response": "Documentation HTML style API avec signatures et descriptions des fonctions"
            },
            {
                "title": "Documentation TypeScript détaillée",
                "description": "Générer une documentation détaillée avec types TypeScript",
                "request": {
                    "code": "interface User {\n    id: number;\n    name: string;\n    email: string;\n}\n\nclass UserService {\n    private users: User[] = [];\n    \n    public addUser(user: User): void {\n        this.users.push(user);\n    }\n    \n    public getUser(id: number): User | undefined {\n        return this.users.find(u => u.id === id);\n    }\n}",
                    "language": "typescript",
                    "doc_format": "rst",
                    "doc_style": "detailed",
                    "include_examples": True
                },
                "expected_response": "Documentation RST détaillée avec types, interfaces et exemples d'utilisation"
            },
            {
                "title": "Documentation docstring Python",
                "description": "Générer des docstrings pour des fonctions Python existantes",
                "request": {
                    "code": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left <= right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1",
                    "language": "python",
                    "doc_format": "docstring",
                    "doc_style": "standard",
                    "focus_on": "functions"
                },
                "expected_response": "Code avec docstrings ajoutées directement dans les fonctions"
            },
            {
                "title": "Documentation minimale rapide",
                "description": "Générer une documentation minimale pour un module complet",
                "request": {
                    "code": "export class EventEmitter {\n    constructor() {\n        this.events = {};\n    }\n    \n    on(event, listener) {\n        if (!this.events[event]) {\n            this.events[event] = [];\n        }\n        this.events[event].push(listener);\n    }\n    \n    emit(event, ...args) {\n        if (this.events[event]) {\n            this.events[event].forEach(listener => listener(...args));\n        }\n    }\n}",
                    "language": "javascript",
                    "doc_format": "markdown",
                    "doc_style": "minimal",
                    "focus_on": "all"
                },
                "expected_response": "Documentation minimale couvrant l'ensemble du module"
            },
            {
                "title": "Documentation tutorial avec exemples",
                "description": "Créer une documentation de type tutoriel avec exemples pratiques",
                "request": {
                    "code": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\n\ndef fibonacci_iterative(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a",
                    "language": "python",
                    "doc_format": "markdown",
                    "doc_style": "tutorial",
                    "include_examples": True
                },
                "expected_response": "Documentation tutorial avec explications détaillées et exemples d'utilisation"
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Génération de documentation dans 8+ langages de programmation",
            "5 formats de sortie : Markdown, RST, HTML, docstring, JSON",
            "5 styles de documentation : standard, detailed, minimal, api, tutorial",
            "Analyse automatique des éléments de code (fonctions, classes, modules)",
            "Calcul de la couverture documentaire en pourcentage",
            "Inclusion d'exemples d'utilisation personnalisés",
            "Focus sélectif sur des éléments spécifiques (fonctions, classes, modules)",
            "Génération de suggestions d'amélioration de la documentation",
            "Support des types et interfaces TypeScript",
            "Documentation des paramètres et valeurs de retour",
            "Génération de liens croisés entre les éléments",
            "Extraction automatique des commentaires existants",
            "Respect des conventions de documentation par langage",
            "Génération de table des matières automatique",
            "Support des annotations et décorateurs",
            "Adaptation du style selon le format de sortie"
        ]

    def get_documentation_style_descriptions(self) -> Dict[str, str]:
        return {
            "standard": "Documentation complète avec descriptions, paramètres, retours et exemples basiques",
            "detailed": "Documentation très détaillée avec explications approfondies, cas d'usage et exemples avancés",
            "minimal": "Documentation concise avec informations essentielles seulement",
            "api": "Documentation technique orientée API avec signatures, types et codes d'erreur",
            "tutorial": "Documentation pédagogique avec explications pas-à-pas et exemples pratiques"
        }

    def get_format_descriptions(self) -> Dict[str, str]:
        """Descriptions des formats de documentation supportés."""
        return {
            "markdown": "Format Markdown (.md) idéal pour GitHub, wikis et documentation web",
            "rst": "reStructuredText (.rst) utilisé par Sphinx et la documentation Python",
            "html": "HTML complet avec CSS pour documentation web interactive",
            "docstring": "Docstrings insérées directement dans le code source",
            "json": "Format JSON structuré pour intégration avec d'autres outils"
        }

    def validate_request(self, request: BaseModel) -> bool:
        """Validation étendue pour les requêtes de documentation."""
        # Validation de base
        super().validate_request(request)

        # Validation du format
        if hasattr(request, 'doc_format') and request.doc_format:
            supported_formats = self.get_supported_formats()
            if request.doc_format not in supported_formats:
                raise ToolError(
                    f"Format '{request.doc_format}' non supporté. "
                    f"Formats supportés: {supported_formats}"
                )

        # Validation du style
        if hasattr(request, 'doc_style') and request.doc_style:
            supported_styles = self.get_supported_styles()
            if request.doc_style not in supported_styles:
                raise ToolError(
                    f"Style '{request.doc_style}' non supporté. "
                    f"Styles supportés: {supported_styles}"
                )

        return True

    def _execute_core_logic(self, request: DocumentationRequest, **kwargs) -> DocumentationResponse:
        """
        Exécute la logique principale de génération de documentation.

        Args:
            request: Requête de documentation validée
            **kwargs: Services additionnels (llm_manager, parser, etc.)

        Returns:
            DocumentationResponse: La documentation générée
        """
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')

        code_elements = self._analyze_code_elements(request.code, request.language, parser)

        if llm_manager is not None:
            try:
                context = {
                    "code": request.code,
                    "language": request.language,
                    "doc_style": request.doc_style or "standard",
                    "doc_format": request.doc_format or "markdown",
                    "include_examples": str(request.include_examples),
                    "focus_on": request.focus_on or "all",
                    "file_path": request.file_path or "unknown",
                    "code_elements": str(code_elements[:5]) if code_elements else "[]"  # Limiter pour éviter un prompt trop long
                }
                
                try:
                    if asyncio.iscoroutinefunction(self.prepare_prompt):
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                future = executor.submit(asyncio.run, self.prepare_prompt(request, context=context))
                                prompt = future.result()
                        else:
                            prompt = loop.run_until_complete(self.prepare_prompt(request, context=context))
                    else:
                        prompt = self.prepare_prompt(request, context=context)
                except Exception as e:
                    self.logger.debug(f"Fallback vers _build_documentation_prompt: {e}")
                    prompt = self._build_documentation_prompt(request, code_elements)
                
                generated_docs = llm_manager.sync_generate(prompt)

                # Post-traitement de la documentation
                formatted_docs = self._format_documentation(generated_docs, request.doc_format, request.language)

                coverage = self._calculate_coverage(code_elements, formatted_docs)
                suggestions = self._generate_documentation_suggestions(request, code_elements, coverage)

                return DocumentationResponse(
                    documentation=formatted_docs,
                    language=request.language,
                    format=request.doc_format or "markdown",
                    documented_elements=code_elements,
                    coverage=coverage,
                    suggestions=suggestions
                )

            except Exception as e:
                self.logger.warning(f"Erreur avec LLM, utilisation du fallback: {e}")
                return self._generate_fallback_documentation(request, code_elements, parser)
        else:
            return self._generate_fallback_documentation(request, code_elements, parser)

    async def _execute_core_logic_async(self, request: DocumentationRequest, **kwargs) -> DocumentationResponse:
        """
        Version async de la logique de génération de documentation (FastMCP 2.14+).
        
        Utilise ctx.sample() pour les appels LLM avec fallback vers ToolLLMManager.
        
        Args:
            request: Requête de documentation validée
            **kwargs: Services additionnels incluant ctx, llm_manager, parser
        
        Returns:
            DocumentationResponse: La documentation générée
        """
        ctx = kwargs.get('ctx')
        llm_manager = kwargs.get('llm_manager')
        parser = kwargs.get('parser')
        
        if ctx:
            await ctx.info("Analyse du code...")
        
        # Analyse du code pour identifier les éléments à documenter
        code_elements = self._analyze_code_elements(request.code, request.language, parser)
        
        # Construire le prompt
        prompt = self._build_documentation_prompt(request, code_elements)
        system_prompt = f"""Tu es un expert en documentation de code {request.language}.
Génère une documentation claire, complète et bien structurée au format {request.doc_format or 'markdown'}.
Style de documentation: {request.doc_style or 'standard'}."""
        
        if ctx:
            await ctx.info("Génération de la documentation via LLM...")
        
        try:
            # Utiliser sample_llm (ctx.sample() prioritaire, fallback vers llm_manager)
            generated_docs = await self.sample_llm(
                prompt=prompt,
                ctx=ctx,
                llm_manager=llm_manager,
                system_prompt=system_prompt,
                temperature=0.5
            )
            
            if ctx:
                await ctx.info("Documentation générée, formatage...")
            
            # Post-traitement de la documentation
            formatted_docs = self._format_documentation(generated_docs, request.doc_format, request.language)
            
            # Calcul de la couverture
            coverage = self._calculate_coverage(code_elements, formatted_docs)
            
            # Génération de suggestions
            suggestions = self._generate_documentation_suggestions(request, code_elements, coverage)
            
            return DocumentationResponse(
                documentation=formatted_docs,
                language=request.language,
                format=request.doc_format or "markdown",
                documented_elements=code_elements,
                coverage=coverage,
                suggestions=suggestions
            )
            
        except Exception as e:
            self.logger.warning(f"Erreur LLM async, utilisation du fallback: {e}")
            return self._generate_fallback_documentation(request, code_elements, parser)

    def _analyze_code_elements(self, code: str, language: str, parser=None) -> List[Dict[str, str]]:
        """
        Analyse le code pour identifier les éléments à documenter.

        Returns:
            Liste des éléments trouvés avec leurs métadonnées
        """
        elements = []

        # Utilisation du parser si disponible
        if parser and hasattr(parser, f'parse_{language.lower()}'):
            try:
                parse_method = getattr(parser, f'parse_{language.lower()}')
                parsed = parse_method(code)

                # Extraction des fonctions
                if 'functions' in parsed:
                    for func in parsed['functions']:
                        elements.append({
                            "type": "function",
                            "name": func.get('name', 'unnamed'),
                            "description": func.get('docstring', ''),
                            "parameters": str(func.get('params', [])),
                            "line_number": str(func.get('line_number', 0)),
                            "complexity": self._estimate_complexity(func)
                        })

                # Extraction des classes
                if 'classes' in parsed:
                    for cls in parsed['classes']:
                        elements.append({
                            "type": "class",
                            "name": cls.get('name', 'unnamed'),
                            "description": cls.get('docstring', ''),
                            "methods": str(len(cls.get('methods', []))),
                            "line_number": str(cls.get('line_number', 0)),
                            "inheritance": str(cls.get('bases', []))
                        })

                return elements
            except Exception as e:
                self.logger.debug(f"Erreur parsing avec parser: {e}")

        # Analyse basique sans parser
        return self._basic_element_analysis(code, language)

    def _basic_element_analysis(self, code: str, language: str) -> List[Dict[str, str]]:
        """Analyse basique des éléments du code."""
        elements = []
        lines = code.split('\n')

        if language.lower() == 'python':
            for i, line in enumerate(lines):
                line_stripped = line.strip()

                # Fonctions
                if line_stripped.startswith("def "):
                    func_signature = line_stripped
                    func_name = func_signature.split("def ")[1].split("(")[0].strip()

                    # Recherche de docstring
                    docstring = ""
                    if i + 1 < len(lines) and '"""' in lines[i + 1]:
                        docstring = "Docstring présente"

                    elements.append({
                        "type": "function",
                        "name": func_name,
                        "description": docstring,
                        "signature": func_signature,
                        "line_number": str(i + 1),
                        "complexity": "medium"
                    })

                # Classes
                elif line_stripped.startswith("class "):
                    class_signature = line_stripped
                    class_name = class_signature.split("class ")[1].split("(")[0].split(":")[0].strip()

                    elements.append({
                        "type": "class",
                        "name": class_name,
                        "description": "",
                        "signature": class_signature,
                        "line_number": str(i + 1),
                        "complexity": "medium"
                    })

        elif language.lower() in ['javascript', 'typescript']:
            for i, line in enumerate(lines):
                line_stripped = line.strip()

                # Fonctions
                if 'function ' in line_stripped or '=>' in line_stripped:
                    func_name = "anonymous"
                    if line_stripped.startswith('function '):
                        func_name = line_stripped.split('function ')[1].split('(')[0].strip()
                    elif 'const ' in line_stripped and '=>' in line_stripped:
                        func_name = line_stripped.split('const ')[1].split(' =')[0].strip()

                    elements.append({
                        "type": "function",
                        "name": func_name,
                        "description": "",
                        "signature": line_stripped,
                        "line_number": str(i + 1),
                        "complexity": "medium"
                    })

                # Classes
                elif line_stripped.startswith('class '):
                    class_name = line_stripped.split('class ')[1].split(' ')[0].split('{')[0].strip()

                    elements.append({
                        "type": "class",
                        "name": class_name,
                        "description": "",
                        "signature": line_stripped,
                        "line_number": str(i + 1),
                        "complexity": "medium"
                    })

        return elements

    def _estimate_complexity(self, element: Dict[str, Any]) -> str:
        """Estime la complexité d'un élément de code."""
        # Estimation basique basée sur le nombre de paramètres et la longueur
        params_count = len(element.get('params', []))

        if params_count <= 2:
            return "low"
        elif params_count <= 5:
            return "medium"
        else:
            return "high"

    def _build_documentation_prompt(self, request: DocumentationRequest, elements: List[Dict[str, str]]) -> str:
        """
        Construit le prompt pour la génération de documentation.

        Args:
            request: Requête de documentation
            elements: Éléments de code identifiés

        Returns:
            Prompt optimisé
        """
        style_instructions = {
            "standard": "Génère une documentation claire et concise avec descriptions, paramètres et valeurs de retour",
            "detailed": "Génère une documentation très détaillée avec exemples, cas d'usage et notes techniques",
            "minimal": "Génère une documentation minimale avec seulement les informations essentielles",
            "api": "Génère une documentation de style API avec format standardisé pour chaque fonction/classe",
            "tutorial": "Génère une documentation de style tutoriel avec explications pédagogiques"
        }

        format_instructions = {
            "markdown": "Utilise le format Markdown avec en-têtes appropriés",
            "rst": "Utilise le format reStructuredText",
            "html": "Génère du HTML bien formaté",
            "docstring": "Génère des docstrings dans le style du langage",
            "json": "Retourne la documentation structurée en JSON"
        }

        prompt_parts = [
            f"Génère une documentation pour le code {request.language} suivant :",
            f"Style: {style_instructions.get(request.doc_style, style_instructions['standard'])}",
            f"Format: {format_instructions.get(request.doc_format, format_instructions['markdown'])}",
            ""
        ]

        # Ajout du code
        prompt_parts.extend([
            f"```{request.language}",
            request.code,
            "```",
            ""
        ])

        # Éléments identifiés
        if elements:
            prompt_parts.append("Éléments identifiés à documenter :")
            for element in elements[:10]:  # Limiter à 10 éléments pour le prompt
                prompt_parts.append(f"- {element['type']}: {element['name']} (ligne {element['line_number']})")
            prompt_parts.append("")

        # Focus spécifique
        if request.focus_on and request.focus_on != "all":
            prompt_parts.append(f"Concentre-toi sur les {request.focus_on}")

        # Exemples d'utilisation
        if request.include_examples:
            prompt_parts.append("Inclus des exemples d'utilisation pratiques pour chaque élément principal.")

        # Instructions par langage
        language_instructions = self._get_language_doc_instructions(request.language)
        if language_instructions:
            prompt_parts.append(f"Instructions {request.language}: {language_instructions}")

        return "\n".join(prompt_parts)

    def _get_language_doc_instructions(self, language: str) -> str:
        """Instructions spécifiques par langage pour la documentation."""
        instructions = {
            "python": "Utilise les conventions PEP 257 pour les docstrings, inclus les types avec les paramètres",
            "javascript": "Utilise JSDoc format avec @param, @returns, @example",
            "typescript": "Inclus les types TypeScript dans la documentation, utilise @param avec types",
            "java": "Utilise Javadoc format avec @param, @return, @throws",
            "c#": "Utilise XML documentation format avec <summary>, <param>, <returns>",
            "go": "Utilise les conventions Go avec commentaires au-dessus des déclarations",
            "rust": "Utilise les doc comments avec /// et inclus les exemples avec ```"
        }
        return instructions.get(language.lower(), "")

    def _format_documentation(self, docs: str, format_type: str, language: str) -> str:
        """Formate la documentation selon le type demandé."""
        if format_type == "docstring":
            return self._convert_to_docstring_format(docs, language)
        elif format_type == "html":
            return self._convert_to_html_format(docs)
        elif format_type == "rst":
            return self._convert_to_rst_format(docs)
        # Pour markdown et json, on retourne tel quel (assumant que le LLM a généré dans le bon format)
        return docs

    def _convert_to_docstring_format(self, docs: str, language: str) -> str:
        """Convertit la documentation en format docstring selon le langage."""
        if language.lower() == "python":
            # Conversion basique en docstring Python
            lines = docs.split('\n')
            formatted_lines = ['"""']
            formatted_lines.extend(lines)
            formatted_lines.append('"""')
            return '\n'.join(formatted_lines)
        elif language.lower() in ["javascript", "typescript"]:
            # Conversion en JSDoc
            lines = docs.split('\n')
            formatted_lines = ['/**']
            for line in lines:
                formatted_lines.append(f' * {line}')
            formatted_lines.append(' */')
            return '\n'.join(formatted_lines)
        return docs

    def _convert_to_html_format(self, docs: str) -> str:
        """Conversion basique en HTML."""
        # Conversion très basique Markdown vers HTML
        html_docs = docs.replace('# ', '<h1>').replace('\n# ', '</h1>\n<h1>')
        html_docs = html_docs.replace('## ', '<h2>').replace('\n## ', '</h2>\n<h2>')
        html_docs = html_docs.replace('### ', '<h3>').replace('\n### ', '</h3>\n<h3>')
        html_docs = html_docs.replace('\n\n', '</p>\n<p>')
        return f'<div class="documentation">\n<p>{html_docs}</p>\n</div>'

    def _convert_to_rst_format(self, docs: str) -> str:
        """Conversion basique en reStructuredText."""
        # Conversion très basique Markdown vers RST
        rst_docs = docs.replace('# ', '').replace('## ', '').replace('### ', '')
        # Ajouter des délimiteurs RST basiques
        lines = rst_docs.split('\n')
        formatted_lines = []
        for line in lines:
            if line and not line.startswith(' '):
                formatted_lines.append(line)
                formatted_lines.append('=' * len(line))
            else:
                formatted_lines.append(line)
        return '\n'.join(formatted_lines)

    def _calculate_coverage(self, elements: List[Dict[str, str]], documentation: str) -> float:
        """Calcule le pourcentage de couverture de la documentation."""
        if not elements:
            return 100.0

        documented_count = 0
        for element in elements:
            element_name = element.get('name', '')
            if element_name and element_name in documentation:
                documented_count += 1

        return (documented_count / len(elements)) * 100.0

    def _generate_documentation_suggestions(self, request: DocumentationRequest,
                                          elements: List[Dict[str, str]], coverage: float) -> List[str]:
        """Génère des suggestions d'amélioration de la documentation."""
        suggestions = []

        # Suggestions basées sur la couverture
        if coverage < 80:
            suggestions.append(f"Couverture documentation faible ({coverage:.1f}%). Documenter les éléments manquants.")

        # Suggestions par type d'élément
        functions_without_docs = [e for e in elements if e['type'] == 'function' and not e.get('description')]
        if functions_without_docs:
            suggestions.append(f"{len(functions_without_docs)} fonction(s) sans documentation détectée(s).")

        # Suggestions par format
        if request.doc_format == "docstring":
            suggestions.append("Intégrer les docstrings directement dans le code source.")

        # Suggestions par style
        if request.doc_style == "api":
            suggestions.append("Considérer l'ajout d'exemples de requêtes/réponses pour une API complète.")

        # Suggestions génériques
        if not request.include_examples:
            suggestions.append("Ajouter des exemples d'utilisation pour améliorer la compréhension.")

        return suggestions[:5]  # Limiter à 5 suggestions

    def _generate_fallback_documentation(self, request: DocumentationRequest,
                                       elements: List[Dict[str, str]], parser=None) -> DocumentationResponse:
        """Génère une documentation basique sans LLM."""

        # Génération de documentation basique
        doc_parts = [
            f"# Documentation - {request.language.title()}",
            "",
            "## Vue d'ensemble",
            f"Ce code {request.language} contient {len(elements)} élément(s) principal(aux).",
            ""
        ]

        # Documentation des fonctions
        functions = [e for e in elements if e['type'] == 'function']
        if functions:
            doc_parts.extend([
                "## Fonctions",
                ""
            ])
            for func in functions:
                doc_parts.extend([
                    f"### {func['name']}",
                    f"- **Ligne:** {func['line_number']}",
                    f"- **Complexité:** {func.get('complexity', 'inconnue')}",
                    f"- **Description:** {func.get('description') or 'À documenter'}",
                    ""
                ])

        # Documentation des classes
        classes = [e for e in elements if e['type'] == 'class']
        if classes:
            doc_parts.extend([
                "## Classes",
                ""
            ])
            for cls in classes:
                doc_parts.extend([
                    f"### {cls['name']}",
                    f"- **Ligne:** {cls['line_number']}",
                    f"- **Description:** {cls.get('description') or 'À documenter'}",
                    ""
                ])

        documentation = "\n".join(doc_parts)

        # Formatage selon le type demandé
        formatted_docs = self._format_documentation(documentation, request.doc_format or "markdown", request.language)

        # Calcul de la couverture
        coverage = self._calculate_coverage(elements, formatted_docs)

        # Suggestions
        suggestions = [
            "Documentation générée automatiquement. Recommandation: utiliser un LLM pour une documentation plus riche.",
            "Ajouter des descriptions détaillées pour chaque élément.",
            "Inclure des exemples d'utilisation."
        ]

        return DocumentationResponse(
            documentation=formatted_docs,
            language=request.language,
            format=request.doc_format or "markdown",
            documented_elements=elements,
            coverage=coverage,
            suggestions=suggestions
        )


# Fonction de compatibilité pour l'ancien système
def generate_documentation(request: DocumentationRequest, parser=None, llm_manager=None) -> DocumentationResponse:
    """
    Fonction de compatibilité avec l'ancien système.

    Args:
        request: Requête de documentation
        parser: Parser pour l'analyse
        llm_manager: Service LLM

    Returns:
        Réponse de documentation
    """
    tool = DocumentationTool()
    return tool.execute(request, parser=parser, llm_manager=llm_manager)
