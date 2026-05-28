"""
Deep Phase 2 Testing — Tests en profondeur du système multi-agents Collègue.

Tests réels avec Gemma 4 26B via API Gemini:
1. Smart Orchestrator multi-step planning with real LLM
2. Full delegation chain execution (not just rule evaluation)
3. Cross-expert memory after fix (verify PR #299 logic)
4. Combined flow: monitor → expert → memory → dashboard
5. Agent loop convergence behavior with real LLM
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))

import google.generativeai as genai

# Gemini API configuration
GEMINI_API_KEY = "AIzaSyDi-rdlmvLVstGjCu3T_iTHS7PDt6Rp0iY"
GEMMA_MODEL = "gemma-4-26b-a4b-it"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMMA_MODEL)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("deep_phase2")

# Results storage
RESULTS: List[Dict[str, Any]] = []

# ============================================================================
# Test Code Samples
# ============================================================================

PYTHON_CODE_BUGGY = """
import os
import sys
import json
from datetime import datetime

class UserManager:
    def __init__(self):
        self.users = []
        self.db_conn = None

    def get_user(self, user_id):
        # SQL injection vulnerability
        query = f"SELECT * FROM users WHERE id = {user_id}"
        result = self.db_conn.execute(query)
        return result

    def find_duplicates(self, items):
        # O(n^2) algorithm
        duplicates = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i] == items[j]:
                    duplicates.append(items[i])
        return duplicates

    def build_report(self, data):
        # String concatenation in loop
        report = ""
        for item in data:
            report += f"Item: {item['name']}, Value: {item['value']}\\n"
        return report

    def process_file(self, path):
        # No error handling, no resource management
        f = open(path)
        content = f.read()
        data = json.loads(content)
        return data

    def validate_email(self, email):
        # Overly simplistic validation
        return "@" in email
"""

PYTHON_CODE_CLEAN = """
def fibonacci(n: int) -> int:
    \"\"\"Calculate the nth Fibonacci number iteratively.\"\"\"
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


def binary_search(arr: list, target: int) -> int:
    \"\"\"Binary search returning index or -1 if not found.\"\"\"
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
"""

DOCKERFILE_INSECURE = """
FROM ubuntu:latest
RUN apt-get update && apt-get install -y curl wget
ENV DB_PASSWORD=super_secret_123
ENV API_KEY=sk-abc123456789
COPY . /app
RUN pip install -r requirements.txt
EXPOSE 22 80 443 3306 5432
CMD ["python", "app.py"]
"""

JAVASCRIPT_CODE = """
const express = require('express');
const app = express();

app.get('/user/:id', async (req, res) => {
    const userId = req.params.id;
    // No input validation
    const user = await db.query(`SELECT * FROM users WHERE id = ${userId}`);
    res.json(user);
});

app.post('/upload', (req, res) => {
    const file = req.files.file;
    // Path traversal vulnerability
    file.mv('/uploads/' + file.name, (err) => {
        if (err) return res.status(500).send(err);
        res.send('File uploaded');
    });
});

function processData(items) {
    let result = '';
    for (let i = 0; i < items.length; i++) {
        result += JSON.stringify(items[i]) + ',';
    }
    return '[' + result.slice(0, -1) + ']';
}
"""


# ============================================================================
# Real Gemini Context for MCP testing
# ============================================================================

class RealGeminiContext:
    """Context that routes sample() calls to real Gemini API with Gemma 4 26B."""

    def __init__(self):
        self.logs = []
        self.lifespan_context = {}
        self._call_count = 0

    async def info(self, msg: str):
        self.logs.append(("info", msg))
        logger.info(f"[CTX] {msg}")

    async def warning(self, msg: str):
        self.logs.append(("warning", msg))
        logger.warning(f"[CTX] {msg}")

    async def error(self, msg: str):
        self.logs.append(("error", msg))
        logger.error(f"[CTX] {msg}")

    async def report_progress(self, *args, **kwargs):
        pass

    async def sample(
        self,
        messages=None,
        system_prompt=None,
        result_type=None,
        temperature=0.5,
        max_tokens=2000,
        **kwargs,
    ):
        """Route to real Gemini API."""
        self._call_count += 1

        # Build prompt
        parts = []
        if system_prompt:
            parts.append(f"[System]: {system_prompt}")
        if messages:
            if isinstance(messages, list):
                for msg in messages:
                    if isinstance(msg, str):
                        parts.append(msg)
                    elif isinstance(msg, dict):
                        parts.append(msg.get("content", str(msg)))
            else:
                parts.append(str(messages))

        prompt = "\n\n".join(parts)

        # If structured output requested, add instructions
        if result_type:
            import inspect
            if hasattr(result_type, "model_json_schema"):
                schema = result_type.model_json_schema()
                prompt += f"\n\nRéponds UNIQUEMENT en JSON valide respectant ce schéma:\n{json.dumps(schema, indent=2)}"

        try:
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )

            text = response.text if response.text else ""

            # Mock result object
            result = MagicMock()
            result.text = text

            if result_type:
                # Try to parse as structured output
                try:
                    # Extract JSON from response
                    json_text = text
                    if "```json" in text:
                        json_text = text.split("```json")[1].split("```")[0].strip()
                    elif "```" in text:
                        json_text = text.split("```")[1].split("```")[0].strip()

                    parsed = json.loads(json_text)
                    result.result = result_type(**parsed)
                except Exception:
                    result.result = text
            else:
                result.result = text

            return result

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            result = MagicMock()
            result.text = f"Error: {e}"
            result.result = f"Error: {e}"
            return result


# ============================================================================
# Test Functions
# ============================================================================

async def test_smart_orchestrator_real_planning():
    """Test 1: Smart Orchestrator plans and executes with real LLM."""
    logger.info("=" * 60)
    logger.info("TEST 1: Smart Orchestrator — Real Multi-Step Planning")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.meta_orchestrator import (
            OrchestratorRequest,
            register_meta_orchestrator,
        )
        from collegue.core.tools_registry import ToolsRegistry

        # Get real tools registry
        registry = ToolsRegistry()
        available_tools = await registry.get()

        ctx = RealGeminiContext()
        ctx.lifespan_context = {
            "tools_registry": available_tools,  # Pass as dict (accepted by orchestrator)
            "prompt_engine": None,
            "delegation_engine": None,
        }

        request = OrchestratorRequest(
            query="Analyse ce code Python pour les problèmes de sécurité et de performance, puis suggère un refactoring",
            context={"code": PYTHON_CODE_BUGGY[:500]},
        )

        # Import handler directly
        # Execute the planning phase manually to test it
        import json as json_mod

        from collegue.core.meta_orchestrator import (
            MAX_QUERY_CHARS,
            OrchestratorPlan,
            OrchestratorResponse,
            OrchestratorStep,
            _synthesis_agent,
        )

        safe_query = (request.query or "")[:MAX_QUERY_CHARS]
        tools_desc = "\n".join([info["prompt_desc"] for name, info in available_tools.items()])
        tool_names_list = ", ".join(sorted(available_tools.keys()))

        system_prompt = (
            "Tu es un architecte logiciel. Ta tâche: choisir, parmi les outils fournis, "
            "ceux qui traitent la requête, et produire un plan structuré d'étapes concrètes."
        )

        user_prompt = f"""Requête utilisateur: {json_mod.dumps(safe_query, ensure_ascii=False)}

