# Collègue MCP

> 🇫🇷 [Version française](README.md) | 🇬🇧 **English version**

[![Tests](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml/badge.svg)](https://github.com/VynoDePal/Collegue/actions/workflows/tests.yml)

A **collective of specialized AI experts** as an MCP (Model Context Protocol) server. Each tool is an expert agent in its domain — code analysis, refactoring, testing, security, architecture — and they collaborate through automatic delegation, persistent memory, and proactive monitoring.

---

## 🚀 Quick Start (Docker)

```bash
git clone https://github.com/VynoDePal/Collegue.git
cd Collegue
cp .env.example .env   # set LLM_API_KEY (Gemini)
docker compose up -d
```

Endpoints:

| URL | Role |
|-----|------|
| `http://localhost:4121/mcp/` | MCP Server (HTTP transport) |
| `http://localhost:4122/_health` | Healthcheck |

### Configure your IDE

#### Claude Code (CLI)

```bash
claude mcp add --transport http collegue http://localhost:4121/mcp/
```

#### Windsurf / Cursor / Antigravity

```json
{
  "mcpServers": {
    "collegue": {
      "serverUrl": "http://localhost:4121/mcp/"
    }
  }
}
```

#### Claude Desktop

```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:4121/mcp/"]
    }
  }
}
```

### stdio mode (on-the-fly container)

```json
{
  "mcpServers": {
    "collegue": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "LLM_API_KEY=your_gemini_key",
        "collegue-mcp"
      ]
    }
  }
}
```

> Build image locally: `docker build -f docker/collegue/Dockerfile -t collegue-mcp .`

---

## ✨ 10 AI Experts

Each expert uses an LLM, iterates through an **agentic loop**, and can **delegate** to other experts.

| Expert | Description |
|--------|-------------|
| **Code Review** | Quality, naming, complexity, security, DRY, SOLID |
| **Architecture Analysis** | Patterns, dependencies, cycles, coupling, technical debt |
| **Performance Analysis** | O(n²), blocking I/O, loop concatenation, hotspots |
| **Code Refactoring** | Restructure, optimize, AST validation, metrics comparison |
| **Test Generation** | Executable unit tests (pytest, jest, phpunit) |
| **Code Documentation** | Docstrings, technical documentation, coverage |
| **IaC Guardrails Scan** | Security for Terraform, Kubernetes, Dockerfile |
| **Impact Analysis** | Predictive risk analysis before code changes |
| **Repo Consistency Check** | Unused imports, dead code, duplication |
| **Smart Orchestrator** | Plans and coordinates multiple experts |

### Additional tools

| Category | Tools |
|----------|-------|
| **Static** | Dependency Guard, Secret Scan, Run Tests |
| **Integrations** | PostgreSQL, GitHub, Sentry, Kubernetes |

---

## 🤖 Multi-Agent System

```
┌─────────────────────────────────────────────────────┐
│                 Collègue MCP Server                   │
│                                                     │
│  Code Review ─── Architecture ─── Performance       │
│       │               │               │             │
│  Refactoring ─── Test Gen ─── Documentation         │
│       │               │               │             │
│  IaC Scan ─── Consistency ─── Impact Analysis       │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │ Delegation · Memory · Monitor · Dashboard    │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

| Component | Role |
|-----------|------|
| **Agentic Loop** | Execute → validate → correct → re-execute until convergence |
| **Delegation** | 14 automatic rules (e.g., `code_review` → `refactoring` if score < 0.5) |
| **Memory** | Stores results in `.collegue/memory/` for future sessions |
| **Monitor** | Detects modified files and triggers relevant experts |
| **Dashboard** | Aggregates project health scores |

---

## 🔑 Configuration

### Environment variables (.env)

| Variable | Description | Required |
|----------|-------------|----------|
| `LLM_API_KEY` | Google Gemini API key | ✓ |
| `POSTGRES_URL` | PostgreSQL URI | |
| `GITHUB_TOKEN` | GitHub token (repo, read:org) | |
| `SENTRY_AUTH_TOKEN` | Sentry auth token | |
| `SENTRY_ORG` | Sentry organization slug | |
| `KUBECONFIG` | Path to kubeconfig | |
| `STATE_DATABASE_URL` | Autonomous engine durable state (Postgres/SQLite) | |
| `MAX_COST_USD` / `MAX_TOKENS_BUDGET` | Hard budget (auto-pause) | |
| `AUTO_MERGE_ENABLED` / `PILOT_TOOL_ENABLED` | Autonomous capabilities (opt-in, **off** by default) | |

> Full autonomous-engine settings (budget, auto-merge/revert, pilot MCP tool):
> [docs/moteur_autonome.md](docs/moteur_autonome.md#réglages-env) (FR).

---

## 🧭 Autonomous Development Engine

Beyond the **reactive** experts, Collègue can drive end-to-end development:
**plan → code → test → open PRs**, under a budget, with GitHub as the substrate.
Stages: `planner` → `pilote` → `executor` → `improve`, on a durable-state
(Postgres/SQLite) + Docker-sandbox foundation.

**Safe by default**: `dry_run` (no writes) until you pass `--execute`; **no merge to
`main` without a human** (§6); hard auto-paused budget; auto-merge, auto-revert and the
pilot MCP tool are **off by default** and fail-closed.

```bash
# Preview (dry_run), then real execution
python -m collegue.pilot --project-id 1 --repo-source /path/clone --owner org --repo app
python -m collegue.pilot ... --execute            # real writes (PRs + state)
python -m collegue.pilot ... --execute --improve  # + improvement cycle
```

Once the MVP is built, `--improve` chains the **continuous-improvement loop** (Phase 4):
a **deterministic quality objective** (coverage − security − lint − complexity, no LLM
judgment) opens PRs only when a diff **improves without regression** (fail-closed gate);
PRs are **stacked** and stop at a plateau.

Architecture, improvement loop, guardrails, observability/audit, crash resume and
settings: **[docs/moteur_autonome.md](docs/moteur_autonome.md)** (FR).

---

## 🤖 Watchdog Agent (Self-Healing)

Monitors Sentry and auto-generates GitHub PRs to fix errors.

```
Sentry (errors) → Watchdog (analysis) → LLM (fix) → GitHub (PR)
```

See [docs/watchdog_deployment.md](docs/watchdog_deployment.md) for deployment.

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [User Guide](docs/guide_utilisateur.md) | Installation, configuration, first steps, best practices |
| [Integration Guide](docs/guide_integration.md) | Claude Desktop, Cursor, Windsurf, CI/CD integration |
| [Expert Reference](docs/reference_experts.md) | Parameters, outputs and use cases for each expert |
| [Multi-Agent System](docs/multi_agent_expert_system.md) | Technical architecture, delegation, memory |
| [Autonomous Development Engine](docs/moteur_autonome.md) | Autonomous pilot: architecture, **continuous improvement (Phase 4)**, guardrails, audit, resume, settings (FR) |
| [LLM Evaluations](docs/llm_evals.md) | Output quality benchmarks |
| [Rate Limiting](docs/rate_limiting_and_quotas.md) | Quotas and limits |

---

## 🛠️ Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m collegue.app
```

Tests:

```bash
python -m pytest --tb=short -q
ruff check collegue tests
```
