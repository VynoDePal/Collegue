"""
Ressources pour les frameworks TypeScript.

Ce module fournit des informations sur les frameworks et bibliothèques populaires pour TypeScript.
"""
from fastmcp import FastMCP
from typing import Dict, Any, List
import json

FRONTEND_FRAMEWORKS = {
    "Angular": {
        "description": "Framework complet pour applications web développé par Google",
        "version": "17+",
        "site": "https://angular.io/",
        "typescript_integration": "Native",
        "key_features": [
            "Architecture MVC",
            "Injection de dépendances",
            "Composants réutilisables",
            "Routage intégré",
            "Formulaires réactifs",
            "RxJS intégré"
        ],
        "example": """
import { Component } from '@angular/core';

@Component({
  selector: 'app-hello',
  template: '<h1>Hello, {{name}}!</h1>'
})
export class HelloComponent {
  name: string = 'Angular';
}
"""
    },
    "React (avec TypeScript)": {
        "description": "Bibliothèque UI développée par Facebook, utilisable avec TypeScript",
        "version": "18+",
        "site": "https://reactjs.org/",
        "typescript_integration": "Via TSX",
        "key_features": [
            "Composants fonctionnels",
            "Hooks",
            "JSX/TSX",
            "Virtual DOM",
            "Écosystème riche"
        ],
        "example": """
import React, { useState } from 'react';

interface HelloProps {
  initialName: string;
}

const Hello: React.FC<HelloProps> = ({ initialName }) => {
  const [name, setName] = useState<string>(initialName);
  
  return <h1>Hello, {name}!</h1>;
};

export default Hello;
"""
    },
    "Vue (avec TypeScript)": {
        "description": "Framework progressif pour interfaces utilisateur",
        "version": "3+",
        "site": "https://vuejs.org/",
        "typescript_integration": "Via Vue Class Component ou Composition API",
        "key_features": [
            "Système de composants",
            "Réactivité",
            "Directives",
            "Composition API",
            "Single-File Components"
        ],
        "example": """
<script lang="ts">
import { defineComponent } from 'vue';

export default defineComponent({
  props: {
    name: {
      type: String,
      required: true
    }
  },
  setup(props) {
    return { name: props.name };
  }
});
</script>

<template>
  <h1>Hello, {{ name }}!</h1>
</template>
"""
    },
    "Svelte (avec TypeScript)": {
        "description": "Compilateur qui génère du code JavaScript optimisé",
        "version": "4+",
        "site": "https://svelte.dev/",
        "typescript_integration": "Via fichiers .svelte avec <script lang='ts'>",
        "key_features": [
            "Pas de Virtual DOM",
            "Réactivité déclarative",
            "Moins de code boilerplate",
            "Transitions et animations intégrées"
        ],
        "example": """
<script lang="ts">
  export let name: string;
</script>

<h1>Hello, {name}!</h1>

<style>
  h1 {
    color: #ff3e00;
  }
</style>
"""
    }
}

BACKEND_FRAMEWORKS = {
    "NestJS": {
        "description": "Framework Node.js progressif pour construire des applications backend",
        "version": "10+",
        "site": "https://nestjs.com/",
        "typescript_integration": "Native",
        "key_features": [
            "Architecture inspirée d'Angular",
            "Injection de dépendances",
            "Décorateurs",
            "Modules",
            "Middleware",
            "Support GraphQL, REST, WebSockets"
        ],
        "example": """
import { Controller, Get, Param } from '@nestjs/common';

@Controller('users')
export class UsersController {
  @Get(':id')
  findOne(@Param('id') id: string): string {
    return `User with ID ${id}`;
  }
}
"""
    },
    "Express (avec TypeScript)": {
        "description": "Framework web minimaliste pour Node.js",
        "version": "4+",
        "site": "https://expressjs.com/",
        "typescript_integration": "Via @types/express",
        "key_features": [
            "Routage",
            "Middleware",
            "Léger et flexible",
            "Grande communauté"
        ],
        "example": """
import express, { Request, Response } from 'express';

const app = express();
const port = 3000;

app.get('/users/:id', (req: Request, res: Response) => {
  const userId = req.params.id;
  res.send(`User with ID ${userId}`);
});

app.listen(port, () => {
  console.log(`Server running at http://localhost:${port}`);
});
"""
    },
    "Deno": {
        "description": "Runtime JavaScript/TypeScript sécurisé par défaut",
        "version": "1.35+",
        "site": "https://deno.land/",
        "typescript_integration": "Native",
        "key_features": [
            "Sécurité par défaut",
            "Support TypeScript intégré",
            "Modules ES",
            "API Web standard",
            "Outils intégrés (linter, formatter)"
        ],
        "example": """
// server.ts
import { serve } from "https://deno.land/std/http/server.ts";

interface User {
  id: string;
  name: string;
}

const handler = (req: Request): Response => {
  const url = new URL(req.url);
  const userId = url.pathname.split('/').pop();
  
  const user: User = { id: userId || '0', name: 'Test User' };
  
  return new Response(JSON.stringify(user), {
    headers: { "content-type": "application/json" },
  });
};

serve(handler, { port: 8000 });
"""
    }
}