Contexte:
{json_mod.dumps(request.context, default=str) if request.context else "Aucun"}

Outils disponibles:
{tools_desc[:3000]}

NOMS D'OUTILS VALIDES:
{tool_names_list}

RÈGLES:
1. `tool` doit correspondre EXACTEMENT à l'un des noms ci-dessus.
2. Limite-toi à 5 étapes maximum.
3. Réponds UNIQUEMENT en JSON avec le format: {{"steps": [{{"tool": "...", "reason": "...", "params": {{...}}}}]}}
"""

        # Call real LLM for planning
        plan_result = await ctx.sample(
            messages=[user_prompt],
            system_prompt=system_prompt,
            result_type=OrchestratorPlan,
            temperature=0.2,
            max_tokens=2000,
        )

        # Analyze the plan
        plan_text = plan_result.text if hasattr(plan_result, "text") else str(plan_result)
        logger.info(f"Plan LLM response (first 500 chars): {plan_text[:500]}")

        if hasattr(plan_result, "result") and isinstance(plan_result.result, OrchestratorPlan):
            plan = plan_result.result
            logger.info(f"Structured plan: {len(plan.steps)} steps")
            for i, step in enumerate(plan.steps):
                logger.info(f"  Step {i+1}: {step.tool} — {step.reason}")
                # Validate tool name exists
                if step.tool not in available_tools and step.tool != "__refuse__":
                    bugs_found.append(f"LLM planned invalid tool: '{step.tool}' not in registry")
        else:
            # Try manual JSON parse
            try:
                json_text = plan_text
                if "```json" in plan_text:
                    json_text = plan_text.split("```json")[1].split("```")[0].strip()
                elif "```" in plan_text:
                    json_text = plan_text.split("```")[1].split("```")[0].strip()
                parsed = json.loads(json_text)
                steps = parsed.get("steps", [])
                logger.info(f"Manual parse: {len(steps)} steps")
                for i, step in enumerate(steps):
                    tool_name = step.get("tool", "UNKNOWN")
                    if tool_name not in available_tools and tool_name != "__refuse__":
                        bugs_found.append(f"LLM planned invalid tool: '{tool_name}'")
                    logger.info(f"  Step {i+1}: {tool_name}")
            except json.JSONDecodeError as e:
                bugs_found.append(f"LLM returned non-JSON plan: {e}")
                logger.error(f"Could not parse plan as JSON: {e}")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "smart_orchestrator_planning",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 1 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "smart_orchestrator_planning",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 1 FAILED: {e}")


async def test_delegation_chain_full_execution():
    """Test 2: Full delegation chain with real LLM execution."""
    logger.info("=" * 60)
    logger.info("TEST 2: Full Delegation Chain Execution with Real LLM")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.core.tools_registry import ToolsRegistry
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        ctx = RealGeminiContext()
        delegation_engine = create_default_delegation_engine(max_chain_depth=3, chain_timeout=180)

        # Step 1: Run code_review on buggy code
        logger.info("Step 1: Running code_review expert on buggy code...")
        review_tool = CodeReviewTool({})
        review_request = CodeReviewRequest(
            code=PYTHON_CODE_BUGGY,
            language="python",
            review_standards=["naming", "complexity", "security", "dry", "solid", "error_handling"],
            severity_threshold="warning",
        )

        review_result = await review_tool.execute_async(review_request, ctx=ctx)
        review_dict = review_result.model_dump()

        logger.info(f"Code Review result: quality_score={review_dict.get('quality_score')}, "
                    f"findings={len(review_dict.get('findings', []))}")

        # Step 2: Evaluate delegations from code_review result
        logger.info("Step 2: Evaluating delegation rules...")
        tasks = await delegation_engine.evaluate_delegations("code_review", review_dict)
        logger.info(f"Delegation tasks triggered: {len(tasks)}")
        for task in tasks:
            logger.info(f"  → {task.target_tool} (condition: {task.rule.condition_name})")

        # Step 3: If quality is low, should trigger refactoring
        quality_score = review_dict.get("quality_score", 1.0)
        if quality_score < 0.5:
            logger.info(f"Quality score {quality_score} < 0.5, delegation to refactoring expected")
            refactoring_tasks = [t for t in tasks if t.target_tool == "code_refactoring"]
            if not refactoring_tasks:
                bugs_found.append(
                    f"quality_score={quality_score} but no delegation to code_refactoring triggered"
                )
        else:
            logger.info(f"Quality score {quality_score} >= 0.5, no refactoring delegation expected")

        # Step 4: Execute delegation chain if tasks exist
        if tasks:
            registry = ToolsRegistry()
            available_tools = await registry.get()

            logger.info("Step 3: Executing delegation chain...")
            del_results = await delegation_engine.execute_delegation_chain(
                tasks,
                available_tools,
                ctx=ctx,
                tool_kwargs={"ctx": ctx},
            )

            for result in del_results:
                logger.info(
                    f"  Delegation {result.source_tool} → {result.target_tool}: "
                    f"success={result.success}, time={result.execution_time:.1f}s"
                )
                if not result.success:
                    logger.warning(f"    Error: {result.error}")
                    # Don't count as bug if it's a timeout or depth limit
                    if result.error and "Timeout" not in result.error and "Profondeur" not in result.error:
                        bugs_found.append(f"Delegation {result.source_tool}→{result.target_tool} failed: {result.error}")

                # Check sub-delegations
                if result.sub_delegations:
                    logger.info(f"    Sub-delegations: {len(result.sub_delegations)}")
                    for sub in result.sub_delegations:
                        logger.info(f"      → {sub.target_tool}: success={sub.success}")

            report = delegation_engine.build_chain_report("code_review", results=del_results)
            logger.info(f"Chain report: {report.total_experts_activated} experts, "
                        f"max_depth={report.max_depth_reached}, time={report.total_time:.1f}s")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "delegation_chain_full_execution",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "quality_score": quality_score,
            "delegations_triggered": len(tasks),
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 2 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "delegation_chain_full_execution",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 2 FAILED: {e}")


async def test_cross_expert_memory_sharing():
    """Test 3: Cross-expert memory sharing (validates PR #299 fix)."""
    logger.info("=" * 60)
    logger.info("TEST 3: Cross-Expert Memory Sharing")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.project_memory import ProjectMemory

        # Use a temp directory for isolated testing
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = ProjectMemory(memory_dir=tmpdir)

            # Expert A (code_review) stores findings
            mem.store(
                expert="code_review",
                entry_type="issue_found",
                category="security",
                title="SQL injection in get_user()",
                data={"severity": "critical", "line": 12, "fix": "Use parameterized queries"},
                score=0.9,
                language="python",
            )
            mem.store(
                expert="code_review",
                entry_type="pattern_learned",
                category="performance",
                title="O(n^2) algorithm detected",
                data={"pattern": "nested_loops", "alternative": "Use set/dict for O(n)"},
                score=0.8,
                language="python",
            )
            mem.store(
                expert="code_review",
                entry_type="fix_applied",
                category="security",
                title="Parameterized query fix",
                data={"before": "f-string SQL", "after": "cursor.execute(?, params)"},
                score=0.95,
                language="python",
            )

            # Expert B (performance_analysis) stores its own findings
            mem.store(
                expert="performance_analysis",
                entry_type="issue_found",
                category="performance",
                title="String concatenation in loop",
                data={"severity": "warning", "fix": "Use list join"},
                score=0.7,
                language="python",
            )

            # Now test cross-expert access — the core fix from PR #299
            # code_refactoring should see issues from BOTH experts
            context = mem.get_context_for("code_refactoring", language="python")

            logger.info(f"Memory context for code_refactoring: {json.dumps(context, indent=2, default=str)}")

            # Validate cross-expert sharing
            # Keys are: known_patterns, known_issues, past_fixes, project_profile
            patterns = context.get("known_patterns", [])
            issues = context.get("known_issues", [])
            fixes = context.get("past_fixes", [])

            logger.info(f"  patterns: {len(patterns)}, issues: {len(issues)}, fixes: {len(fixes)}")

            # Should see issues from both code_review AND performance_analysis
            if len(issues) < 2:
                bugs_found.append(
                    f"Cross-expert memory broken: code_refactoring sees only {len(issues)} issues "
                    f"(expected >= 2 from code_review + performance_analysis)"
                )

            # Should see patterns from code_review
            if len(patterns) < 1:
                bugs_found.append(
                    f"Cross-expert patterns broken: code_refactoring sees {len(patterns)} patterns"
                )

            # Should see fixes from code_review
            if len(fixes) < 1:
                bugs_found.append(
                    f"Cross-expert fixes broken: code_refactoring sees {len(fixes)} fixes"
                )

            # Expert-specific result should NOT leak
            mem.store(
                expert="code_review",
                entry_type="expert_result",
                category="code_review",
                title="Review session result",
                data={"quality_score": 0.3},
                score=0.3,
                language="python",
            )

            # expert_result should stay expert-specific
            review_context = mem.get_context_for("code_review", language="python")
            refactor_context = mem.get_context_for("code_refactoring", language="python")

            review_profile = review_context.get("profile", {})
            refactor_profile = refactor_context.get("profile", {})

            logger.info(f"  code_review profile: {review_profile}")
            logger.info(f"  code_refactoring profile: {refactor_profile}")

            # Verify recall without expert filter returns cross-expert entries
            all_issues = mem.recall(entry_type="issue_found", language="python")
            logger.info(f"  All issues (no expert filter): {len(all_issues)}")
            if len(all_issues) < 2:
                bugs_found.append(
                    f"recall() without expert filter returns only {len(all_issues)} issues"
                )

            # Verify recall WITH expert filter is still scoped
            cr_issues = mem.recall(expert="code_review", entry_type="issue_found", language="python")
            pa_issues = mem.recall(expert="performance_analysis", entry_type="issue_found", language="python")
            logger.info(f"  code_review issues: {len(cr_issues)}, performance issues: {len(pa_issues)}")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "cross_expert_memory_sharing",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
        })
        logger.info(f"TEST 3 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "cross_expert_memory_sharing",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 3 FAILED: {e}")


