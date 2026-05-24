"""
Tests réels avec Gemma 4 26B via l'API Gemini.

Ces tests appellent réellement l'API Gemini pour valider que la boucle
agentique fonctionne de bout en bout avec un vrai LLM.

Usage: GEMINI_API_KEY=... PYTHONPATH=. python -m pytest tests/test_real_gemma4.py -v -s
"""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMMA_MODEL = "gemma-4-26b-a4b-it"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

skip_no_key = pytest.mark.skipif(not GEMINI_API_KEY, reason="GEMINI_API_KEY not set")


@dataclass
class SampleResult:
    text: str = ""


class RealGeminiContext:
    """Mock ctx that routes sample() to the real Gemini API."""

    def __init__(self):
        self.info = AsyncMock()
        self.warning = AsyncMock()
        self.error = AsyncMock()
        self.report_progress = AsyncMock()
        self._session = None

    async def sample(
        self,
        messages: str = "",
        system_prompt: Optional[str] = None,
        temperature: float = 0.5,
        max_tokens: int = 2000,
        **kwargs,
    ) -> SampleResult:
        import aiohttp

        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": system_prompt}]})
            contents.append({"role": "model", "parts": [{"text": "Compris."}]})
        contents.append({"role": "user", "parts": [{"text": messages}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        url = f"{GEMINI_URL}/models/{GEMMA_MODEL}:generateContent?key={GEMINI_API_KEY}"

        if self._session is None:
            self._session = aiohttp.ClientSession()

        async with self._session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                error_msg = data.get("error", {}).get("message", str(data))
                raise RuntimeError(f"Gemini API error ({resp.status}): {error_msg}")

            candidates = data.get("candidates", [])
            if not candidates:
                return SampleResult(text="")

            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            return SampleResult(text=text)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None


# ─── Test 1: code_refactoring ────────────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_refactoring():
    """Test agentique: refactoring d'un code Python complexe."""
    from collegue.tools.refactoring.models import RefactoringRequest
    from collegue.tools.refactoring.tool import RefactoringTool

    tool = RefactoringTool()
    ctx = RealGeminiContext()

    code = """
import os
import sys
import json
import re

def process_data(data, flag, mode, extra=None):
    result = []
    if flag == True:
        if mode == "fast":
            for item in data:
                if item is not None:
                    if isinstance(item, dict):
                        if "value" in item:
                            result.append(item["value"])
                        else:
                            result.append(None)
                    else:
                        result.append(item)
        elif mode == "slow":
            for item in data:
                if item is not None:
                    result.append(item)
    else:
        if mode == "fast":
            result = data
        else:
            result = list(data)
    return result
"""

    request = RefactoringRequest(
        code=code,
        language="python",
        refactoring_type="clean",
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== REFACTORING ===")
        print(f"Iterations: {response.agent_iterations}")
        print(f"Best score: {response.agent_best_score}")
        print(f"Converged: {response.agent_converged}")
        print(f"Code (first 300 chars): {response.refactored_code[:300]}")

        assert response.refactored_code, "Le code refactoré ne doit pas être vide"
        assert response.agent_iterations >= 1, "Au moins 1 itération"
    finally:
        await ctx.close()


# ─── Test 2: test_generation ─────────────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_test_generation():
    """Test agentique: génération de tests pour une fonction multi-branches."""
    from collegue.tools.test_generation.models import TestGenerationRequest
    from collegue.tools.test_generation.tool import TestGenerationTool

    tool = TestGenerationTool()
    ctx = RealGeminiContext()

    code = '''
def calculate_price(base_price, quantity, discount_code=None, is_member=False):
    """Calculate the final price with various rules."""
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    if base_price < 0:
        raise ValueError("Price cannot be negative")

    total = base_price * quantity

    if discount_code == "HALF":
        total *= 0.5
    elif discount_code == "QUARTER":
        total *= 0.75
    elif discount_code and discount_code.startswith("FLAT"):
        try:
            flat = int(discount_code[4:])
            total = max(0, total - flat)
        except ValueError:
            pass

    if is_member and total > 100:
        total *= 0.9

    if total > 500:
        total -= 20

    return round(total, 2)
'''

    request = TestGenerationRequest(
        code=code,
        language="python",
        test_framework="pytest",
        coverage_target=0.8,
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== TEST GENERATION ===")
        print(f"Iterations: {response.agent_iterations}")
        print(f"Best score: {response.agent_best_score}")
        print(f"Converged: {response.agent_converged}")
        print(f"Test code (first 400 chars): {response.test_code[:400]}")
        print(f"Tested elements: {response.tested_elements}")

        assert response.test_code, "Le code de test ne doit pas être vide"
        assert "def test_" in response.test_code, "Doit contenir des fonctions de test"
        assert response.agent_iterations >= 1
    finally:
        await ctx.close()


# ─── Test 3: code_documentation ──────────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_documentation():
    """Test agentique: documentation d'une classe Python."""
    from collegue.tools.code_documentation.models import DocumentationRequest
    from collegue.tools.code_documentation.tool import DocumentationTool

    tool = DocumentationTool()
    ctx = RealGeminiContext()

    code = """
class UserManager:
    def __init__(self, db_connection, cache_ttl=300):
        self.db = db_connection
        self.cache_ttl = cache_ttl
        self._cache = {}

    def get_user(self, user_id: int) -> dict:
        if user_id in self._cache:
            return self._cache[user_id]
        user = self.db.query(f"SELECT * FROM users WHERE id={user_id}")
        self._cache[user_id] = user
        return user

    def create_user(self, name: str, email: str) -> int:
        result = self.db.execute(
            "INSERT INTO users (name, email) VALUES (?, ?)", (name, email)
        )
        return result.lastrowid

    def delete_user(self, user_id: int) -> bool:
        self._cache.pop(user_id, None)
        affected = self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return affected.rowcount > 0

    def list_users(self, limit: int = 100) -> list:
        return self.db.query(f"SELECT * FROM users LIMIT {limit}")
"""

    request = DocumentationRequest(
        code=code,
        language="python",
        doc_format="markdown",
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== DOCUMENTATION ===")
        print(f"Iterations: {response.agent_iterations}")
        print(f"Best score: {response.agent_best_score}")
        print(f"Converged: {response.agent_converged}")
        print(f"Coverage: {response.coverage}%")
        print(f"Doc (first 400 chars): {response.documentation[:400]}")

        assert response.documentation, "La documentation ne doit pas être vide"
        assert response.agent_iterations >= 1
    finally:
        await ctx.close()


# ─── Test 4: repo_consistency_check ──────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_consistency_check():
    """Test agentique: analyse deep d'incohérences de code."""
    from collegue.tools.repo_consistency_check.models import ConsistencyCheckRequest
    from collegue.tools.repo_consistency_check.tool import RepoConsistencyCheckTool

    tool = RepoConsistencyCheckTool()
    ctx = RealGeminiContext()

    request = ConsistencyCheckRequest(
        files=[
            {
                "path": "utils.py",
                "content": """
import os
import sys
import json
import re
import hashlib

def helper():
    return 42

def unused_function():
    x = 10
    y = 20
    return None

class OldManager:
    pass

def process(data):
    result = helper()
    return result
""",
            },
            {
                "path": "main.py",
                "content": """
from utils import process

def main():
    data = [1, 2, 3]
    result = process(data)
    print(result)

if __name__ == "__main__":
    main()
""",
            },
        ],
        language="python",
        analysis_depth="deep",
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== CONSISTENCY CHECK ===")
        print(f"Issues found: {len(response.issues)}")
        print(f"Refactoring score: {response.refactoring_score}")
        print(f"Depth used: {response.analysis_depth_used}")
        if response.llm_insights:
            print(f"LLM insights: {len(response.llm_insights)}")
            for ins in response.llm_insights[:3]:
                print(f"  - [{ins.category}] {ins.insight[:100]}")

        assert response.analysis_depth_used == "deep"
        assert len(response.issues) > 0, "Doit détecter des incohérences"
    finally:
        await ctx.close()


# ─── Test 5: impact_analysis ─────────────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_impact_analysis():
    """Test agentique: analyse d'impact d'un renommage."""
    from collegue.tools.impact_analysis.models import ImpactAnalysisRequest
    from collegue.tools.impact_analysis.tool import ImpactAnalysisTool

    tool = ImpactAnalysisTool()
    ctx = RealGeminiContext()

    request = ImpactAnalysisRequest(
        change_intent="Renommer la classe UserService en AuthenticationService",
        files=[
            {
                "path": "services/user_service.py",
                "content": """
class UserService:
    def authenticate(self, username, password):
        pass
    def get_profile(self, user_id):
        pass
    def update_password(self, user_id, new_password):
        pass
""",
            },
            {
                "path": "api/auth.py",
                "content": """
from services.user_service import UserService

class AuthController:
    def __init__(self):
        self.user_service = UserService()

    def login(self, request):
        return self.user_service.authenticate(
            request.username, request.password
        )
""",
            },
            {
                "path": "tests/test_auth.py",
                "content": """
from services.user_service import UserService

def test_login():
    svc = UserService()
    assert svc.authenticate("admin", "pass") is not None
""",
            },
        ],
        analysis_depth="deep",
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== IMPACT ANALYSIS ===")
        print(f"Impacted files: {len(response.impacted_files)}")
        print(f"Risk notes: {len(response.risk_notes)}")
        print(f"Agent iterations: {response.agent_iterations}")
        print(f"Agent best score: {response.agent_best_score}")
        print(f"Agent converged: {response.agent_converged}")
        if response.llm_insights:
            print(f"LLM insights: {len(response.llm_insights)}")
            for ins in response.llm_insights[:3]:
                print(f"  - [{ins.category}] {ins.insight[:100]}")

        assert len(response.impacted_files) > 0, "Doit détecter des fichiers impactés"
        assert response.analysis_depth_used in ("deep", "fast"), "Mode doit être deep ou fast (fallback OK)"
    finally:
        await ctx.close()


# ─── Test 6: iac_guardrails_scan ─────────────────────────────────────


@skip_no_key
@pytest.mark.asyncio
async def test_real_iac_scan():
    """Test agentique: scan de sécurité IaC sur un Dockerfile."""
    from collegue.tools.iac_guardrails_scan.models import IacGuardrailsRequest
    from collegue.tools.iac_guardrails_scan.tool import IacGuardrailsScanTool

    tool = IacGuardrailsScanTool()
    ctx = RealGeminiContext()

    request = IacGuardrailsRequest(
        files=[
            {
                "path": "Dockerfile",
                "content": """
FROM ubuntu:latest
RUN apt-get update && apt-get install -y curl wget
COPY . /app
RUN chmod 777 /app
EXPOSE 22
USER root
CMD ["python", "app.py"]
""",
            }
        ],
        analysis_depth="deep",
    )

    try:
        response = await tool.execute_async(request, ctx=ctx)
        print(f"\n=== IAC GUARDRAILS SCAN ===")
        print(f"Findings: {len(response.findings)}")
        print(f"Security score: {response.security_score}")
        for f in response.findings[:5]:
            title = getattr(f, "title", "") or getattr(f, "message", "") or str(f)
            print(f"  - [{f.severity}] {f.rule_id}: {title[:80]}")

        assert len(response.findings) > 0, "Doit détecter des problèmes de sécurité"
    finally:
        await ctx.close()
