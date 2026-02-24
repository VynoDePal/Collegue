"""
Moteur d'analyse et de génération de documentation.

Contient la logique métier pure : analyse d'éléments de code, formatage,
calcul de couverture, génération de suggestions.
"""
from typing import List, Dict, Any, Optional
from .config import STYLE_INSTRUCTIONS, FORMAT_INSTRUCTIONS, LANGUAGE_INSTRUCTIONS


class DocumentationEngine:
    """Moteur d'analyse et de génération de documentation."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def analyze_code_elements(self, code: str, language: str, parser=None) -> List[Dict[str, str]]:
        """Analyse le code pour identifier les éléments à documenter."""
        elements = []
        
        if parser and hasattr(parser, f'parse_{language.lower()}'):
            try:
                parse_method = getattr(parser, f'parse_{language.lower()}')
                parsed = parse_method(code)
                
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
                if self.logger:
                    self.logger.debug(f"Erreur parsing avec parser: {e}")
        
        return self._basic_element_analysis(code, language)
    
    def _basic_element_analysis(self, code: str, language: str) -> List[Dict[str, str]]:
        """Analyse basique des éléments sans parser externe."""
        elements = []
        lines = code.split('\n')
        
        if language.lower() == 'python':
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                
                if line_stripped.startswith("def "):
                    func_signature = line_stripped
                    func_name = func_signature.split("def ")[1].split("(")[0].strip()
                    
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
        
        elif language.lower() == 'php':
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                
                if 'function ' in line_stripped:
                    func_name = "anonymous"
                    if 'function ' in line_stripped and '(' in line_stripped:
                        parts = line_stripped.split('function ')[1].split('(')
                        if parts[0].strip():
                            func_name = parts[0].strip()
                    
                    elements.append({
                        "type": "function",
                        "name": func_name,
                        "description": "",
                        "signature": line_stripped,
                        "line_number": str(i + 1),
                        "complexity": "medium"
                    })
                
                elif (line_stripped.startswith('class ') or 
                      line_stripped.startswith('abstract class ') or 
                      line_stripped.startswith('interface ') or 
                      line_stripped.startswith('trait ')):
                    parts = line_stripped.split()
                    name_idx = 1
                    if parts[0] == 'abstract':
                        name_idx = 2
                    
                    if len(parts) > name_idx:
                        name = parts[name_idx]
                        
                        elements.append({
                            "type": "class",
                            "name": name,
                            "description": "",
                            "signature": line_stripped,
                            "line_number": str(i + 1),
                            "complexity": "medium"
                        })
        
        return elements
    
    def _estimate_complexity(self, element: Dict[str, Any]) -> str:
        """Estime la complexité d'un élément."""
        params_count = len(element.get('params', []))
        
        if params_count <= 2:
            return "low"
        elif params_count <= 5:
            return "medium"
        else:
            return "high"
    
    def build_prompt(self, code: str, language: str, style: str, format_type: str,
                    include_examples: bool, focus_on: str, elements: List[Dict[str, str]]) -> str:
        """Construit le prompt pour le LLM."""
        prompt_parts = [
            f"Génère une documentation pour le code {language} suivant",
            f"",
            f"Style: {STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS['standard'])}",
            f"Format: {FORMAT_INSTRUCTIONS.get(format_type, FORMAT_INSTRUCTIONS['markdown'])}",
            f""
        ]
        
        prompt_parts.extend([
            f"```{language}",
            code,
            f"```",
            f""
        ])
        
        if elements:
            prompt_parts.append("Éléments identifiés à documenter :")
            for element in elements[:10]:
                prompt_parts.append(f"- {element['type']}: {element['name']} (ligne {element['line_number']})")
            prompt_parts.append("")
        
        if focus_on != "all":
            prompt_parts.append(f"Concentre-toi sur les {focus_on}")
            prompt_parts.append("")
        
        if include_examples:
            prompt_parts.append("Inclus des exemples d'utilisation pratiques pour chaque élément principal")
            prompt_parts.append("")
        
        lang_instructions = LANGUAGE_INSTRUCTIONS.get(language.lower(), "")
        if lang_instructions:
            prompt_parts.append(f"Instructions {language}: {lang_instructions}")
        
        return "\n".join(prompt_parts)
    
    def format_documentation(self, docs: str, format_type: str, language: str) -> str:
        """Formate la documentation selon le type demandé."""
        if format_type == "docstring":
            return self._convert_to_docstring_format(docs, language)
        elif format_type == "html":
            return self._convert_to_html_format(docs)
        elif format_type == "rst":
            return self._convert_to_rst_format(docs)
        
        return docs
    
    def _convert_to_docstring_format(self, docs: str, language: str) -> str:
        """Convertit la documentation en format docstring."""
        if language.lower() == "python":
            lines = docs.split('\n')
            formatted_lines = ['"""']
            formatted_lines.extend(lines)
            formatted_lines.append('"""')
            return '\n'.join(formatted_lines)
        elif language.lower() in ["javascript", "typescript", "java", "php", "c#"]:
            lines = docs.split('\n')
            formatted_lines = ['/**']
            for line in lines:
                formatted_lines.append(f' * {line}')
            formatted_lines.append(' */')
            return '\n'.join(formatted_lines)
        return docs
    
    def _convert_to_html_format(self, docs: str) -> str:
        """Convertit la documentation en format HTML."""
        html_docs = docs.replace('# ', '<h1>').replace('\n# ', '</h1>\n<h1>')
        html_docs = html_docs.replace('## ', '<h2>').replace('\n## ', '</h2>\n<h2>')
        html_docs = html_docs.replace('### ', '<h3>').replace('\n### ', '</h3>\n<h3>')
        html_docs = html_docs.replace('\n\n', '</p>\n<p>')
        return f'<div class="documentation">\n<p>{html_docs}</p>\n</div>'
    
    def _convert_to_rst_format(self, docs: str) -> str:
        """Convertit la documentation en format RST."""
        rst_docs = docs.replace('# ', '').replace('## ', '').replace('### ', '')
        
        lines = rst_docs.split('\n')
        formatted_lines = []
        for line in lines:
            if line and not line.startswith(' '):
                formatted_lines.append(line)
                formatted_lines.append('=' * len(line))
            else:
                formatted_lines.append(line)
        return '\n'.join(formatted_lines)
    
    def calculate_coverage(self, elements: List[Dict[str, str]], documentation: str) -> float:
        """Calcule le pourcentage de couverture documentaire."""
        if not elements:
            return 100.0
        
        documented_count = 0
        for element in elements:
            element_name = element.get('name', '')
            if element_name and element_name in documentation:
                documented_count += 1
        
        return (documented_count / len(elements)) * 100.0
    
    def generate_suggestions(self, elements: List[Dict[str, str]], coverage: float,
                            doc_format: str, doc_style: str, include_examples: bool) -> List[str]:
        """Génère des suggestions d'amélioration de la documentation."""
        suggestions = []
        
        if coverage < 80:
            suggestions.append(f"Couverture documentation faible ({coverage:.1f}%). Documenter les éléments manquants.")
        
        functions_without_docs = [e for e in elements if e['type'] == 'function' and not e.get('description')]
        if functions_without_docs:
            suggestions.append(f"{len(functions_without_docs)} fonction(s) sans documentation détectée(s).")
        
        if doc_format == "docstring":
            suggestions.append("Intégrer les docstrings directement dans le code source.")
        
        if doc_style == "api":
            suggestions.append("Considérer l'ajout d'exemples de requêtes/réponses pour une API complète.")
        
        if not include_examples:
            suggestions.append("Ajouter des exemples d'utilisation pour améliorer la compréhension.")
        
        return suggestions[:5]
    
    def generate_fallback_documentation(self, code: str, language: str,
                                       elements: List[Dict[str, str]], doc_format: str) -> str:
        """Génère une documentation fallback quand le LLM n'est pas disponible."""
        doc_parts = [
            f"# Documentation - {language.title()}",
            "",
            "## Vue d'ensemble",
            f"Ce code {language} contient {len(elements)} élément(s) principal(aux).",
            ""
        ]
        
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
        return self.format_documentation(documentation, doc_format or "markdown", language)