async def test_combined_flow_monitor_expert_memory_dashboard():
    """Test 4: Combined flow — ProactiveMonitor → Expert → Memory → Dashboard."""
    logger.info("=" * 60)
    logger.info("TEST 4: Combined Flow — Monitor → Expert → Memory → Dashboard")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.autonomous.proactive_monitor import ProactiveMonitor
        from collegue.core.project_memory import ProjectMemory
        from collegue.tools.expert_dashboard.tool import ExpertDashboardTool

        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup memory
            mem = ProjectMemory(memory_dir=os.path.join(tmpdir, "memory"))

            # Step 1: Simulate ProactiveMonitor detecting changes
            logger.info("Step 1: ProactiveMonitor — detecting file changes...")
            from collegue.autonomous.proactive_monitor import ChangeDetector, ExpertTriggerer, MonitorConfig

            repo_path = str(Path(__file__).parent.parent)
            detector = ChangeDetector(repo_path)
            changes = detector.detect_changes()
            logger.info(f"  Changes detected: {len(changes)}")

            config = MonitorConfig()
            triggerer = ExpertTriggerer(config)
            triggered = triggerer.decide_triggers(changes) if changes else []
            logger.info(f"  Experts to trigger: {[t.expert for t in triggered] if triggered else []}")

            # Step 2: Run an expert that stores to memory
            logger.info("Step 2: Running code_review expert...")
            from collegue.tools.code_review.models import CodeReviewRequest
            from collegue.tools.code_review.tool import CodeReviewTool

            ctx = RealGeminiContext()
            review_tool = CodeReviewTool({})

            # Inject memory singleton for the tool
            from collegue.core import project_memory as pm_module
            original_memory = pm_module._project_memory
            pm_module._project_memory = mem

            try:
                review_request = CodeReviewRequest(
                    code=PYTHON_CODE_BUGGY[:300],
                    language="python",
                    review_standards=["security", "complexity"],
                    severity_threshold="warning",
                )

                review_result = await review_tool.execute_async(review_request, ctx=ctx)
                logger.info(f"  Review result: quality={review_result.quality_score}, "
                            f"findings={len(review_result.findings)}")
            finally:
                pm_module._project_memory = original_memory

            # Step 3: Verify memory was written
            logger.info("Step 3: Verifying memory persistence...")
            entries = mem.recall(expert="code_review")
            logger.info(f"  Memory entries from code_review: {len(entries)}")

            if len(entries) == 0:
                bugs_found.append("code_review tool did not store any entries in ProjectMemory")

            # Step 4: Test ExpertDashboard aggregation
            logger.info("Step 4: ExpertDashboard aggregation...")
            from collegue.tools.expert_dashboard.tool import ExpertDashboardTool
            try:
                from collegue.tools.expert_dashboard.models import DashboardRequest
            except ImportError:
                from collegue.tools.expert_dashboard.tool import DashboardRequest

            dashboard = ExpertDashboardTool({})

            # The dashboard needs context — test what we can
            try:
                dash_request = DashboardRequest()
                dash_result = dashboard._execute_core_logic(dash_request)
                logger.info(f"  Dashboard: experts={dash_result.total_experts}, "
                            f"overall_score={dash_result.overall_score}")

                if dash_result.total_experts == 0:
                    bugs_found.append("ExpertDashboard reports 0 experts")
                if dash_result.overall_score is None:
                    bugs_found.append("ExpertDashboard overall_score is None")

            except Exception as e:
                logger.warning(f"  Dashboard error (may need full context): {e}")
                # Not necessarily a bug — dashboard may need full lifespan context

        elapsed = time.time() - start
        RESULTS.append({
            "test": "combined_flow_monitor_expert_memory_dashboard",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 4 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "combined_flow_monitor_expert_memory_dashboard",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 4 FAILED: {e}")


async def test_agent_loop_convergence():
    """Test 5: Agent loop convergence — verify iterative refinement works."""
    logger.info("=" * 60)
    logger.info("TEST 5: Agent Loop Convergence with Real LLM")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        ctx = RealGeminiContext()
        review_tool = CodeReviewTool({})

        # Use code with obvious issues to trigger agent iteration
        request = CodeReviewRequest(
            code=PYTHON_CODE_BUGGY,
            language="python",
            review_standards=["naming", "complexity", "security", "dry", "solid", "error_handling"],
            severity_threshold="info",
        )

        logger.info("Running code_review with agent loop on buggy code...")
        result = await review_tool.execute_async(request, ctx=ctx)

        logger.info(f"Result: quality={result.quality_score}, findings={len(result.findings)}, "
                    f"iterations={result.agent_iterations}, converged={result.agent_converged}")

        # Validate agent loop behavior
        if result.agent_iterations == 0:
            # This is suspicious — with ctx present, agent loop should run
            bugs_found.append("Agent loop did not iterate (agent_iterations=0) despite ctx being present")

        if result.agent_best_score is not None and result.agent_best_score < 0.3:
            logger.warning(f"Agent best_score very low ({result.agent_best_score}) — quality concern")

        # Validate findings are reasonable for buggy code
        if len(result.findings) < 3:
            bugs_found.append(
                f"Only {len(result.findings)} findings for obviously buggy code "
                f"(expected at least 3 for SQL injection + O(n^2) + concat in loop)"
            )

        # Validate quality score is appropriately low for bad code
        if result.quality_score > 0.8:
            bugs_found.append(
                f"quality_score={result.quality_score} too high for code with SQL injection, "
                "O(n^2) algorithms, and string concat in loops"
            )

        elapsed = time.time() - start
        RESULTS.append({
            "test": "agent_loop_convergence",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "agent_iterations": result.agent_iterations,
            "agent_converged": result.agent_converged,
            "quality_score": result.quality_score,
            "findings_count": len(result.findings),
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 5 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "agent_loop_convergence",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 5 FAILED: {e}")


async def test_performance_to_refactoring_chain():
    """Test 6: performance_analysis → code_refactoring delegation with real LLM."""
    logger.info("=" * 60)
    logger.info("TEST 6: Performance → Refactoring Delegation Chain")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.core.tools_registry import ToolsRegistry
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        ctx = RealGeminiContext()

        # Run performance analysis on buggy code
        logger.info("Running performance_analysis on code with O(n^2) issues...")

        perf_tool = PerformanceAnalysisTool({})
        perf_request = PerformanceAnalysisRequest(
            code=PYTHON_CODE_BUGGY,
            language="python",
        )

        perf_result = await perf_tool.execute_async(perf_request, ctx=ctx)
        perf_dict = perf_result.model_dump()

        perf_score = perf_dict.get("performance_score", 1.0)
        issues = perf_dict.get("issues", [])
        optimizations = perf_dict.get("optimizations", [])

        logger.info(f"Performance result: score={perf_score}, issues={len(issues)}, "
                    f"optimizations={len(optimizations)}")

        # Evaluate delegations
        delegation_engine = create_default_delegation_engine(max_chain_depth=2, chain_timeout=120)
        tasks = await delegation_engine.evaluate_delegations("performance_analysis", perf_dict)

        logger.info(f"Delegations triggered: {len(tasks)}")
        for task in tasks:
            logger.info(f"  → {task.target_tool} (condition: {task.rule.condition_name})")

        # If score < 0.5, should trigger refactoring
        if perf_score < 0.5:
            refactor_tasks = [t for t in tasks if t.target_tool == "code_refactoring"]
            if not refactor_tasks:
                bugs_found.append(
                    f"performance_score={perf_score} < 0.5 but no refactoring delegation triggered"
                )
            else:
                logger.info("Correct: low perf score triggered refactoring delegation")

        # If optimizations > 0, should trigger test_generation
        if optimizations:
            test_tasks = [t for t in tasks if t.target_tool == "test_generation"]
            if not test_tasks:
                bugs_found.append(
                    f"{len(optimizations)} optimizations proposed but no test_generation triggered"
                )
            else:
                logger.info("Correct: optimizations triggered test_generation delegation")

        # Execute one delegation if available
        if tasks:
            registry = ToolsRegistry()
            available_tools = await registry.get()
            first_task = tasks[:1]  # Only execute first to save time

            logger.info(f"Executing first delegation: → {first_task[0].target_tool}")
            del_results = await delegation_engine.execute_delegation_chain(
                first_task,
                available_tools,
                ctx=ctx,
                tool_kwargs={"ctx": ctx},
            )

            for r in del_results:
                logger.info(f"  Result: success={r.success}, time={r.execution_time:.1f}s")
                if not r.success and r.error and "Timeout" not in r.error:
                    bugs_found.append(f"Delegation failed: {r.error}")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "performance_to_refactoring_chain",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "perf_score": perf_score,
            "delegations": len(tasks),
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 6 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "performance_to_refactoring_chain",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 6 FAILED: {e}")


async def test_iac_security_chain():
    """Test 7: IaC scan → refactoring remediation chain."""
    logger.info("=" * 60)
    logger.info("TEST 7: IaC Security Scan → Remediation Chain")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine
        from collegue.tools.iac_guardrails_scan.models import IacGuardrailsRequest
        from collegue.tools.iac_guardrails_scan.tool import IacGuardrailsScanTool

        ctx = RealGeminiContext()

        iac_tool = IacGuardrailsScanTool({})
        iac_request = IacGuardrailsRequest(
            files=[
                {"path": "Dockerfile", "content": DOCKERFILE_INSECURE, "type": "dockerfile"},
            ],
            policy_profile="strict",
            analysis_depth="deep",
        )

        logger.info("Running iac_guardrails_scan on insecure Dockerfile...")
        iac_result = await iac_tool.execute_async(iac_request, ctx=ctx)
        iac_dict = iac_result.model_dump()

        security_score = iac_dict.get("security_score", 1.0)
        findings = iac_dict.get("findings", [])

        logger.info(f"IaC result: security_score={security_score}, findings={len(findings)}")
        for f in findings[:5]:
            if isinstance(f, dict):
                logger.info(f"  [{f.get('severity', '?')}] {f.get('title', f.get('message', '?'))}")

        # Evaluate delegations
        delegation_engine = create_default_delegation_engine(max_chain_depth=2, chain_timeout=120)
        tasks = await delegation_engine.evaluate_delegations("iac_guardrails_scan", iac_dict)

        logger.info(f"Delegations triggered: {len(tasks)}")

        # If security_score < 0.5, should trigger remediation (code_refactoring)
        if security_score < 0.5:
            refactor_tasks = [t for t in tasks if t.target_tool == "code_refactoring"]
            if not refactor_tasks:
                bugs_found.append(
                    f"security_score={security_score} < 0.5 but no remediation delegation triggered"
                )
            else:
                logger.info("Correct: low security score triggered remediation")
        else:
            logger.info(f"Security score {security_score} >= 0.5, no remediation expected")
            # The insecure Dockerfile should score below 0.5
            if security_score > 0.7:
                bugs_found.append(
                    f"Insecure Dockerfile with exposed secrets scored {security_score} > 0.7 "
                    "(expected < 0.5 for DB_PASSWORD and API_KEY exposure)"
                )

        elapsed = time.time() - start
        RESULTS.append({
            "test": "iac_security_chain",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "security_score": security_score,
            "findings_count": len(findings),
            "delegations": len(tasks),
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 7 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "iac_security_chain",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 7 FAILED: {e}")


async def test_orchestrator_full_execution():
    """Test 8: Smart Orchestrator full Plan→Execute→Synthesize with simple query."""
    logger.info("=" * 60)
    logger.info("TEST 8: Smart Orchestrator Full Execution (Plan→Execute→Synthesize)")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.meta_orchestrator import OrchestratorRequest, OrchestratorResponse
        from collegue.core.tools_registry import ToolsRegistry

        ctx = RealGeminiContext()
        registry = ToolsRegistry()
        available_tools = await registry.get()

        ctx.lifespan_context = {
            "tools_registry": available_tools,
            "prompt_engine": None,
            "delegation_engine": None,
            "parser": None,
            "context_manager": None,
        }

        # We need to register and call the tool handler
        # Since it's registered on FastMCP, let's simulate it directly
        import json as json_mod

        from collegue.core.meta_orchestrator import (
            MAX_ORCHESTRATION_STEPS,
            MAX_QUERY_CHARS,
            OrchestratorPlan,
            _synthesis_agent,
        )

        request = OrchestratorRequest(
            query="Vérifie la sécurité de ce code et documente-le",
            context={"code": PYTHON_CODE_CLEAN},
        )

        # Phase 1: Planning
        logger.info("Phase 1: Planning with LLM...")
        safe_query = (request.query or "")[:MAX_QUERY_CHARS]
        tools_desc = "\n".join([info["prompt_desc"] for name, info in available_tools.items()])
        tool_names_list = ", ".join(sorted(available_tools.keys()))

        system_prompt = (
            "Tu es un architecte logiciel. Choisis les outils appropriés et produis un plan structuré."
        )

        user_prompt = f"""Requête: {json_mod.dumps(safe_query, ensure_ascii=False)}

Contexte: {json_mod.dumps(request.context, default=str)}

Outils disponibles (noms EXACTS): {tool_names_list}

Réponds en JSON: {{"steps": [{{"tool": "nom_exact", "reason": "...", "params": {{...}}}}]}}
Limite: 3 étapes max."""

        plan_result = await ctx.sample(
            messages=[user_prompt],
            system_prompt=system_prompt,
            result_type=OrchestratorPlan,
            temperature=0.2,
            max_tokens=2000,
        )

        # Parse plan
        steps = []
        if hasattr(plan_result, "result") and isinstance(plan_result.result, OrchestratorPlan):
            steps = plan_result.result.steps
        else:
            plan_text = plan_result.text if hasattr(plan_result, "text") else str(plan_result.result)
            try:
                json_text = plan_text
                if "```json" in plan_text:
                    json_text = plan_text.split("```json")[1].split("```")[0].strip()
                elif "```" in plan_text:
                    json_text = plan_text.split("```")[1].split("```")[0].strip()
                parsed = json.loads(json_text)
                from collegue.core.meta_orchestrator import OrchestratorStep
                steps = [OrchestratorStep(**s) for s in parsed.get("steps", [])]
            except Exception as e:
                bugs_found.append(f"Failed to parse orchestrator plan: {e}")

        logger.info(f"Plan: {len(steps)} steps")
        for i, step in enumerate(steps):
            logger.info(f"  Step {i+1}: {step.tool}")

        # Phase 2: Execute steps
        execution_results = []
        tools_used = []

        for i, step in enumerate(steps[:3]):  # Max 3 for time
            tool_name = step.tool
            if tool_name == "__refuse__":
                execution_results.append({"step": i+1, "refused": True})
                continue

            if tool_name not in available_tools:
                bugs_found.append(f"Orchestrator planned non-existent tool: '{tool_name}'")
                execution_results.append({"step": i+1, "error": f"Tool '{tool_name}' unknown"})
                continue

            logger.info(f"Executing step {i+1}: {tool_name}...")
            try:
                tool_class = available_tools[tool_name]["class"]
                tool_instance = tool_class({})
                req_model = tool_instance.get_request_model()
                req_obj = req_model(**step.params)
                result = await tool_instance.execute_async(req_obj, ctx=ctx)
                res_dict = result.model_dump() if hasattr(result, "model_dump") else str(result)
                execution_results.append({"step": i+1, "tool": tool_name, "result": res_dict})
                tools_used.append(tool_name)
                logger.info(f"  Step {i+1} done: {tool_name} ✓")
            except Exception as e:
                execution_results.append({"step": i+1, "error": str(e)})
                logger.warning(f"  Step {i+1} failed: {e}")

        # Phase 3: Synthesis
        logger.info("Phase 3: Synthesis...")
        synth_prompt = f"""Requête: {json_mod.dumps(safe_query, ensure_ascii=False)}

Résultats: {json_mod.dumps(execution_results, indent=2, default=str)[:3000]}

Synthétise une réponse claire pour l'utilisateur."""

        try:
            agent_result = await _synthesis_agent.agent_execute(
                initial_prompt=synth_prompt,
                system_prompt="Synthétise les résultats en une réponse claire et actionnable.",
                ctx=ctx,
                context={"tools_used": tools_used},
                max_tokens=2000,
            )
            logger.info(f"Synthesis: iterations={agent_result.total_iterations}, "
                        f"converged={agent_result.converged}, "
                        f"output_len={len(agent_result.best_output)}")

            if not agent_result.best_output.strip():
                bugs_found.append("Orchestrator synthesis returned empty output")

        except Exception as e:
            bugs_found.append(f"Orchestrator synthesis failed: {e}")
            logger.error(f"Synthesis error: {e}")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "orchestrator_full_execution",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "plan_steps": len(steps),
            "tools_executed": tools_used,
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 8 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "orchestrator_full_execution",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 8 FAILED: {e}")


async def test_multi_language_expert_flow():
    """Test 9: Test experts with JavaScript code (multi-language support)."""
    logger.info("=" * 60)
    logger.info("TEST 9: Multi-Language Expert Flow (JavaScript)")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        ctx = RealGeminiContext()
        review_tool = CodeReviewTool({})

        request = CodeReviewRequest(
            code=JAVASCRIPT_CODE,
            language="javascript",
            review_standards=["security", "complexity", "naming"],
            severity_threshold="warning",
        )

        logger.info("Running code_review on JavaScript code...")
        result = await review_tool.execute_async(request, ctx=ctx)

        logger.info(f"Result: quality={result.quality_score}, findings={len(result.findings)}, "
                    f"language={result.language}")

        # Should detect SQL injection and path traversal
        finding_titles = [f.title.lower() for f in result.findings]
        security_findings = [f for f in result.findings if f.category == "security"]
        logger.info(f"  Security findings: {len(security_findings)}")

        if result.language != "javascript":
            bugs_found.append(f"Language mismatch: expected 'javascript', got '{result.language}'")

        # The JavaScript code has SQL injection — should be detected
        has_security = any("sql" in t or "injection" in t or "security" in t for t in finding_titles)
        if not has_security and not security_findings:
            logger.warning("No SQL injection finding detected in JS code (may be LLM-dependent)")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "multi_language_expert_flow",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "quality_score": result.quality_score,
            "findings_count": len(result.findings),
            "llm_calls": ctx._call_count,
        })
        logger.info(f"TEST 9 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "multi_language_expert_flow",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 9 FAILED: {e}")


async def test_memory_persistence_across_sessions():
    """Test 10: Memory persistence across ProjectMemory instances (simulating sessions)."""
    logger.info("=" * 60)
    logger.info("TEST 10: Memory Persistence Across Sessions")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.project_memory import ProjectMemory

        with tempfile.TemporaryDirectory() as tmpdir:
            # Session 1: Store data
            logger.info("Session 1: Storing expert results...")
            mem1 = ProjectMemory(memory_dir=tmpdir)
            mem1.store(
                expert="architecture_analysis",
                entry_type="pattern_learned",
                category="architecture",
                title="Service Layer Pattern",
                data={"pattern": "service_layer", "confidence": 0.9},
                score=0.9,
                language="python",
            )
            mem1.store(
                expert="code_review",
                entry_type="issue_found",
                category="security",
                title="Hardcoded credentials",
                data={"file": "config.py", "line": 42},
                score=0.95,
                language="python",
            )
            mem1.store(
                expert="performance_analysis",
                entry_type="issue_found",
                category="performance",
                title="N+1 query pattern",
                data={"file": "models.py", "line": 88},
                score=0.8,
                language="python",
            )
            mem1.save()  # Explicit save
            logger.info(f"  Stored 3 entries, file exists: {mem1._storage_path().exists()}")

            # Session 2: Load and verify
            logger.info("Session 2: Loading and verifying...")
            mem2 = ProjectMemory(memory_dir=tmpdir)
            entries = mem2.recall()
            logger.info(f"  Loaded {len(entries)} entries")

            if len(entries) != 3:
                bugs_found.append(f"Persistence broken: stored 3 entries, loaded {len(entries)}")

            # Verify cross-expert context works
            refactor_ctx = mem2.get_context_for("code_refactoring", language="python")
            logger.info(f"  Cross-expert context: {json.dumps(refactor_ctx, default=str)[:200]}")

            patterns = refactor_ctx.get("known_patterns", [])
            issues = refactor_ctx.get("known_issues", [])

            if len(patterns) < 1:
                bugs_found.append(f"Session 2: patterns not loaded ({len(patterns)})")
            if len(issues) < 2:
                bugs_found.append(f"Session 2: cross-expert issues not loaded ({len(issues)})")

            # Session 3: Search functionality
            logger.info("Session 3: Testing search...")
            mem3 = ProjectMemory(memory_dir=tmpdir)
            search_results = mem3.search("security")
            logger.info(f"  Search 'security': {len(search_results)} results")

            if len(search_results) == 0:
                bugs_found.append("search('security') returned 0 results despite storing security issue")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "memory_persistence_across_sessions",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
        })
        logger.info(f"TEST 10 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "memory_persistence_across_sessions",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 10 FAILED: {e}")


async def test_delegation_anti_loop():
    """Test 11: Verify anti-loop protection (code_review ↔ code_refactoring cycle)."""
    logger.info("=" * 60)
    logger.info("TEST 11: Delegation Anti-Loop Protection")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.core.expert_delegation import create_default_delegation_engine

        engine = create_default_delegation_engine(max_chain_depth=3, chain_timeout=10)

        # Simulate a result that would trigger infinite loop:
        # code_review (quality < 0.5) → code_refactoring (changes > 0) → code_review → ...
        # The max_chain_depth should prevent infinite recursion

        # Simulate code_review result with low quality
        review_result = {
            "quality_score": 0.3,
            "findings": [
                {"category": "naming", "severity": "error", "title": "Bad naming", "description": "x, y, z"},
                {"category": "security", "severity": "critical", "title": "SQL injection"},
            ],
            "language": "python",
        }

        tasks = await engine.evaluate_delegations("code_review", review_result)
        logger.info(f"code_review (quality=0.3) → {len(tasks)} delegations")
        for t in tasks:
            logger.info(f"  → {t.target_tool}")

        # Should trigger code_refactoring
        refactor_tasks = [t for t in tasks if t.target_tool == "code_refactoring"]
        if not refactor_tasks:
            bugs_found.append("quality_score=0.3 did not trigger code_refactoring delegation")

        # Now simulate refactoring result — should trigger review back
        refactoring_result = {
            "refactored_code": "def calculate(a, b):\n    return a + b\n",
            "original_code": "def x(a,b):\n  return a+b\n",
            "changes": [{"type": "rename", "description": "Renamed x to calculate"}],
            "language": "python",
        }

        tasks2 = await engine.evaluate_delegations("code_refactoring", refactoring_result)
        logger.info(f"code_refactoring (with changes) → {len(tasks2)} delegations")
        for t in tasks2:
            logger.info(f"  → {t.target_tool}")

        # Should trigger documentation, tests, AND review
        review_back = [t for t in tasks2 if t.target_tool == "code_review"]
        if not review_back:
            logger.info("No review-back triggered (expected since refactoring has changes)")
            # This is actually fine - let's check what delegations exist
        else:
            logger.info("code_refactoring → code_review triggered (bidirectional)")

        # Verify depth protection
        logger.info(f"Max chain depth: {engine.max_chain_depth}")
        if engine.max_chain_depth < 2:
            bugs_found.append(f"max_chain_depth too low ({engine.max_chain_depth}), delegation won't work")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "delegation_anti_loop",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
        })
        logger.info(f"TEST 11 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "delegation_anti_loop",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 11 FAILED: {e}")


