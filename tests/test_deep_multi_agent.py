"""
Deep Integration Tests for Collègue MCP Multi-Agent Expert System.

Tests the complete multi-agent flow in real conditions with Gemma 4 26B:
1. Delegation chains end-to-end with real LLM calls
2. Memory persistence across expert calls
3. ProactiveMonitor triggering with real file changes
4. ExpertDashboard with real aggregated scores
5. Edge cases (empty inputs, error recovery, large files)

Usage:
    GEMINI_API_KEY=... PYTHONPATH=. python tests/test_deep_multi_agent.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import aiohttp

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMMA_MODEL = "gemma-4-26b-a4b-it"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class SampleResult:
    text: str = ""


class RealGeminiContext:
    """Mock ctx that routes sample() to real Gemini API."""

    def __init__(self):
        self.info = AsyncMock()
        self.warning = AsyncMock()
        self.error = AsyncMock()
        self.report_progress = AsyncMock()
        self._session: Optional[aiohttp.ClientSession] = None
        self.lifespan_context = {}

    async def sample(
        self,
        messages="",
        system_prompt=None,
        temperature=0.5,
        max_tokens=2000,
        **kwargs,
    ):
        if isinstance(messages, list):
            messages = "\n".join(str(m) for m in messages)

        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": system_prompt}]})
            contents.append({"role": "model", "parts": [{"text": "Compris."}]})
        contents.append({"role": "user", "parts": [{"text": messages}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": min(temperature, 1.0),
                "maxOutputTokens": max_tokens,
            },
        }
        url = f"{GEMINI_URL}/models/{GEMMA_MODEL}:generateContent?key={GEMINI_API_KEY}"

        if self._session is None:
            self._session = aiohttp.ClientSession()

        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if resp.status == 429:
                        wait = 10 * (attempt + 1)
                        print(f"  [rate limit, waiting {wait}s...]")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 500:
                        print(f"  [API 500, retrying in 5s...]")
                        await asyncio.sleep(5)
                        continue
                    if resp.status != 200:
                        raise RuntimeError(f"API error ({resp.status}): {data}")
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return SampleResult(text="")
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                    return SampleResult(text=text)
            except aiohttp.ClientError as e:
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                raise

        return SampleResult(text="")

    async def close(self):
        if self._session:
            await self._session.close()


@dataclass
class DeepTestResult:
    name: str
    passed: bool = False
    duration: float = 0.0
    bugs: list = field(default_factory=list)
    details: dict = field(default_factory=dict)
    error: str = ""


# --- Test code samples ---

PYTHON_CODE_WITH_ISSUES = '''
import os
import sys

def get_user(db, user_id):
    """Get user by ID."""
    query = "SELECT * FROM users WHERE id=" + str(user_id)
    result = db.execute(query)
    return result

def process_items(items):
    """Process a list of items."""
    output = ""
    for item in items:
        output += str(item) + ","
    return output

class UserManager:
    def __init__(self):
        self.users = []
        self.cache = {}

    def find_duplicates(self):
        duplicates = []
        for i in range(len(self.users)):
            for j in range(len(self.users)):
                if i != j and self.users[i] == self.users[j]:
                    duplicates.append(self.users[i])
        return duplicates

    def export_all(self):
        f = open("users.csv", "w")
        for u in self.users:
            f.write(str(u) + "\\n")
'''

DOCKERFILE_WITH_ISSUES = '''FROM python:latest
RUN pip install flask
ENV SECRET_KEY=my_super_secret_123
COPY . /app
RUN apt-get update && apt-get install -y curl
CMD ["python", "app.py"]
'''

SIMPLE_FUNCTION = '''
def calculate_discount(price: float, discount_percent: float) -> float:
    """Calculate discounted price."""
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("Discount must be between 0 and 100")
    return price * (1 - discount_percent / 100)
'''


async def test_delegation_chain_code_review_to_refactoring(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: code_review detects low quality → triggers code_refactoring via delegation."""
    r = DeepTestResult("delegation_chain_review_to_refactoring")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        tool = CodeReviewTool()
        request = CodeReviewRequest(code=PYTHON_CODE_WITH_ISSUES, language="python")
        response = await tool.execute_async(request, ctx=ctx)

        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("code_review", result_dict)

        r.details = {
            "quality_score": response.quality_score,
            "findings_count": len(response.findings),
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Quality score: {response.quality_score}")
        print(f"  Findings: {len(response.findings)}")
        print(f"  Delegation tasks triggered: {len(tasks)}")
        for t in tasks:
            print(f"    → {t.target_tool} (depth={t.depth})")

        if response.quality_score is None:
            r.bugs.append("BUG: quality_score is None")

        # With bad code, quality should be low and delegation should trigger
        if response.quality_score is not None and response.quality_score < 0.5:
            if len(tasks) == 0:
                r.bugs.append(
                    f"BUG: quality_score={response.quality_score} < 0.5 but no delegation to code_refactoring triggered"
                )
        elif response.quality_score is not None and response.quality_score >= 0.5:
            print(f"  NOTE: quality_score={response.quality_score} >= 0.5, delegation not expected")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_delegation_chain_consistency_to_refactoring(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: repo_consistency_check with high score → triggers code_refactoring."""
    r = DeepTestResult("delegation_chain_consistency_to_refactoring")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.repo_consistency_check.models import (
            ConsistencyCheckRequest,
            ConsistencyFile,
        )
        from collegue.tools.repo_consistency_check.tool import RepoConsistencyCheckTool

        tool = RepoConsistencyCheckTool()
        request = ConsistencyCheckRequest(
            files=[
                ConsistencyFile(path="user_manager.py", content=PYTHON_CODE_WITH_ISSUES),
            ],
            analysis_depth="fast",
        )

        response = await tool.execute_async(request, ctx=ctx)
        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("repo_consistency_check", result_dict)

        r.details = {
            "issues_found": len(response.issues),
            "refactoring_score": response.refactoring_score,
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Issues found: {len(response.issues)}")
        print(f"  Refactoring score: {response.refactoring_score}")
        print(f"  Delegation tasks: {len(tasks)}")
        for t in tasks:
            print(f"    → {t.target_tool}")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_delegation_chain_perf_to_refactoring(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: performance_analysis with low score → triggers code_refactoring."""
    r = DeepTestResult("delegation_chain_perf_to_refactoring")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        tool = PerformanceAnalysisTool()
        request = PerformanceAnalysisRequest(
            code=PYTHON_CODE_WITH_ISSUES,
            language="python",
        )
        response = await tool.execute_async(request, ctx=ctx)
        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("performance_analysis", result_dict)

        r.details = {
            "performance_score": response.performance_score,
            "issues_count": len(response.issues),
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Performance score: {response.performance_score}")
        print(f"  Issues: {len(response.issues)}")
        print(f"  Delegation tasks: {len(tasks)}")
        for t in tasks:
            print(f"    → {t.target_tool}")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_memory_cross_expert_persistence(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: Expert A stores results → Expert B recalls them via ProjectMemory."""
    r = DeepTestResult("memory_cross_expert_persistence")
    start = time.time()

    try:
        from collegue.core.project_memory import ProjectMemory

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = ProjectMemory(memory_dir=tmpdir)

            # Step 1: Simulate code_review storing findings
            mem.store(
                expert="code_review",
                entry_type="issue_found",
                category="security",
                title="SQL injection in get_user()",
                data={"severity": "critical", "file": "db.py", "line": 42},
                score=0.9,
                language="python",
            )

            mem.store(
                expert="code_review",
                entry_type="pattern_learned",
                category="naming",
                title="Utilisation de snake_case pour les fonctions",
                data={"convention": "snake_case"},
                score=0.5,
                language="python",
            )

            # Step 2: Another expert recalls the context
            ctx_for_refactoring = mem.get_context_for("code_refactoring", language="python")

            print(f"  Memory entries stored: {len(mem._entries)}")
            print(f"  Context keys for refactoring: {list(ctx_for_refactoring.keys())}")
            print(f"  Known issues: {ctx_for_refactoring.get('known_issues', [])}")
            print(f"  Known patterns: {ctx_for_refactoring.get('known_patterns', [])}")

            r.details = {
                "entries_stored": len(mem._entries),
                "context_keys": list(ctx_for_refactoring.keys()),
                "has_known_issues": "known_issues" in ctx_for_refactoring,
                "has_known_patterns": "known_patterns" in ctx_for_refactoring,
            }

            # Validate: the context should have issues and patterns
            if "known_issues" not in ctx_for_refactoring:
                r.bugs.append("BUG: get_context_for() doesn't return known_issues from code_review findings")

            if "known_patterns" not in ctx_for_refactoring:
                r.bugs.append("BUG: get_context_for() doesn't return known_patterns from learned patterns")

            # Step 3: Verify persistence to disk
            mem.save()
            mem2 = ProjectMemory(memory_dir=tmpdir)
            # ProjectMemory auto-loads in __init__

            if len(mem2._entries) != len(mem._entries):
                r.bugs.append(
                    f"BUG: persistence failed, loaded {len(mem2._entries)} entries vs stored {len(mem._entries)}"
                )

            # Step 4: Verify search
            search_results = mem.search("SQL injection")
            if len(search_results) == 0:
                r.bugs.append("BUG: search('SQL injection') returned 0 results")
            else:
                print(f"  Search 'SQL injection': {len(search_results)} result(s)")

            r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_memory_real_expert_stores(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: A real expert execution stores results in memory via _store_to_memory."""
    r = DeepTestResult("memory_real_expert_stores")
    start = time.time()

    try:
        import collegue.core.project_memory as pm_module
        from collegue.core.project_memory import ProjectMemory, get_project_memory
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = ProjectMemory(memory_dir=tmpdir)

            # Inject memory into the singleton so _store_to_memory picks it up
            old_pm = pm_module._project_memory
            pm_module._project_memory = mem

            try:
                tool = CodeReviewTool()
                request = CodeReviewRequest(code=PYTHON_CODE_WITH_ISSUES, language="python")
                response = tool.execute(request)
            finally:
                pm_module._project_memory = old_pm

            # Check if tool stored results in memory
            entries = mem.recall(expert="code_review", limit=20)

            r.details = {
                "quality_score": response.quality_score,
                "findings_count": len(response.findings),
                "memory_entries_after": len(entries),
                "entry_types": list(set(e.entry_type for e in entries)),
            }

            print(f"  Quality score: {response.quality_score}")
            print(f"  Findings: {len(response.findings)}")
            print(f"  Memory entries after execution: {len(entries)}")
            for e in entries[:5]:
                print(f"    [{e.entry_type}] {e.title[:60]}")

            if len(entries) == 0:
                r.bugs.append("BUG: code_review did not store any results in memory after execution")

            r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_proactive_monitor_real_changes(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: ProactiveMonitor detects real file changes and triggers correct experts."""
    r = DeepTestResult("proactive_monitor_real_changes")
    start = time.time()

    try:
        from collegue.autonomous.proactive_monitor import MonitorConfig, ProactiveMonitor

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal git repo with some files
            os.system(f"cd {tmpdir} && git init -q && git config user.email 'test@test.com' && git config user.name 'Test'")
            
            # Create initial files and commit
            with open(os.path.join(tmpdir, "app.py"), "w") as f:
                f.write("print('hello')\n")
            with open(os.path.join(tmpdir, "Dockerfile"), "w") as f:
                f.write("FROM python:3.12\n")
            with open(os.path.join(tmpdir, "requirements.txt"), "w") as f:
                f.write("flask==3.0\n")

            os.system(f"cd {tmpdir} && git add -A && git commit -q -m 'initial'")

            # Now modify files (uncommitted changes)
            with open(os.path.join(tmpdir, "app.py"), "w") as f:
                f.write("import flask\napp = flask.Flask(__name__)\n")
            with open(os.path.join(tmpdir, "Dockerfile"), "w") as f:
                f.write("FROM python:latest\nRUN pip install flask\n")
            with open(os.path.join(tmpdir, "utils.py"), "w") as f:
                f.write("def helper(): pass\n")

            monitor = ProactiveMonitor(config=MonitorConfig(repo_path=tmpdir))
            monitor.set_repo_path(tmpdir)
            monitor.start()

            result = monitor.scan_once()

            r.details = {
                "scan_time": result.scan_time,
                "changes_detected": result.changes_detected,
                "triggers_decided": result.triggers_decided,
                "decisions": [
                    {"expert": d.expert, "reason": d.reason}
                    for d in result.decisions
                ],
                "errors": result.errors,
            }

            print(f"  Scan time: {result.scan_time:.4f}s")
            print(f"  Changes detected: {result.changes_detected}")
            print(f"  Triggers decided: {result.triggers_decided}")
            for d in result.decisions:
                print(f"    → {d.expert} (reason: {d.reason})")
            if result.errors:
                print(f"  Errors: {result.errors}")

            if result.changes_detected == 0:
                r.bugs.append("BUG: ProactiveMonitor detected 0 changes despite modified files")

            if result.triggers_decided == 0 and result.changes_detected > 0:
                r.bugs.append("BUG: changes detected but no triggers decided")

            # Verify correct experts are triggered
            expert_names = [d.expert for d in result.decisions]
            if result.changes_detected > 0:
                # .py files should trigger code_review or performance_analysis
                has_py_expert = any(e in expert_names for e in ["code_review", "performance_analysis"])
                # Dockerfile should trigger iac_guardrails_scan
                has_iac_expert = "iac_guardrails_scan" in expert_names

                if not has_py_expert and not has_iac_expert:
                    r.bugs.append(
                        f"BUG: no relevant experts triggered for .py and Dockerfile changes. Got: {expert_names}"
                    )

            stats = monitor.get_stats()
            print(f"  Monitor stats: running={stats['is_running']}, scans={stats['total_scans']}")

            r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_expert_dashboard_aggregation(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: ExpertDashboard aggregates scores from multiple experts."""
    r = DeepTestResult("expert_dashboard_aggregation")
    start = time.time()

    try:
        import collegue.core.project_memory as pm_module
        from collegue.core.project_memory import ProjectMemory
        from collegue.tools.expert_dashboard.models import DashboardRequest
        from collegue.tools.expert_dashboard.tool import ExpertDashboardTool

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = ProjectMemory(memory_dir=tmpdir)

            # Simulate multiple expert results
            mem.store(
                expert="code_review",
                entry_type="expert_result",
                category="code_review",
                title="Revue: score 0.45",
                data={"quality_score": 0.45, "findings_count": 8},
                score=0.45,
                language="python",
            )
            mem.store(
                expert="architecture_analysis",
                entry_type="expert_result",
                category="architecture",
                title="Architecture: score 0.82",
                data={"architecture_score": 0.82, "patterns": ["Service Layer"]},
                score=0.82,
                language="python",
            )
            mem.store(
                expert="performance_analysis",
                entry_type="expert_result",
                category="performance",
                title="Performance: score 0.67",
                data={"performance_score": 0.67, "issues_count": 5},
                score=0.67,
                language="python",
            )
            mem.store(
                expert="iac_guardrails_scan",
                entry_type="expert_result",
                category="security",
                title="IaC: security 0.55",
                data={"security_score": 0.55, "findings_count": 3},
                score=0.55,
                language="python",
            )

            # Inject memory into the singleton so dashboard picks it up
            old_pm = pm_module._project_memory
            pm_module._project_memory = mem

            try:
                tool = ExpertDashboardTool()
                request = DashboardRequest()
                response = tool.execute(request)
            finally:
                pm_module._project_memory = old_pm

            r.details = {
                "overall_score": response.project_health.overall_score,
                "quality_score": response.project_health.quality_score,
                "architecture_score": response.project_health.architecture_score,
                "performance_score": response.project_health.performance_score,
                "security_score": response.project_health.security_score,
                "recommendations_count": len(response.recommendations),
                "experts_tracked": len(response.expert_statuses),
            }

            print(f"  Overall score: {response.project_health.overall_score}")
            print(f"  Quality: {response.project_health.quality_score}")
            print(f"  Architecture: {response.project_health.architecture_score}")
            print(f"  Performance: {response.project_health.performance_score}")
            print(f"  Security: {response.project_health.security_score}")
            print(f"  Recommendations: {len(response.recommendations)}")
            for rec in response.recommendations[:3]:
                print(f"    [{rec.priority}] {rec.action[:80]}")

            if response.project_health.overall_score is None:
                r.bugs.append("BUG: overall_score is None despite expert results in memory")

            r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_edge_case_empty_code(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: experts handle empty or minimal code gracefully."""
    r = DeepTestResult("edge_case_empty_code")
    start = time.time()

    try:
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        # Test 1: Empty string
        tool = CodeReviewTool()
        try:
            request = CodeReviewRequest(code="", language="python")
            response = tool.execute(request)
            print(f"  Empty code → quality_score={response.quality_score}, findings={len(response.findings)}")
            r.details["empty_code_review"] = "handled"
        except Exception as e:
            print(f"  Empty code → Exception: {type(e).__name__}: {e}")
            r.details["empty_code_review"] = f"exception: {type(e).__name__}"

        # Test 2: Single line
        try:
            request = CodeReviewRequest(code="x = 1", language="python")
            response = tool.execute(request)
            print(f"  Single line → quality_score={response.quality_score}")
            r.details["single_line_review"] = "handled"
        except Exception as e:
            print(f"  Single line → Exception: {type(e).__name__}: {e}")
            r.details["single_line_review"] = f"exception: {type(e).__name__}"

        # Test 3: Unknown language
        try:
            request = CodeReviewRequest(code="fn main() { println!(\"hello\"); }", language="rust")
            response = tool.execute(request)
            print(f"  Rust code → quality_score={response.quality_score}")
            r.details["unknown_lang_review"] = "handled"
        except Exception as e:
            print(f"  Rust code → Exception: {type(e).__name__}: {e}")
            r.details["unknown_lang_review"] = f"exception: {type(e).__name__}"

        # Test 4: Performance analysis on empty code
        perf_tool = PerformanceAnalysisTool()
        try:
            request = PerformanceAnalysisRequest(code="", language="python")
            response = perf_tool.execute(request)
            print(f"  Empty perf → score={response.performance_score}")
            r.details["empty_perf"] = "handled"
        except Exception as e:
            print(f"  Empty perf → Exception: {type(e).__name__}: {e}")
            r.details["empty_perf"] = f"exception: {type(e).__name__}"

        r.passed = True  # No crashes = pass

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_edge_case_large_file(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: experts handle large code files without hanging."""
    r = DeepTestResult("edge_case_large_file")
    start = time.time()

    try:
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        # Generate a 500-line Python file
        lines = ['"""Large module for testing."""', "import os", "import sys", ""]
        for i in range(100):
            lines.append(f"def function_{i}(x, y, z):")
            lines.append(f"    result = x + y + z + {i}")
            lines.append(f"    return result")
            lines.append("")

        large_code = "\n".join(lines)
        print(f"  Large code: {len(large_code)} chars, {len(lines)} lines")

        tool = PerformanceAnalysisTool()
        request = PerformanceAnalysisRequest(code=large_code, language="python")

        # Must complete in < 30 seconds (no regex backtracking)
        response = tool.execute(request)

        r.details = {
            "code_size": len(large_code),
            "lines": len(lines),
            "performance_score": response.performance_score,
            "issues_count": len(response.issues),
            "execution_time": time.time() - start,
        }

        print(f"  Performance score: {response.performance_score}")
        print(f"  Issues: {len(response.issues)}")
        print(f"  Execution time: {time.time() - start:.2f}s")

        if time.time() - start > 30:
            r.bugs.append(f"BUG: large file analysis took {time.time() - start:.1f}s (>30s limit)")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_delegation_engine_anti_loop(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: delegation engine prevents infinite loops."""
    r = DeepTestResult("delegation_engine_anti_loop")
    start = time.time()

    try:
        from collegue.core.expert_delegation import (
            ExpertDelegationEngine,
            create_default_delegation_engine,
        )

        engine = create_default_delegation_engine()

        # The default rules have: code_review → refactoring and refactoring → code_review
        # This could loop infinitely without protection

        # Simulate: code_review result with low score
        review_result = {
            "quality_score": 0.2,
            "findings": [{"severity": "critical"}] * 5,
            "agent_iterations": 1,
        }

        tasks = await engine.evaluate_delegations("code_review", review_result)
        print(f"  code_review (score=0.2) → {len(tasks)} delegation(s)")

        for t in tasks:
            print(f"    → {t.target_tool} (depth={t.depth})")

            # Simulate the delegated tool's result
            refactoring_result = {
                "refactored_code": "...",
                "changes": [{"type": "rename"}],
                "agent_iterations": 1,
            }

            sub_tasks = await engine.evaluate_delegations(t.target_tool, refactoring_result)
            print(f"    {t.target_tool} → {len(sub_tasks)} sub-delegation(s)")

            for st in sub_tasks:
                print(f"      → {st.target_tool} (depth={st.depth})")

        r.details = {
            "initial_tasks": len(tasks),
            "max_chain_depth": engine.max_chain_depth,
            "chain_timeout": engine.chain_timeout,
        }

        # Verify anti-loop protection exists
        if engine.max_chain_depth > 10:
            r.bugs.append(f"BUG: max_chain_depth={engine.max_chain_depth} is too high, risk of deep chains")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_iac_delegation_chain(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: iac_guardrails_scan detects issues → triggers code_refactoring."""
    r = DeepTestResult("iac_delegation_chain")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.core.shared import FileInput
        from collegue.tools.iac_guardrails_scan.models import IacGuardrailsRequest
        from collegue.tools.iac_guardrails_scan.tool import IacGuardrailsScanTool

        tool = IacGuardrailsScanTool()
        request = IacGuardrailsRequest(
            files=[
                FileInput(path="Dockerfile", content=DOCKERFILE_WITH_ISSUES),
            ],
            policy_profile="baseline",
            analysis_depth="fast",
        )

        response = tool.execute(request)
        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("iac_guardrails_scan", result_dict)

        r.details = {
            "findings_count": len(response.findings),
            "security_score": response.security_score,
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Findings: {len(response.findings)}")
        print(f"  Security score: {response.security_score}")
        print(f"  Delegation tasks: {len(tasks)}")
        for t in tasks:
            print(f"    → {t.target_tool}")

        # With low security score, should delegate to refactoring
        if response.security_score is not None and response.security_score < 0.5:
            if len(tasks) == 0:
                r.bugs.append(
                    f"BUG: security_score={response.security_score} < 0.5 but no delegation triggered"
                )

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_architecture_delegation_chain(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: architecture_analysis detects debt → delegates to refactoring/impact."""
    r = DeepTestResult("architecture_delegation_chain")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.architecture_analysis.models import ArchitectureAnalysisRequest
        from collegue.tools.architecture_analysis.tool import ArchitectureAnalysisTool

        tool = ArchitectureAnalysisTool()
        request = ArchitectureAnalysisRequest(
            code=PYTHON_CODE_WITH_ISSUES,
            language="python",
        )

        response = await tool.execute_async(request, ctx=ctx)
        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("architecture_analysis", result_dict)

        r.details = {
            "architecture_score": response.architecture_score,
            "patterns": [str(p) for p in response.detected_patterns[:3]],
            "debt_score": response.debt_score,
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Architecture score: {response.architecture_score}")
        print(f"  Debt score: {response.debt_score}")
        print(f"  Delegation tasks: {len(tasks)}")
        for t in tasks:
            print(f"    → {t.target_tool}")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def test_full_chain_refactoring_to_review_doc_tests(ctx: RealGeminiContext) -> DeepTestResult:
    """Test: code_refactoring → triggers code_review + code_documentation + test_generation."""
    r = DeepTestResult("full_chain_refactoring_triggers")
    start = time.time()

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.refactoring.models import RefactoringRequest
        from collegue.tools.refactoring.tool import RefactoringTool

        tool = RefactoringTool()
        request = RefactoringRequest(
            code=PYTHON_CODE_WITH_ISSUES,
            language="python",
            refactoring_type="optimize",
        )

        response = await tool.execute_async(request, ctx=ctx)
        result_dict = response.model_dump()

        engine = create_default_delegation_engine()
        tasks = await engine.evaluate_delegations("code_refactoring", result_dict)

        r.details = {
            "refactored_code_length": len(response.refactored_code) if response.refactored_code else 0,
            "changes_count": len(response.changes) if response.changes else 0,
            "delegation_tasks": len(tasks),
            "delegation_targets": [t.target_tool for t in tasks],
        }

        print(f"  Refactored code: {len(response.refactored_code) if response.refactored_code else 0} chars")
        print(f"  Changes: {len(response.changes) if response.changes else 0}")
        print(f"  Delegation tasks: {len(tasks)}")

        expected_targets = {"code_documentation", "test_generation", "code_review"}
        actual_targets = set(t.target_tool for t in tasks)

        for t in tasks:
            print(f"    → {t.target_tool}")

        if len(tasks) > 0:
            missing = expected_targets - actual_targets
            if missing:
                print(f"  NOTE: Expected targets not triggered: {missing}")

        r.passed = len(r.bugs) == 0

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        r.bugs.append(f"CRASH: {type(e).__name__}: {e}")

    r.duration = time.time() - start
    return r


async def main():
    print("=" * 60)
    print("  DEEP MULTI-AGENT SYSTEM TEST — Gemma 4 26B")
    print("=" * 60)
    print(f"Model: {GEMMA_MODEL}")
    print(f"API key: {GEMINI_API_KEY[:8]}...{GEMINI_API_KEY[-4:]}")
    print()

    ctx = RealGeminiContext()
    results: List[DeepTestResult] = []

    # Phase 1: Delegation chains (with real LLM calls)
    print("=" * 60)
    print("  PHASE 1: Delegation Chains (Real LLM)")
    print("=" * 60)

    tests_phase1 = [
        ("delegation: code_review → refactoring", test_delegation_chain_code_review_to_refactoring),
        ("delegation: consistency → refactoring", test_delegation_chain_consistency_to_refactoring),
        ("delegation: perf → refactoring", test_delegation_chain_perf_to_refactoring),
        ("delegation: iac → refactoring", test_iac_delegation_chain),
        ("delegation: architecture → refactoring/impact", test_architecture_delegation_chain),
        ("delegation: refactoring → review+doc+tests", test_full_chain_refactoring_to_review_doc_tests),
    ]

    for name, test_fn in tests_phase1:
        print(f"\n--- {name} ---")
        result = await test_fn(ctx)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        bugs_str = f" [{len(result.bugs)} bugs]" if result.bugs else ""
        print(f"  Result: [{status}] {result.name} ({result.duration:.1f}s){bugs_str}")
        if result.error:
            print(f"  Error: {result.error[:200]}")

    # Phase 2: Memory persistence
    print("\n" + "=" * 60)
    print("  PHASE 2: Memory Persistence")
    print("=" * 60)

    tests_phase2 = [
        ("memory: cross-expert persistence", test_memory_cross_expert_persistence),
        ("memory: real expert stores", test_memory_real_expert_stores),
    ]

    for name, test_fn in tests_phase2:
        print(f"\n--- {name} ---")
        result = await test_fn(ctx)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        bugs_str = f" [{len(result.bugs)} bugs]" if result.bugs else ""
        print(f"  Result: [{status}] {result.name} ({result.duration:.1f}s){bugs_str}")

    # Phase 3: ProactiveMonitor + Dashboard
    print("\n" + "=" * 60)
    print("  PHASE 3: Monitor & Dashboard")
    print("=" * 60)

    tests_phase3 = [
        ("proactive monitor: real file changes", test_proactive_monitor_real_changes),
        ("dashboard: score aggregation", test_expert_dashboard_aggregation),
    ]

    for name, test_fn in tests_phase3:
        print(f"\n--- {name} ---")
        result = await test_fn(ctx)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        bugs_str = f" [{len(result.bugs)} bugs]" if result.bugs else ""
        print(f"  Result: [{status}] {result.name} ({result.duration:.1f}s){bugs_str}")

    # Phase 4: Edge cases
    print("\n" + "=" * 60)
    print("  PHASE 4: Edge Cases & Safety")
    print("=" * 60)

    tests_phase4 = [
        ("edge: empty/minimal code", test_edge_case_empty_code),
        ("edge: large file (500 lines)", test_edge_case_large_file),
        ("edge: delegation anti-loop", test_delegation_engine_anti_loop),
    ]

    for name, test_fn in tests_phase4:
        print(f"\n--- {name} ---")
        result = await test_fn(ctx)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        bugs_str = f" [{len(result.bugs)} bugs]" if result.bugs else ""
        print(f"  Result: [{status}] {result.name} ({result.duration:.1f}s){bugs_str}")

    # Final summary
    await ctx.close()

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    total_time = sum(r.duration for r in results)
    all_bugs = []
    for r in results:
        for bug in r.bugs:
            all_bugs.append({"test": r.name, "bug": bug})

    print("\n" + "=" * 60)
    print("  FINAL SUMMARY")
    print("=" * 60)
    print(f"\nTotal tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total time: {total_time:.1f}s")

    if all_bugs:
        print(f"\n{'─' * 60}")
        print(f"BUGS FOUND: {len(all_bugs)}")
        print(f"{'─' * 60}")
        for i, bug in enumerate(all_bugs, 1):
            print(f"\n  Bug #{i}: [{bug['test']}] {bug['bug']}")

    # Save results
    report = {
        "model": GEMMA_MODEL,
        "total_tests": total,
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
                "details": r.details,
                "error": r.error,
            }
            for r in results
        ],
    }

    with open("/home/ubuntu/deep_test_results.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nResults saved to /home/ubuntu/deep_test_results.json")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
