"""
Tests réels du système multi-agents complet avec Gemma 4 26B.

Teste chaque expert individuellement, les chaînes de délégation,
la mémoire projet persistante, et le moniteur proactif.

Usage: GEMINI_API_KEY=... PYTHONPATH=. python tests/test_real_full_system.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMMA_MODEL = "gemma-4-26b-a4b-it"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"


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


# ─── Test data ───────────────────────────────────────────────────────

PYTHON_CODE_BAD = """
import os
import sys
import json
import re
import hashlib
import subprocess

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

def get_user(db, user_id):
    query = "SELECT * FROM users WHERE id=" + str(user_id)
    result = db.execute(query)
    return result

def read_all_lines(path):
    f = open(path)
    lines = f.readlines()
    return lines

class DataProcessor:
    def __init__(self):
        self.data = []
        self.cache = {}

    def add(self, item):
        self.data.append(item)
        self.cache = {}

    def find(self, query):
        result = ""
        for d in self.data:
            result = result + str(d) + ","
        return query in result

    def process_all(self):
        results = []
        for i in range(len(self.data)):
            item = self.data[i]
            for j in range(len(self.data)):
                other = self.data[j]
                if item == other and i != j:
                    results.append((i, j))
        return results
"""

DOCKERFILE_BAD = """
FROM ubuntu:latest
RUN apt-get update && apt-get install -y curl wget python3 python3-pip
COPY . /app
WORKDIR /app
RUN chmod 777 /app
RUN pip3 install -r requirements.txt
EXPOSE 22 8080
USER root
ENV DB_PASSWORD=secretpass123
CMD ["python3", "app.py"]
"""

MULTI_FILE_PROJECT = [
    {
        "path": "models/user.py",
        "content": """
class User:
    def __init__(self, name, email, role="user"):
        self.name = name
        self.email = email
        self.role = role
        self.active = True

    def deactivate(self):
        self.active = False

    def is_admin(self):
        return self.role == "admin"
""",
    },
    {
        "path": "services/user_service.py",
        "content": """
from models.user import User

class UserService:
    def __init__(self, db):
        self.db = db
        self._cache = {}

    def get_user(self, user_id):
        if user_id in self._cache:
            return self._cache[user_id]
        user = self.db.query("SELECT * FROM users WHERE id=" + str(user_id))
        self._cache[user_id] = user
        return user

    def create_user(self, name, email, role="user"):
        user = User(name, email, role)
        self.db.insert(user)
        return user

    def delete_user(self, user_id):
        self._cache.pop(user_id, None)
        self.db.delete("users", user_id)
""",
    },
    {
        "path": "api/routes.py",
        "content": """
from services.user_service import UserService

def setup_routes(app, db):
    service = UserService(db)

    @app.get("/users/{user_id}")
    def get_user(user_id):
        return service.get_user(user_id)

    @app.post("/users")
    def create_user(data):
        return service.create_user(data["name"], data["email"])

    @app.delete("/users/{user_id}")
    def delete_user(user_id):
        service.delete_user(user_id)
        return {"status": "ok"}
""",
    },
    {
        "path": "tests/test_user.py",
        "content": """
from models.user import User

def test_create_user():
    u = User("test", "test@example.com")
    assert u.name == "test"
    assert u.active == True

def test_deactivate():
    u = User("test", "test@example.com")
    u.deactivate()
    assert u.active == False