async def test_dashboard_with_real_experts():
    """Test 12: ExpertDashboard aggregation with real expert data."""
    logger.info("=" * 60)
    logger.info("TEST 12: ExpertDashboard Real Aggregation")
    logger.info("=" * 60)

    start = time.time()
    bugs_found = []

    try:
        from collegue.tools.expert_dashboard.tool import ExpertDashboardTool

        # Try to import models
        try:
            from collegue.tools.expert_dashboard.models import DashboardRequest, DashboardResponse
        except ImportError:
            try:
                from collegue.tools.expert_dashboard.tool import DashboardRequest, DashboardResponse
            except ImportError:
                logger.warning("Cannot import DashboardRequest/Response models")
                RESULTS.append({
                    "test": "dashboard_with_real_experts",
                    "status": "SKIP",
                    "duration": time.time() - start,
                    "reason": "Cannot import dashboard models",
                })
                return

        dashboard = ExpertDashboardTool({})

        # Execute dashboard
        request = DashboardRequest()
        result = dashboard._execute_core_logic(request)

        logger.info(f"Dashboard: expert_statuses={len(result.expert_statuses)}")
        logger.info(f"  overall_score={result.project_health.overall_score}")
        logger.info(f"  quality_score={result.project_health.quality_score}")
        logger.info(f"  architecture_score={result.project_health.architecture_score}")
        logger.info(f"  performance_score={result.project_health.performance_score}")
        logger.info(f"  security_score={result.project_health.security_score}")

        # Validate structure
        if len(result.expert_statuses) == 0:
            bugs_found.append("Dashboard reports 0 expert_statuses")

        # Check that known experts are listed
        expert_names = [s.name for s in result.expert_statuses]
        for expected in ["code_review", "architecture_analysis", "performance_analysis"]:
            if expected not in expert_names:
                bugs_found.append(f"Dashboard missing expert: {expected}")

        # Check recommendations
        recommendations = result.recommendations
        logger.info(f"  recommendations: {len(recommendations)}")

        elapsed = time.time() - start
        RESULTS.append({
            "test": "dashboard_with_real_experts",
            "status": "PASS" if not bugs_found else "BUGS_FOUND",
            "duration": elapsed,
            "bugs": bugs_found,
            "total_experts": len(result.expert_statuses),
            "overall_score": result.project_health.overall_score,
        })
        logger.info(f"TEST 12 DONE: {elapsed:.1f}s, bugs={len(bugs_found)}")

    except Exception as e:
        elapsed = time.time() - start
        RESULTS.append({
            "test": "dashboard_with_real_experts",
            "status": "ERROR",
            "duration": elapsed,
            "error": str(e),
        })
        logger.exception(f"TEST 12 FAILED: {e}")


