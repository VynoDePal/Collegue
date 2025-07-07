#!/usr/bin/env python3
"""
Test du parser TypeScript
"""
import sys
import os
import json
from pathlib import Path

# Ajouter le répertoire parent au path pour pouvoir importer les modules
sys.path.append(str(Path(__file__).parent.parent))

from collegue.core.parser import CodeParser

def main():
    """Test du parser TypeScript avec un fichier d'exemple"""
    # Chemin vers le fichier de test TypeScript
    ts_file_path = Path(__file__).parent / "typescript_test.ts"
    
    # Vérifier que le fichier existe
    if not ts_file_path.exists():
        print(f"Erreur: Le fichier {ts_file_path} n'existe pas.")
        return
    
    # Lire le contenu du fichier
    with open(ts_file_path, "r", encoding="utf-8") as f:
        ts_code = f.read()
    
    # Créer une instance du parser
    parser = CodeParser()
    
    # Analyser le code TypeScript
    result = parser.parse(ts_code, language="typescript")
    
    # Afficher les résultats
    print("\n=== RÉSULTATS DE L'ANALYSE TYPESCRIPT ===\n")
    
    # Vérifier si l'analyse a réussi
    if "error" in result:
        print(f"Erreur d'analyse: {result['error']}")
        return
    
    # Afficher les statistiques
    print(f"Langage détecté: {result['language']}")
    print(f"Imports: {len(result['imports'])}")
    print(f"Fonctions: {len(result['functions'])}")
    print(f"Classes: {len(result['classes'])}")
    print(f"Interfaces: {len(result['interfaces'])}")
    print(f"Types: {len(result['types'])}")
    print(f"Variables: {len(result['variables'])}")
    
    # Afficher les détails
    print("\n--- IMPORTS ---")
    for imp in result['imports']:
        print(f"  {imp['type']}: {imp['statement']} (ligne {imp['line']})")
    
    print("\n--- INTERFACES ---")
    for interface in result['interfaces']:
        extends_info = f", extends: {', '.join(interface['extends'])}" if 'extends' in interface else ""
        generics_info = f", generics: {interface['generics']}" if 'generics' in interface else ""
        print(f"  {interface['name']}{generics_info}{extends_info} (ligne {interface['line']})")
    
    print("\n--- TYPES ---")
    for type_def in result['types']:
        generics_info = f", generics: {type_def['generics']}" if 'generics' in type_def else ""
        print(f"  {type_def['name']}{generics_info}: {type_def['definition']} (ligne {type_def['line']})")
    
    print("\n--- CLASSES ---")
    for cls in result['classes']:
        extends_info = f", extends: {cls['extends']}" if 'extends' in cls else ""
        implements_info = f", implements: {', '.join(cls['implements'])}" if 'implements' in cls else ""
        generics_info = f", generics: {cls['generics']}" if 'generics' in cls else ""
        print(f"  {cls['name']}{generics_info}{extends_info}{implements_info} (ligne {cls['line']})")
    
    print("\n--- FONCTIONS ---")
    for func in result['functions']:
        return_type = f" -> {func['return_type']}" if 'return_type' in func else ""
        print(f"  {func['name']} ({func['params']}){return_type} (ligne {func['line']}, type: {func['type']})")
    
    print("\n--- VARIABLES ---")
    for var in result['variables']:
        type_info = f": {var['type']}" if 'type' in var else ""
        print(f"  {var['declaration_type']} {var['name']}{type_info} = {var['value']} (ligne {var['line']})")
    
    # Sauvegarder les résultats complets dans un fichier JSON
    result_file = Path(__file__).parent / "typescript_parser_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    
    print(f"\nRésultats complets sauvegardés dans {result_file}")

if __name__ == "__main__":
    main()