STATE_MANAGEMENT = {
    "Redux Toolkit": {
        "description": "Boîte à outils officielle pour Redux avec TypeScript",
        "site": "https://redux-toolkit.js.org/",
        "typescript_integration": "Excellent",
        "key_features": [
            "createSlice",
            "configureStore",
            "createAsyncThunk",
            "RTK Query"
        ],
        "example": """
import { createSlice, PayloadAction } from '@reduxjs/toolkit';

interface CounterState {
  value: number;
}

const initialState: CounterState = {
  value: 0,
};

export const counterSlice = createSlice({
  name: 'counter',
  initialState,
  reducers: {
    increment: (state) => {
      state.value += 1;
    },
    decrement: (state) => {
      state.value -= 1;
    },
    incrementByAmount: (state, action: PayloadAction<number>) => {
      state.value += action.payload;
    },
  },
});

export const { increment, decrement, incrementByAmount } = counterSlice.actions;
export default counterSlice.reducer;
"""
    },
    "MobX": {
        "description": "Bibliothèque de gestion d'état simple et évolutive",
        "site": "https://mobx.js.org/",
        "typescript_integration": "Excellent",
        "key_features": [
            "Observable",
            "Computed values",
            "Reactions",
            "Actions"
        ],
        "example": """
import { makeAutoObservable } from "mobx";

class TodoStore {
  todos: string[] = [];
  
  constructor() {
    makeAutoObservable(this);
  }
  
  addTodo(task: string) {
    this.todos.push(task);
  }
  
  get todoCount() {
    return this.todos.length;
  }
}

export const todoStore = new TodoStore();
"""
    },
    "Zustand": {
        "description": "Bibliothèque de gestion d'état minimaliste avec hooks",
        "site": "https://github.com/pmndrs/zustand",
        "typescript_integration": "Excellent",
        "key_features": [
            "API simple basée sur hooks",
            "Pas de Provider nécessaire",
            "Sélecteurs optimisés",
            "Middleware"
        ],
        "example": """
import create from 'zustand';

interface BearState {
  bears: number;
  increase: (by: number) => void;
}

const useBearStore = create<BearState>((set) => ({
  bears: 0,
  increase: (by) => set((state) => ({ bears: state.bears + by })),
}));

// Dans un composant:
// const { bears, increase } = useBearStore();
"""
    }
}