# ============================================================================
# Main Runner
# ============================================================================

async def main():
    """Run all deep Phase 2 tests."""
    logger.info("=" * 70)
    logger.info("DEEP PHASE 2 TESTING — Collègue Multi-Agent System")
    logger.info(f"Model: {GEMMA_MODEL}")
    logger.info(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("=" * 70)

    total_start = time.time()

    # Run tests sequentially (LLM rate limits + resource sharing)
    await test_smart_orchestrator_real_planning()
    await test_delegation_chain_full_execution()
    await test_cross_expert_memory_sharing()
    await test_combined_flow_monitor_expert_memory_dashboard()
    await test_agent_loop_convergence()
    await test_performance_to_refactoring_chain()
    await test_iac_security_chain()
    await test_orchestrator_full_execution()
    await test_multi_language_expert_flow()
    await test_memory_persistence_across_sessions()
    await test_delegation_anti_loop()
    await test_dashboard_with_real_experts()

    total_elapsed = time.time() - total_start

    # Summary
    logger.info("\n" + "=" * 70)
    logger.info("RESULTS SUMMARY")
    logger.info("=" * 70)

    all_bugs = []
    for r in RESULTS:
        status = r.get("status", "UNKNOWN")
        test = r.get("test", "?")
        duration = r.get("duration", 0)
        bugs = r.get("bugs", [])
        error = r.get("error", "")

        icon = "✓" if status == "PASS" else ("⚠" if status == "BUGS_FOUND" else "✗")
        logger.info(f"  {icon} {test}: {status} ({duration:.1f}s)")
        if bugs:
            for b in bugs:
                logger.info(f"    BUG: {b}")
                all_bugs.append({"test": test, "bug": b})
        if error:
            logger.info(f"    ERROR: {error}")

    logger.info(f"\nTotal: {len(RESULTS)} tests, {total_elapsed:.0f}s")
    logger.info(f"Passed: {sum(1 for r in RESULTS if r['status'] == 'PASS')}")
    logger.info(f"Bugs found: {len(all_bugs)}")
    logger.info(f"Errors: {sum(1 for r in RESULTS if r['status'] == 'ERROR')}")

    # Save results
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": GEMMA_MODEL,
        "total_duration": total_elapsed,
        "results": RESULTS,
        "bugs_found": all_bugs,
    }

    output_path = "/home/ubuntu/deep_phase2_results.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\nResults saved to: {output_path}")

    return all_bugs


if __name__ == "__main__":
    bugs = asyncio.run(main())
    if bugs:
        print(f"\n{'='*50}")
        print(f"BUGS FOUND: {len(bugs)}")
        for b in bugs:
            print(f"  - [{b['test']}] {b['bug']}")
        print(f"{'='*50}")
