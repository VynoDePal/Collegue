"""
Frameworks JavaScript - Ressources pour les frameworks JavaScript populaires
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

import json

class JavaScriptFrameworkReference(BaseModel):
    """Modèle pour une référence de framework JavaScript."""
    name: str
    description: str
    version: Optional[str] = None
    website: Optional[str] = None
    documentation: Optional[str] = None
    github: Optional[str] = None
    categories: List[str] = []
    features: List[str] = []
    examples: List[Dict[str, str]] = []

JS_FRAMEWORKS = {
    # Frontend Frameworks
    "react": {
        "name": "React",
        "description": "Bibliothèque JavaScript pour construire des interfaces utilisateur",
        "version": "18.2.0",
        "website": "https://reactjs.org/",
        "documentation": "https://react.dev/reference/react",
        "github": "https://github.com/facebook/react",
        "categories": ["frontend", "ui", "component-based"],
        "features": ["Virtual DOM", "Component-based", "JSX", "Unidirectional data flow", "React Hooks"],
        "examples": [
            {"title": "Composant fonctionnel", "code": "function Welcome(props) {\n  return <h1>Hello, {props.name}</h1>;\n}"},
            {"title": "Hook d'état", "code": "import React, { useState } from 'react';\n\nfunction Counter() {\n  const [count, setCount] = useState(0);\n  return (\n    <div>\n      <p>You clicked {count} times</p>\n      <button onClick={() => setCount(count + 1)}>Click me</button>\n    </div>\n  );\n}"}
        ]
    },
    "vue": {
        "name": "Vue.js",
        "description": "Framework JavaScript progressif pour construire des interfaces utilisateur",
        "version": "3.3.0",
        "website": "https://vuejs.org/",
        "documentation": "https://vuejs.org/guide/introduction.html",
        "github": "https://github.com/vuejs/core",
        "categories": ["frontend", "ui", "component-based"],
        "features": ["Reactive data binding", "Component system", "Template syntax", "Transitions", "Composition API"],
        "examples": [
            {"title": "Application simple", "code": "const { createApp } = Vue\n\ncreateApp({\n  data() {\n    return {\n      message: 'Hello Vue!'\n    }\n  }\n}).mount('#app')"},
            {"title": "Composant Vue", "code": "Vue.component('button-counter', {\n  data: function() {\n    return {\n      count: 0\n    }\n  },\n  template: '<button v-on:click=\"count++\">You clicked me {{ count }} times.</button>'\n})"}
        ]
    },
    "angular": {
        "name": "Angular",
        "description": "Plateforme pour construire des applications web",
        "version": "16.0.0",
        "website": "https://angular.io/",
        "documentation": "https://angular.io/docs",
        "github": "https://github.com/angular/angular",
        "categories": ["frontend", "ui", "full-framework"],
        "features": ["Two-way data binding", "Dependency injection", "TypeScript integration", "RxJS", "Component-based"],
        "examples": [
            {"title": "Composant", "code": "import { Component } from '@angular/core';\n\n@Component({\n  selector: 'app-hello',\n  template: '<h1>Hello {{name}}!</h1>'\n})\nexport class HelloComponent {\n  name = 'Angular';\n}"},
            {"title": "Service", "code": "import { Injectable } from '@angular/core';\n\n@Injectable({\n  providedIn: 'root'\n})\nexport class DataService {\n  getData() {\n    return ['item1', 'item2', 'item3'];\n  }\n}"}
        ]
    },
    
    # Backend Frameworks
    "express": {
        "name": "Express.js",
        "description": "Framework web minimaliste pour Node.js",
        "version": "4.18.2",
        "website": "https://expressjs.com/",
        "documentation": "https://expressjs.com/en/4x/api.html",
        "github": "https://github.com/expressjs/express",
        "categories": ["backend", "server", "api"],
        "features": ["Routing", "Middleware", "Template engines", "Error handling", "Static file serving"],
        "examples": [
            {"title": "Application simple", "code": "const express = require('express');\nconst app = express();\n\napp.get('/', (req, res) => {\n  res.send('Hello World!');\n});\n\napp.listen(3000, () => {\n  console.log('Server running on port 3000');\n});"},
            {"title": "Middleware", "code": "app.use((req, res, next) => {\n  console.log('Time:', Date.now());\n  next();\n});"}
        ]
    },
    "nest": {
        "name": "NestJS",
        "description": "Framework Node.js progressif pour construire des applications côté serveur",
        "version": "10.0.0",
        "website": "https://nestjs.com/",
        "documentation": "https://docs.nestjs.com/",
        "github": "https://github.com/nestjs/nest",
        "categories": ["backend", "server", "typescript"],
        "features": ["TypeScript support", "Dependency injection", "Decorators", "Modules", "Middleware"],
        "examples": [
            {"title": "Contrôleur", "code": "import { Controller, Get } from '@nestjs/common';\n\n@Controller('cats')\nexport class CatsController {\n  @Get()\n  findAll(): string {\n    return 'This action returns all cats';\n  }\n}"},
            {"title": "Service", "code": "import { Injectable } from '@nestjs/common';\n\n@Injectable()\nexport class CatsService {\n  private readonly cats = [];\n\n  create(cat) {\n    this.cats.push(cat);\n  }\n\n  findAll() {\n    return this.cats;\n  }\n}"}
        ]
    },
    
    # Testing Frameworks
    "jest": {
        "name": "Jest",
        "description": "Framework de test JavaScript avec un focus sur la simplicité",
        "version": "29.5.0",
        "website": "https://jestjs.io/",
        "documentation": "https://jestjs.io/docs/getting-started",
        "github": "https://github.com/facebook/jest",
        "categories": ["testing", "unit-testing", "snapshot-testing"],
        "features": ["Zero config", "Snapshots", "Isolated tests", "Code coverage", "Mocking"],
        "examples": [
            {"title": "Test simple", "code": "test('adds 1 + 2 to equal 3', () => {\n  expect(1 + 2).toBe(3);\n});"},
            {"title": "Mock de fonction", "code": "const mockFn = jest.fn();\nmockFn.mockReturnValue(42);\nexpect(mockFn()).toBe(42);"}
        ]
    },
    "mocha": {
        "name": "Mocha",
        "description": "Framework de test JavaScript riche en fonctionnalités",
        "version": "10.2.0",
        "website": "https://mochajs.org/",
        "documentation": "https://mochajs.org/#getting-started",
        "github": "https://github.com/mochajs/mocha",
        "categories": ["testing", "unit-testing", "bdd"],
        "features": ["Browser support", "Async support", "Test coverage", "Multiple interfaces", "Plugin ecosystem"],
        "examples": [
            {"title": "Test simple", "code": "describe('Array', function() {\n  describe('#indexOf()', function() {\n    it('should return -1 when the value is not present', function() {\n      assert.equal([1, 2, 3].indexOf(4), -1);\n    });\n  });\n});"},
            {"title": "Test asynchrone", "code": "describe('User', function() {\n  describe('#save()', function() {\n    it('should save without error', function(done) {\n      var user = new User('Luna');\n      user.save(function(err) {\n        if (err) done(err);\n        else done();\n      });\n    });\n  });\n});"}
        ]
    }
}

def get_framework_reference(framework_name: str) -> Optional[JavaScriptFrameworkReference]:
    """Récupère les informations d'un framework JavaScript."""
    if framework_name.lower() in JS_FRAMEWORKS:
        return JavaScriptFrameworkReference(**JS_FRAMEWORKS[framework_name.lower()])
    return None