""",
    },
]


# ─── Results tracking ────────────────────────────────────────────────

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error: Optional[str] = None
        self.bugs: List[str] = []
        self.details: Dict[str, Any] = {}
        self.duration: float = 0.0

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        bugs_str = f" [{len(self.bugs)} bugs]" if self.bugs else ""
        return f"[{status}] {self.name} ({self.duration:.1f}s){bugs_str}"


ALL_RESULTS: List[TestResult] = []


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ─── Expert 1: code_review ───────────────────────────────────────────

async def test_code_review(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("code_review")
    start = time.time()
    try:
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        tool = CodeReviewTool()
        request = CodeReviewRequest(code=PYTHON_CODE_BAD, language="python")

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "quality_score": response.quality_score,
            "findings_count": len(response.findings),
            "category_scores": response.category_scores,
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
            "strengths": response.strengths[:3] if response.strengths else [],
            "recommendations": response.recommendations[:3] if response.recommendations else [],
        }

        print(f"  Quality score: {response.quality_score}")
        print(f"  Findings: {len(response.findings)}")
        print(f"  Agent iterations: {response.agent_iterations}")
        print(f"  Converged: {response.agent_converged}")
        for f in response.findings[:5]:
            print(f"    - [{f.severity}] {f.title}")

        # Validations
        if response.quality_score is None:
            r.bugs.append("BUG: quality_score is None")
        if len(response.findings) == 0:
            r.bugs.append("BUG: no findings on intentionally bad code")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 2: architecture_analysis ─────────────────────────────────

async def test_architecture_analysis(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("architecture_analysis")
    start = time.time()
    try:
        from collegue.tools.architecture_analysis.models import ArchitectureAnalysisRequest
        from collegue.tools.architecture_analysis.tool import ArchitectureAnalysisTool

        tool = ArchitectureAnalysisTool()
        code = "\n\n".join(f"# {f['path']}\n{f['content']}" for f in MULTI_FILE_PROJECT)
        request = ArchitectureAnalysisRequest(code=code, language="python")

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "architecture_score": response.architecture_score,
            "detected_patterns": response.detected_patterns,
            "issues_count": len(response.issues),
            "debt_score": response.debt_score,
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
        }

        print(f"  Architecture score: {response.architecture_score}")
        print(f"  Patterns detected: {response.detected_patterns}")
        print(f"  Issues: {len(response.issues)}")
        print(f"  Debt score: {response.debt_score}")
        print(f"  Agent iterations: {response.agent_iterations}")

        if response.architecture_score is None:
            r.bugs.append("BUG: architecture_score is None")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 3: performance_analysis ──────────────────────────────────

async def test_performance_analysis(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("performance_analysis")
    start = time.time()
    try:
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        # NOTE: Using shorter code to avoid regex catastrophic backtracking
        # in string_concat_loop and global_import_in_func patterns (Bug #1)
        perf_code = """
def process_all(data):
    results = []
    for i in range(len(data)):
        item = data[i]
        for j in range(len(data)):
            other = data[j]
            if item == other and i != j:
                results.append((i, j))
    return results

def read_all(path):
    f = open(path)
    lines = f.readlines()
    return lines
"""
        tool = PerformanceAnalysisTool()
        request = PerformanceAnalysisRequest(code=perf_code, language="python")

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "performance_score": response.performance_score,
            "issues_count": len(response.issues),
            "hotspots": response.hotspots[:3] if response.hotspots else [],
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
        }

        print(f"  Performance score: {response.performance_score}")
        print(f"  Issues: {len(response.issues)}")
        print(f"  Hotspots: {len(response.hotspots)}")
        print(f"  Agent iterations: {response.agent_iterations}")
        for issue in response.issues[:5]:
            print(f"    - [{issue.severity}] {issue.title}")

        if response.performance_score is None:
            r.bugs.append("BUG: performance_score is None")
        if len(response.issues) == 0:
            r.bugs.append("BUG: no issues found on O(n²) code")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 4: code_refactoring ──────────────────────────────────────

async def test_code_refactoring(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("code_refactoring")
    start = time.time()
    try:
        from collegue.tools.refactoring.models import RefactoringRequest
        from collegue.tools.refactoring.tool import RefactoringTool

        tool = RefactoringTool()
        request = RefactoringRequest(
            code=PYTHON_CODE_BAD,
            language="python",
            refactoring_type="clean",
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "has_refactored_code": bool(response.refactored_code),
            "code_length": len(response.refactored_code),
            "changes_count": len(response.changes) if response.changes else 0,
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
        }

        print(f"  Refactored code length: {len(response.refactored_code)}")
        print(f"  Changes: {len(response.changes) if response.changes else 0}")
        print(f"  Agent iterations: {response.agent_iterations}")
        print(f"  Converged: {response.agent_converged}")

        if not response.refactored_code:
            r.bugs.append("BUG: refactored_code is empty")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 5: test_generation ───────────────────────────────────────

async def test_test_generation(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("test_generation")
    start = time.time()
    try:
        from collegue.tools.test_generation.models import TestGenerationRequest
        from collegue.tools.test_generation.tool import TestGenerationTool

        tool = TestGenerationTool()
        code = """