TESTING_LIBRARIES = {
    "Jest": {
        "description": "Framework de test JavaScript avec support TypeScript",
        "site": "https://jestjs.io/",
        "typescript_integration": "Via ts-jest",
        "key_features": [
            "Tests unitaires",
            "Snapshots",
            "Mocks",
            "Code coverage"
        ],
        "example": """
import { sum } from '../math';

describe('sum function', () => {
  test('adds 1 + 2 to equal 3', () => {
    expect(sum(1, 2)).toBe(3);
  });
  
  test('handles negative numbers', () => {
    expect(sum(-1, -2)).toBe(-3);
  });
});
"""
    },
    "Vitest": {
        "description": "Framework de test ultra-rapide pour Vite",
        "site": "https://vitest.dev/",
        "typescript_integration": "Native",
        "key_features": [
            "Compatible avec l'API Jest",
            "Exécution rapide",
            "Support ESM",
            "Intégration Vite"
        ],
        "example": """
import { describe, it, expect } from 'vitest';
import { sum } from '../math';

describe('sum function', () => {
  it('adds 1 + 2 to equal 3', () => {
    expect(sum(1, 2)).toBe(3);
  });
  
  it('handles negative numbers', () => {
    expect(sum(-1, -2)).toBe(-3);
  });
});
"""
    },
    "Cypress": {
        "description": "Framework de test end-to-end avec support TypeScript",
        "site": "https://www.cypress.io/",
        "typescript_integration": "Excellent",
        "key_features": [
            "Tests E2E",
            "Tests de composants",
            "Capture d'écran",
            "Vidéos",
            "Mocks et stubs"
        ],
        "example": """
describe('Login Page', () => {
  it('successfully logs in', () => {
    cy.visit('/login');
    
    cy.get('[data-cy=username]').type('testuser');
    cy.get('[data-cy=password]').type('password123');
    cy.get('[data-cy=submit]').click();
    
    cy.url().should('include', '/dashboard');
    cy.get('[data-cy=welcome-message]').should('contain', 'Welcome, Test User');
  });
});
"""
    },
    "Playwright": {
        "description": "Framework de test end-to-end multi-navigateurs",
        "site": "https://playwright.dev/",
        "typescript_integration": "Native",
        "key_features": [
            "Multi-navigateurs",
            "API moderne",
            "Auto-waiting",
            "Génération de code",
            "Traces"
        ],
        "example": """
import { test, expect } from '@playwright/test';

test('login flow', async ({ page }) => {
  await page.goto('/login');
  
  await page.fill('[data-testid="username"]', 'testuser');
  await page.fill('[data-testid="password"]', 'password123');
  await page.click('[data-testid="login-button"]');
  
  await expect(page).toHaveURL(/.*dashboard/);
  await expect(page.locator('[data-testid="welcome"]')).toContainText('Welcome, Test User');
});
"""
    }
}

def register(app: FastMCP, app_state: dict):
    """
    Enregistre les ressources de frameworks TypeScript dans l'application FastMCP.
    
    Args:
        app: L'application FastMCP
        app_state: L'état de l'application
    """
    @app.resource("collegue://typescript/frameworks/frontend")
    def typescript_frontend_frameworks() -> str:
        """Fournit des informations sur les frameworks frontend TypeScript."""
        return json.dumps(FRONTEND_FRAMEWORKS)
    
    @app.resource("collegue://typescript/frameworks/backend")
    def typescript_backend_frameworks() -> str:
        """Fournit des informations sur les frameworks backend TypeScript."""
        return json.dumps(BACKEND_FRAMEWORKS)
    
    @app.resource("collegue://typescript/frameworks/state_management")
    def typescript_state_management() -> str:
        """Fournit des informations sur les bibliothèques de gestion d'état TypeScript."""
        return json.dumps(STATE_MANAGEMENT)
    
    @app.resource("collegue://typescript/frameworks/testing")
    def typescript_testing_libraries() -> str:
        """Fournit des informations sur les bibliothèques de test TypeScript."""
        return json.dumps(TESTING_LIBRARIES)
    
    @app.resource("collegue://typescript/frameworks/{framework_name}")
    def typescript_framework_example(framework_name: str) -> str:
        """
        Fournit un exemple d'utilisation pour un framework TypeScript spécifique.
        
        Args:
            framework_name: Nom du framework TypeScript (ex: 'Angular', 'React', 'NestJS', etc.)
        """
        if not framework_name:
            return json.dumps({"error": "Framework name is required"})
        
        # Rechercher dans les frameworks frontend
        for name, info in FRONTEND_FRAMEWORKS.items():
            if framework_name.lower() in name.lower():
                return json.dumps(info)
        
        # Rechercher dans les frameworks backend
        for name, info in BACKEND_FRAMEWORKS.items():
            if framework_name.lower() in name.lower():
                return json.dumps(info)
        
        # Rechercher dans les bibliothèques de gestion d'état
        for name, info in STATE_MANAGEMENT.items():
            if framework_name.lower() in name.lower():
                return json.dumps(info)
        
        # Rechercher dans les bibliothèques de test
        for name, info in TESTING_LIBRARIES.items():
            if framework_name.lower() in name.lower():
                return json.dumps(info)
        
        return json.dumps({"error": f"Framework '{framework_name}' not found"})
