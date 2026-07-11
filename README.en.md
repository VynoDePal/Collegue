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

Overview **by theme** (full list and default values in
**[.env.example](.env.example)**):

| Variable(s) | Description | Required |
|-------------|-------------|----------|
| `LLM_API_KEY` | LLM provider API key (Gemini by default) | ✓ |
| `LLM_PROVIDER` / `LLM_MODEL` | Default LLM provider and model | |
| `LLM_MODEL_*` / `LLM_PROVIDER_*` | Per-**role** model/provider (CODER, QA, PLANNER, REVIEWER) | |
| `LLM_RATE_LIMIT_*` | Per-client LLM call limits (per minute / day) | |
| `CACHE_ENABLED` / `CACHE_TTL` | Tool response cache | |
| `OAUTH_ENABLED` (+ `OAUTH_*`, Keycloak) | OAuth authentication (**off** by default) | |
| `GITHUB_TOKEN` / `GITHUB_OWNER` / `GITHUB_REPO` | GitHub integration (watchdog, PRs) | |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Sentry observability | |
| `STATE_DATABASE_URL` | Autonomous engine durable state (Postgres/SQLite) | |
| `MAX_COST_USD` / `MAX_TOKENS_BUDGET` / `COLLEGUE_RUN_DEADLINE_SECONDS` | Hard run budget (auto-pause) | |
| `COLLEGUE_HOME` | Persistence root (budget, metrics, checkpoints) | |
| `CODER_SUBSCRIPTION` (+ `CODER_SUBSCRIPTION_MODEL`, `SANDBOX_SUBSCRIPTION_AUTH_DIR`) | Code via a ChatGPT/Codex **subscription** (`$0` API cost) instead of an API key | |
| `BUILD_AUTO_MERGE` | **Build-phase merge-bot** (auto-merges task PRs; **on** by default). Improvement stays human-merge | |
| `SANDBOX_NETWORK` / `SANDBOX_MEMORY` / `SANDBOX_CPUS` / `SANDBOX_TIMEOUT` | Coder container network and resources | |
| `AUTO_MERGE_ENABLED` / `AUTO_REVERT_ENABLED` / `PILOT_TOOL_ENABLED` | Risk-gated autonomous capabilities (opt-in, **off** by default) | |

> Detailed autonomous-engine settings (budget, auto-merge/revert, pilot MCP tool):
> [docs/moteur_autonome.md](docs/moteur_autonome.md#réglages-env) (FR).

---

## 🧭 Autonomous Development Engine

Beyond the **reactive** experts, Collègue can drive end-to-end development:
**plan → code → test → open PRs**, under a budget, with GitHub as the substrate.
Stages: `planner` → `pilote` → `executor` → `improve`, on a durable-state
(Postgres/SQLite) + Docker-sandbox foundation.

**Safe by default**: a run stays in `dry_run` until you pass `--execute`.
`plan draft` only persists its durable draft; the operator then approves the
displayed hash, and only `plan sync --execute` writes to GitHub. The hard budget
auto-pauses the engine. In a real BUILD, a **merge-bot** auto-merges each task to
construct the MVP (`BUILD_AUTO_MERGE`, on by default); the **improvement** phase
leaves its PRs **open for human merge** (§6) by default. Phase 5 risk-gated
auto-merge is wired but remains opt-in: complete CI, a stable SHA, base resync and
post-merge health are mandatory. Auto-revert and the pilot MCP tool stay **off by
default** and fail-closed. The coder can run via a
ChatGPT/Codex **subscription** (`$0` API cost).

```bash
# Phase 1: three separate actions — the LLM process never approves itself
python -m collegue.pilot plan draft --name app --problem "..." --owner org --repo app --base main
python -m collegue.pilot plan approve --project-id 1 --expected-plan-hash DISPLAYED_SHA256
python -m collegue.pilot plan sync --project-id 1 --execute

# Build: preview (dry_run), then real execution
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