def get_all_frameworks() -> List[str]:
    """Récupère la liste de tous les frameworks disponibles."""
    return list(JS_FRAMEWORKS.keys())

def get_frameworks_by_category(category: str) -> List[str]:
    """Récupère la liste des frameworks d'une catégorie spécifique."""
    return [name for name, data in JS_FRAMEWORKS.items() 
            if category in data.get("categories", [])]

def register_frameworks(app, app_state):
    """Enregistre les ressources des frameworks JavaScript."""
    
    @app.resource("collegue://javascript/frameworks/index")
    def get_js_frameworks_index() -> str:
        """Liste tous les frameworks JavaScript disponibles."""
        return json.dumps(get_all_frameworks())
    
    @app.resource("collegue://javascript/frameworks/category/{category}")
    def get_js_frameworks_by_category_resource(category: str) -> str:
        """Liste les frameworks d'une catégorie spécifique."""
        return json.dumps(get_frameworks_by_category(category))
    
    @app.resource("collegue://javascript/frameworks/{framework_name}")
    def get_js_framework_resource(framework_name: str) -> str:
        """Récupère les informations d'un framework spécifique."""
        framework = get_framework_reference(framework_name)
        if framework:
            return framework.model_dump_json()
        return json.dumps({"error": f"Framework {framework_name} non trouvé"})