def calculate_price(base_price, quantity, discount_code=None, is_member=False):
    if quantity <= 0:
        raise ValueError("Quantity must be positive")
    if base_price < 0:
        raise ValueError("Price cannot be negative")
    total = base_price * quantity
    if discount_code == "HALF":
        total *= 0.5
    elif discount_code == "QUARTER":
        total *= 0.75
    if is_member and total > 100:
        total *= 0.9
    return round(total, 2)
"""
        request = TestGenerationRequest(
            code=code,
            language="python",
            test_framework="pytest",
            coverage_target=0.8,
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "has_test_code": bool(response.test_code),
            "test_code_length": len(response.test_code),
            "tested_elements": response.tested_elements,
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
        }

        print(f"  Test code length: {len(response.test_code)}")
        print(f"  Tested elements: {response.tested_elements}")
        print(f"  Agent iterations: {response.agent_iterations}")

        if not response.test_code:
            r.bugs.append("BUG: test_code is empty")
        if "def test_" not in response.test_code:
            r.bugs.append("BUG: no test functions generated")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 6: code_documentation ────────────────────────────────────

async def test_code_documentation(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("code_documentation")
    start = time.time()
    try:
        from collegue.tools.code_documentation.models import DocumentationRequest
        from collegue.tools.code_documentation.tool import DocumentationTool

        tool = DocumentationTool()
        request = DocumentationRequest(
            code=MULTI_FILE_PROJECT[1]["content"],  # UserService
            language="python",
            doc_format="markdown",
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "has_documentation": bool(response.documentation),
            "doc_length": len(response.documentation),
            "coverage": response.coverage,
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
        }

        print(f"  Documentation length: {len(response.documentation)}")
        print(f"  Coverage: {response.coverage}%")
        print(f"  Agent iterations: {response.agent_iterations}")

        if not response.documentation:
            r.bugs.append("BUG: documentation is empty")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 7: iac_guardrails_scan ───────────────────────────────────

async def test_iac_guardrails_scan(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("iac_guardrails_scan")
    start = time.time()
    try:
        from collegue.tools.iac_guardrails_scan.models import IacGuardrailsRequest
        from collegue.tools.iac_guardrails_scan.tool import IacGuardrailsScanTool

        tool = IacGuardrailsScanTool()
        request = IacGuardrailsRequest(
            files=[{"path": "Dockerfile", "content": DOCKERFILE_BAD}],
            analysis_depth="deep",
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "findings_count": len(response.findings),
            "security_score": response.security_score,
            "agent_iterations": getattr(response, "agent_iterations", None),
        }

        print(f"  Findings: {len(response.findings)}")
        print(f"  Security score: {response.security_score}")
        for f in response.findings[:5]:
            title = getattr(f, "title", "") or getattr(f, "message", "") or str(f)
            print(f"    - [{f.severity}] {f.rule_id}: {title[:80]}")

        if len(response.findings) == 0:
            r.bugs.append("BUG: no findings on insecure Dockerfile")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 8: repo_consistency_check ────────────────────────────────

async def test_repo_consistency_check(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("repo_consistency_check")
    start = time.time()
    try:
        from collegue.tools.repo_consistency_check.models import ConsistencyCheckRequest
        from collegue.tools.repo_consistency_check.tool import RepoConsistencyCheckTool

        tool = RepoConsistencyCheckTool()
        request = ConsistencyCheckRequest(
            files=MULTI_FILE_PROJECT,
            language="python",
            analysis_depth="deep",
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "issues_count": len(response.issues),
            "refactoring_score": response.refactoring_score,
            "analysis_depth_used": response.analysis_depth_used,
            "llm_insights_count": len(response.llm_insights) if response.llm_insights else 0,
        }

        print(f"  Issues found: {len(response.issues)}")
        print(f"  Refactoring score: {response.refactoring_score}")
        print(f"  Depth used: {response.analysis_depth_used}")
        if response.llm_insights:
            for ins in response.llm_insights[:3]:
                print(f"    - [{ins.category}] {ins.insight[:80]}")

        if response.analysis_depth_used != "deep":
            r.bugs.append(f"BUG: expected deep analysis but got '{response.analysis_depth_used}'")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 9: impact_analysis ───────────────────────────────────────

async def test_impact_analysis(ctx: RealGeminiContext) -> TestResult:
    r = TestResult("impact_analysis")
    start = time.time()
    try:
        from collegue.tools.impact_analysis.models import ImpactAnalysisRequest
        from collegue.tools.impact_analysis.tool import ImpactAnalysisTool

        tool = ImpactAnalysisTool()
        request = ImpactAnalysisRequest(
            change_intent="Renommer la classe UserService en AuthenticationService",
            files=MULTI_FILE_PROJECT,
            analysis_depth="deep",
        )

        response = await tool.execute_async(request, ctx=ctx)
        r.details = {
            "impacted_files": len(response.impacted_files),
            "risk_notes": len(response.risk_notes),
            "agent_iterations": response.agent_iterations,
            "agent_converged": response.agent_converged,
            "analysis_depth_used": response.analysis_depth_used,
        }

        print(f"  Impacted files: {len(response.impacted_files)}")
        print(f"  Risk notes: {len(response.risk_notes)}")
        print(f"  Agent iterations: {response.agent_iterations}")
        print(f"  Depth used: {response.analysis_depth_used}")

        if len(response.impacted_files) == 0:
            r.bugs.append("BUG: no impacted files detected for class rename")
        if response.agent_iterations < 1:
            r.bugs.append("BUG: agent_iterations < 1")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Expert 10: expert_dashboard (no LLM, aggregation) ──────────────

async def test_expert_dashboard() -> TestResult:
    r = TestResult("expert_dashboard")
    start = time.time()
    try:
        from collegue.tools.expert_dashboard.models import DashboardRequest
        from collegue.tools.expert_dashboard.tool import ExpertDashboardTool

        tool = ExpertDashboardTool()
        request = DashboardRequest(
            include_memory=True,
            include_recommendations=True,
            top_recommendations=10,
        )

        response = tool.execute(request)
        r.details = {
            "project_health": response.project_health,
            "expert_count": len(response.expert_statuses),
            "recommendations_count": len(response.recommendations),
            "has_summary": bool(response.summary),
            "memory_stats": response.memory_stats,
            "delegation_activity": {
                "total_chains": response.delegation_activity.total_chains,
                "total_rules": response.delegation_activity.total_rules,
            },
        }

        print(f"  Project health: {response.project_health}")
        print(f"  Experts tracked: {len(response.expert_statuses)}")
        print(f"  Recommendations: {len(response.recommendations)}")
        print(f"  Delegation rules: {response.delegation_activity.total_rules}")
        print(f"  Summary: {response.summary[:100]}..." if response.summary else "  Summary: (none)")

        if not response.summary:
            r.bugs.append("BUG: dashboard summary is empty")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── ProjectMemory persistence test ──────────────────────────────────

async def test_project_memory_persistence() -> TestResult:
    r = TestResult("project_memory_persistence")
    start = time.time()
    try:
        from collegue.core.project_memory import ProjectMemory

        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = os.path.join(tmpdir, "memory")

            # Create, store, save
            mem1 = ProjectMemory(memory_dir=mem_dir)
            mem1.store(
                expert="code_review",
                entry_type="expert_result",
                category="code_review",
                title="Test review: score 0.75",
                data={"quality_score": 0.75, "findings": 3},
                score=0.75,
                language="python",
            )
            mem1.store(
                expert="architecture_analysis",
                entry_type="pattern_learned",
                category="architecture",
                title="Service Layer",
                data={"source": "static_analysis"},
                score=0.8,
            )
            mem1.store(
                expert="performance_analysis",
                entry_type="issue_found",
                category="cpu",
                title="O(n²) nested loop",
                data={"severity": "critical"},
                score=0.5,
                language="python",
            )
            mem1.save()

            # Verify file exists
            json_path = os.path.join(mem_dir, "project_memory.json")
            assert os.path.exists(json_path), "Memory JSON file not created"

            # Reload from disk
            mem2 = ProjectMemory(memory_dir=mem_dir)
            entries = mem2.recall(limit=100)
            assert len(entries) == 3, f"Expected 3 entries, got {len(entries)}"

            # Verify recall by expert
            cr_entries = mem2.recall(expert="code_review")
            assert len(cr_entries) == 1
            assert cr_entries[0].score == 0.75

            # Verify get_context_for
            ctx = mem2.get_context_for("code_review", language="python")
            assert "known_issues" in ctx or "patterns" in ctx or "past_fixes" in ctx or "project_info" in ctx

            # Verify search
            results = mem2.search("O(n²)")
            assert len(results) >= 1

            # Verify prune expired
            mem2.store(
                expert="test",
                entry_type="expert_result",
                category="test",
                title="Expired entry",
                data={},
                ttl_seconds=0.001,
            )
            import time as _time
            _time.sleep(0.01)
            mem2.prune_expired()
            assert len(mem2.recall(expert="test")) == 0

            r.details = {
                "entries_stored": 3,
                "entries_recalled": len(entries),
                "search_works": len(results) >= 1,
                "context_keys": list(ctx.keys()),
                "prune_works": True,
            }
            print(f"  Entries stored & recalled: 3")
            print(f"  Search works: True")
            print(f"  Context keys: {list(ctx.keys())}")
            print(f"  Prune expired works: True")

            r.passed = True
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── ProactiveMonitor test ───────────────────────────────────────────

async def test_proactive_monitor() -> TestResult:
    r = TestResult("proactive_monitor")
    start = time.time()
    try:
        from collegue.autonomous.proactive_monitor import ProactiveMonitor

        monitor = ProactiveMonitor()

        # Test start/stop lifecycle
        assert not monitor.is_running, "Should not be running initially"
        monitor.start()
        assert monitor.is_running, "Should be running after start()"
        monitor.stop()
        assert not monitor.is_running, "Should not be running after stop()"

        # Test with actual repo
        repo_path = str(Path(__file__).parent.parent)
        monitor.set_repo_path(repo_path)
        monitor.start()

        result = monitor.scan_once()
        r.details = {
            "scan_time": result.scan_time,
            "changes_detected": result.changes_detected,
            "triggers_decided": result.triggers_decided,
            "decisions_count": len(result.decisions),
            "errors": result.errors,
            "is_running": monitor.is_running,
        }

        print(f"  Scan time: {result.scan_time:.3f}s")
        print(f"  Changes detected: {result.changes_detected}")
        print(f"  Triggers decided: {result.triggers_decided}")
        print(f"  Errors: {result.errors}")
        for d in result.decisions[:5]:
            print(f"    - {d.expert} → {d.file_path} ({d.reason})")

        stats = monitor.get_stats()
        print(f"  Stats: {stats}")

        if result.errors:
            r.bugs.append(f"BUG: scan errors: {result.errors}")

        monitor.stop()
        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Delegation chain test ───────────────────────────────────────────

async def test_delegation_chain() -> TestResult:
    r = TestResult("delegation_chain")
    start = time.time()
    try:
        from collegue.core.expert_delegation import (
            DelegationRule,
            ExpertDelegationEngine,
            create_default_delegation_engine,
        )

        engine = create_default_delegation_engine()

        # Check default rules loaded
        rules = engine.get_all_rules()
        print(f"  Total delegation rules: {len(rules)}")
        for rule in rules[:10]:
            print(f"    {rule.source_tool} → {rule.target_tool} (prio={rule.priority})")

        # Test chain history
        history = engine.get_chain_history()
        print(f"  Chain history entries: {len(history)}")

        # Simulate a delegation decision
        # code_refactoring → code_review (if score < 0.8)
        cr_rules = engine.get_rules_for_tool("code_refactoring")
        print(f"  Rules from code_refactoring: {len(cr_rules)}")
        for rule in cr_rules:
            print(f"    → {rule.target_tool} (condition: {rule.condition})")

        # Check that all tools referenced in rules are real tools
        all_sources = {rule.source_tool for rule in rules}
        all_targets = {rule.target_tool for rule in rules}
        all_referenced = all_sources | all_targets
        print(f"  Referenced tools: {sorted(all_referenced)}")

        r.details = {
            "total_rules": len(rules),
            "referenced_tools": sorted(all_referenced),
            "chain_history": len(history),
        }

        if len(rules) == 0:
            r.bugs.append("BUG: no delegation rules loaded")

        r.passed = len(r.bugs) == 0
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Memory integration across experts ───────────────────────────────

async def test_memory_integration(ctx: RealGeminiContext) -> TestResult:
    """Tests that experts actually store results in ProjectMemory."""
    r = TestResult("memory_integration")
    start = time.time()
    try:
        # Reset singleton to use a temp dir
        import collegue.core.project_memory as pm_module
        from collegue.core.project_memory import ProjectMemory, _pm_lock
        with _pm_lock:
            old_pm = pm_module._project_memory
            pm_module._project_memory = None

        tmpdir = tempfile.mkdtemp()
        mem_dir = os.path.join(tmpdir, "memory")

        try:
            from collegue.core.project_memory import get_project_memory
            memory = get_project_memory(memory_dir=mem_dir)

            # Run code_review — it should store results
            from collegue.tools.code_review.models import CodeReviewRequest
            from collegue.tools.code_review.tool import CodeReviewTool

            tool = CodeReviewTool()
            request = CodeReviewRequest(
                code="def foo(): return 42\n",
                language="python",
            )

            # Sync path stores to memory
            response = tool.execute(request)

            entries_after_cr = memory.recall(expert="code_review", limit=100)
            print(f"  Entries after code_review (sync): {len(entries_after_cr)}")
            for e in entries_after_cr[:3]:
                print(f"    - [{e.entry_type}] {e.title}")

            # Run performance_analysis — should also store
            from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
            from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

            ptool = PerformanceAnalysisTool()
            # Short code to avoid regex backtracking bug
            short_perf_code = "def slow(data):\n    for i in range(len(data)):\n        for j in range(len(data)):\n            pass\n"
            prequest = PerformanceAnalysisRequest(code=short_perf_code, language="python")
            presponse = ptool.execute(prequest)

            entries_after_perf = memory.recall(expert="performance_analysis", limit=100)
            print(f"  Entries after performance_analysis (sync): {len(entries_after_perf)}")
            for e in entries_after_perf[:3]:
                print(f"    - [{e.entry_type}] {e.title}")

            total = memory.recall(limit=1000)
            print(f"  Total memory entries: {len(total)}")

            r.details = {
                "code_review_entries": len(entries_after_cr),
                "perf_entries": len(entries_after_perf),
                "total_entries": len(total),
            }

            if len(entries_after_cr) == 0:
                r.bugs.append("BUG: code_review sync path didn't store to memory")
            if len(entries_after_perf) == 0:
                r.bugs.append("BUG: performance_analysis sync path didn't store to memory")

            r.passed = len(r.bugs) == 0
        finally:
            # Restore singleton
            with _pm_lock:
                pm_module._project_memory = old_pm
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")
    r.duration = time.time() - start
    return r


# ─── Main runner ─────────────────────────────────────────────────────

async def main():
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    print_section("COLLEGUE MCP — Full System Real Test with Gemma 4 26B")
    print(f"Model: {GEMMA_MODEL}")
    print(f"API key: {GEMINI_API_KEY[:8]}...{GEMINI_API_KEY[-4:]}")

    ctx = RealGeminiContext()
    all_bugs: List[Dict[str, Any]] = []

    try:
        # ── Phase 1: Individual experts (LLM-backed) ──
        print_section("PHASE 1: Individual Expert Tests (LLM-backed)")

        experts_llm = [
            ("1/9", test_code_review),
            ("2/9", test_architecture_analysis),
            ("3/9", test_performance_analysis),
            ("4/9", test_code_refactoring),
            ("5/9", test_test_generation),
            ("6/9", test_code_documentation),
            ("7/9", test_iac_guardrails_scan),
            ("8/9", test_repo_consistency_check),
            ("9/9", test_impact_analysis),
        ]

        for label, test_fn in experts_llm:
            print(f"\n--- [{label}] {test_fn.__name__} ---")
            result = await test_fn(ctx)
            ALL_RESULTS.append(result)
            print(f"  Result: {result}")
            if result.bugs:
                for bug in result.bugs:
                    all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

        # ── Phase 2: Non-LLM components ──
        print_section("PHASE 2: Non-LLM Components")

        print(f"\n--- expert_dashboard ---")
        result = await test_expert_dashboard()
        ALL_RESULTS.append(result)
        print(f"  Result: {result}")
        if result.bugs:
            for bug in result.bugs:
                all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

        print(f"\n--- project_memory ---")
        result = await test_project_memory_persistence()
        ALL_RESULTS.append(result)
        print(f"  Result: {result}")
        if result.bugs:
            for bug in result.bugs:
                all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

        print(f"\n--- proactive_monitor ---")
        result = await test_proactive_monitor()
        ALL_RESULTS.append(result)
        print(f"  Result: {result}")
        if result.bugs:
            for bug in result.bugs:
                all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

        print(f"\n--- delegation_chain ---")
        result = await test_delegation_chain()
        ALL_RESULTS.append(result)
        print(f"  Result: {result}")
        if result.bugs:
            for bug in result.bugs:
                all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

        # ── Phase 3: Memory integration ──
        print_section("PHASE 3: Memory Integration Test")

        print(f"\n--- memory_integration ---")
        result = await test_memory_integration(ctx)
        ALL_RESULTS.append(result)
        print(f"  Result: {result}")
        if result.bugs:
            for bug in result.bugs:
                all_bugs.append({"expert": result.name, "bug": bug, "details": result.details, "error": result.error})

    finally:
        await ctx.close()

    # ── Summary ──
    print_section("FINAL SUMMARY")
    passed = sum(1 for r in ALL_RESULTS if r.passed)
    failed = sum(1 for r in ALL_RESULTS if not r.passed)
    total_time = sum(r.duration for r in ALL_RESULTS)

    print(f"\nTotal tests: {len(ALL_RESULTS)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total time: {total_time:.1f}s")

    if all_bugs:
        print(f"\n{'─'*60}")
        print(f"BUGS FOUND: {len(all_bugs)}")
        print(f"{'─'*60}")
        for i, bug in enumerate(all_bugs, 1):
            print(f"\n  Bug #{i}: [{bug['expert']}] {bug['bug']}")
            if bug.get("error"):
                # Print first 5 lines of traceback
                lines = bug["error"].split("\n")[:5]
                for line in lines:
                    print(f"    {line}")
    else:
        print("\nNo bugs found!")

    # Save results to JSON
    results_path = "/home/ubuntu/test_results.json"
    with open(results_path, "w") as f:
        json.dump({
            "model": GEMMA_MODEL,
            "total_tests": len(ALL_RESULTS),
            "passed": passed,
            "failed": failed,
            "total_time": total_time,
            "bugs": all_bugs,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "duration": r.duration,
                    "bugs": r.bugs,
                    "error": r.error,
                    "details": {k: str(v) for k, v in r.details.items()} if r.details else {},
                }
                for r in ALL_RESULTS
            ],
        }, f, indent=2)
    print(f"\nResults saved to {results_path}")

    return len(all_bugs)


if __name__ == "__main__":
    bug_count = asyncio.run(main())
    sys.exit(1 if bug_count > 0 else 0)
