"""
Tests unitaires pour le CodeParser
"""
import sys
import os
import unittest
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.core.parser import CodeParser

class TestCodeParser(unittest.TestCase):
    """Tests unitaires pour la classe CodeParser"""
    
    def setUp(self):
        """Initialisation avant chaque test"""
        self.parser = CodeParser()
    
    def test_detect_language(self):
        python_code = """
def hello(name):
    return f'Hello, {name}!'
        
class Person:
    def __init__(self, name):
        self.name = name
        """
        self.assertEqual(self.parser._detect_language(python_code), "python")
        
        js_code = """
function hello(name) {
    return `Hello, ${name}!`;
}

class Person {
    constructor(name) {
        this.name = name;
    }
}
        """
        self.assertEqual(self.parser._detect_language(js_code), "javascript")
        
        ambiguous_code = """
// This is a comment
x = 10
        """
        self.assertIn(self.parser._detect_language(ambiguous_code), ["python", "javascript", "unknown"])
    
    def test_parse_python(self):
        python_code = """
import os
from pathlib import Path

def hello(name):
    return f'Hello, {name}!'
        
class Person:
    def __init__(self, name):
        self.name = name
        """
        
        result = self.parser.parse(python_code, "python")
        
        self.assertEqual(result["language"], "python")
        self.assertTrue(result["ast_valid"])
        
        self.assertEqual(len(result["imports"]), 2)
        import_names = [imp["name"] for imp in result["imports"]]
        self.assertIn("os", import_names)
        
        from_imports = [imp for imp in result["imports"] if imp["type"] == "from_import"]
        self.assertTrue(any(imp["module"] == "pathlib" for imp in from_imports))
        
        self.assertEqual(len(result["functions"]), 2)
        function_names = [func["name"] for func in result["functions"]]
        self.assertIn("hello", function_names)
        self.assertIn("__init__", function_names)
        
        self.assertEqual(len(result["classes"]), 1)
        self.assertEqual(result["classes"][0]["name"], "Person")
    
    def test_parse_javascript(self):
        js_code = """
import { useState } from 'react';
const axios = require('axios');

function hello(name) {
    return `Hello, ${name}!`;
}

class Person {
    constructor(name) {
        this.name = name;
    }
    
    greet() {
        return `Hi, I'm ${this.name}`;
    }
}

const multiply = (a, b) => a * b;
        """
        
        result = self.parser.parse(js_code, "javascript")
        
        self.assertEqual(result["language"], "javascript")
        self.assertTrue(result["syntax_valid"])
        
        self.assertEqual(len(result["imports"]), 2)
        import_types = [imp["type"] for imp in result["imports"]]
        self.assertIn("es6_import", import_types)
        self.assertIn("commonjs_require", import_types)
        
        function_names = [func["name"] for func in result["functions"]]
        self.assertIn("hello", function_names)
        self.assertIn("multiply", function_names)
        self.assertIn("greet", function_names)
        
        self.assertEqual(len(result["classes"]), 1)
        self.assertEqual(result["classes"][0]["name"], "Person")
        
        variable_names = [var["name"] for var in result["variables"]]
        self.assertIn("axios", variable_names)
        self.assertIn("multiply", variable_names)
    
    def test_invalid_python_code(self):
        invalid_code = """
def broken_function(
    print("Missing closing parenthesis")
        """
        
        result = self.parser.parse(invalid_code, "python")
        
        self.assertEqual(result["language"], "python")
        self.assertFalse(result["ast_valid"])
        self.assertIn("error", result)
    
    def test_unsupported_language(self):
        code = "int main() { return 0; }"
        result = self.parser.parse(code, "c")
        
        self.assertIn("error", result)
        self.assertIn("non supporté", result["error"])

if __name__ == "__main__":
    unittest.main()
