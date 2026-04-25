# SDLC Factory 🏭

**sdlc-factory** is a sophisticated, headless AI engineering team designed to automate the full **Software Development Life Cycle (SDLC)**. 

By utilizing the **OpenClaw agentic pattern**—a modern framework where agent behavior is strictly governed by transparent Markdown "constitutions" rather than hardcoded prompts—this system transforms software engineering into a highly reproducible and configurable pipeline.

---

## 🚀 Getting Started

Ensure you have [uv](https://github.com/astral-sh/uv) installed for rapid Python package management.

### 1. Installation
Navigate to the `cli/` directory to sync dependencies and bind the `sdlc-factory` CLI:

```bash
cd cli
uv sync

# Optional: Bind the CLI globally to your system path
uv pip install -e .
```

### 2. Infrastructure Initialization
The factory relies on Postgres (pgvector) for long-term insight persistence and Arize Phoenix for OpenTelemetry tracing. Bring up the required Docker containers:

```bash
docker compose up -d
```

---

## 🛠️ Configuration & Tracing

To connect the CLI with the framework components and external LLM models, create a configuration file at `~/.sdlc-factory.json`. This file acts as the master manifest for database bounds, pathing, and GenAI observability.

> [!NOTE]
> The internal Markdown protocols (`SOUL.md` and `AGENTS.md`) and state machine structures have been heavily tuned specifically for the **Gemini** family of models (e.g., `gemini-3.1-pro-preview` and `gemini-3-flash-preview`). While you can technically swap the backend model via the configuration layer, reasoning and protocol adherence are optimized exclusively for Google's GenAI endpoints.

### Native `~/.sdlc-factory.json` Example
```json
{
  "workspace_root": "path/to/sdlc-factory/workspace",
  "agents_root": "path/to/sdlc-factory/agents",
  "connection_string": "dbname=factory-dev user=postgres password=secret host=0.0.0.0",
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "vertex_api_key": "YOUR_VERTEX_API_KEY",
  "hf_token": "YOUR_HF_TOKEN",
  "max_retry_limit": 5,
  "cli_command_timeout": 300,
  "models": {
    "planner": {
      "model": "gemini-3.1-pro-preview",
      "temperature": 0.2,
      "max_iterations": 60
    },
    "architect": {
      "model": "gemini-3.1-pro-preview",
      "temperature": 0.0,
      "max_iterations": 60
    },
    "tester": {
      "model": "gemini-3.1-pro-preview",
      "temperature": 0.0,
      "max_iterations": 90
    },
    "coder": {
      "model": "gemini-3.1-pro-preview",
      "temperature": 0.0,
      "max_iterations": 120
    },
    "deployer": {
      "model": "gemini-3-flash-preview",
      "temperature": 0.0,
      "max_iterations": 60
    },
    "monitor": {
      "model": "gemini-3-flash-preview",
      "temperature": 0.0,
      "max_iterations": 60
    }
  },
  "tracing_enabled": true,
  "tracing_endpoint": "http://127.0.0.1:4317"
}
```

### Telemetry (Arize Phoenix)
The factory includes native **OTLP telemetry integration** for full visibility into LLM node executions, token usage, and subagent latency. 
* Set `"tracing_enabled": true` to log OpenTelemetry traces for LangChain API calls.
* Navigate to [`http://127.0.0.1:6006`](http://127.0.0.1:6006) to interact with the live trace UI payload! (Gracefully drops telemetry if the container is offline).

---

## 💻 Common Workflows & Use Cases

### 1. Initialization
Provision a dedicated workspace with structural directories (`.state`, `handoff`, `src`, etc.) ready for the Planner agent:
```bash
sdlc-factory init --task-id "TICKET-123"
```

### 2. Autonomous Factory Runner
To begin processing queues and firing agents autonomously across the ledger, start the heartbeat daemon:
```bash
sdlc-factory run
```
*(You can run a single manual pulse using `sdlc-factory heartbeat`)*

### 3. Ad-Hoc Agent Execution
Execute a specific agent context manually without triggering the strict state-advancing pipeline. Using standard Unix termination (`Ctrl+D`), you can safely pass multi-line ad-hoc prompts cleanly into the active topologies:
```bash
sdlc-factory task --agent coder
```

### 4. Human-in-the-Loop Override
If an agent is executing a command that you want to intercept (e.g., it is trapped in a loop or writing incorrect code), you can press `Ctrl+C` during its run. The factory will safely pause execution after the current tool finishes, allowing you to inject a direct conversational override. 
- Press `Ctrl+C` once to trigger the human override prompt.
- Enter your instructions, and they will be injected directly into the agent's context window.
- If you change your mind, press `Ctrl+D` at the override prompt to seamlessly resume execution without interference.

### 5. Resuming a Paused Session
Agent histories are serialized and saved automatically. If you hard-abort a run or wish to continue a specific task from a previous state, you can resume it using its unique session ID (found in your `agents/sessions/` directory or trace logs):
```bash
sdlc-factory heartbeat --resume <UUID>
```
*(This also works with `run` and `task` commands).*

### 6. Diagnostic Chat Mode
To interrogate an agent's reasoning without altering its codebase or corrupting its active session state, use the read-only chat mode. This disables the agent's file modification tools while retaining its entire memory and context for that session.
```bash
sdlc-factory chat --session-id <UUID>
```
*(You can explicitly tell the agent to save insights during chat, which will route through the isolated `sdlc_store_memory` vector tool).*

### 7. Triggering a Pipeline Regression
If an agent detects a critical failure from an upstream worker (e.g., Code fails to compile, or tests fail), they generate a `regression_report.json` and invoke a state swap rollback to immediately assign the fracture to the previous worker.

**Example `handoff/regression_report.json`:**
```json
{
  "status": "success",
  "indictment_metadata": {
    "source_phase": "QA_REVIEW",
    "target_phase": "CODING",
    "error_category": "LOGIC_MISMATCH"
  },
  "diagnostic_trace": {
    "observed_behavior": "AssertionError: expected '2.0000' but got '2.0'"
  }
}
```
**Triggering the Rollback:**
```bash
sdlc-factory advance-state --task-id "TICKET-123" --to CODING --regression
```

---
### 8. Testing Code Quality
Verify the framework's robustness by running the test suite with coverage reporting.
```bash
cd cli
uv run pytest tests/ --cov=src/sdlc_factory --cov-report=term-missing
```


## 🧠 The OpenClaw Protocol ("Soul" vs. "Execution")

The core methodology of SDLC Factory separates an agent’s identity from its technical capabilities using plain-text Markdown protocols:
1. **`SOUL.md`**: Acts as the agent's **Constitution**. It defines the persona, ethical boundaries, decision-making frameworks, and communication style. 
2. **`AGENTS.md`**: Acts as the **Operational Manual**. It defines the core identity constraints and macro tasks the persona manages.
3. **`HEARTBEAT.md`**: Controls the rigorous **State Machine Transitions** and phase-loops required during autonomous workflow operations.
4. **`PROTOCOL.md`**: The global rules of engagement. Defines the decentralized, event-driven methodology, directory structures, and boundary limits of the shared task environments.
5. **`SKILL.md`**: Documented mappings of natively bound Python tools allowing an agent to fetch dynamic localized context, execute state progressions, or interact directly with the Postgres semantic memory store.

By utilizing Markdown files, the `sdlc-factory` allows you to reprogram your engineering team's entire standards culture safely in plain text without touching the internal Python runtime!

---

## 👥 The Headless Engineering Team

Responsibilities are distributed across six highly specialized roles:

| Role | Responsibility | Key Objective |
| :--- | :--- | :--- |
| **Planner** | Task Roadmapping | Translates raw business requirements into actionable tickets. |
| **Architect** | System Standards | Defines the tech stack, data models, and API boundary contracts. |
| **Coder** | Implementation | Writes clean, testable code strictly following Architect designs. |
| **Tester** | Quality Assurance | Creates unit, integration, and E2E tests to validate the Coder. |
| **Deployer** | CI/CD Engineering | Manages heuristics and packaging for `docker` artifact delivery. |
| **Monitor** | Observability | Sandbox validation, endpoint polls, and pipeline audits. |

---

## ⚙️ Technical Engine (Python CLI)

Under the hood, the Python-based execution engine (`cli/src/sdlc_factory/`) drives the deterministic pipelines without relying on heavy third-party agent wrappers:
* **`cli.py` & `heartbeat.py`**: The core daemon orchestrators. They expose the user endpoints, load pending queues from the state ledger, enforce task blockages, and route tasks to agents.
* **`agent.py`**: The dynamic LLM integration layer. Handles parallel tool execution, contextual human-in-the-loop overrides (`Ctrl+C`), and serializes agent history to `.session` files for deterministic resumption.
* **`chat.py`**: A read-only diagnostic REPL. Safely loads historic `.session` payloads into the LLM while disabling file modification tooling to prevent state corruption during operator interrogation.
* **`state.py`**: A strict JSON Schema validation engine executing rigorous handoffs between phases (e.g. Deployer cannot act until the Tester submits a properly signed JSON schema).
* **`telemetry.py`**: Automatically wraps generative execution via OpenTelemetry, streaming logs natively to Arize Phoenix.
* **`tools.py`**: Houses the strict native capability scripts (like `advance_state` and `search_codebase`) that are systematically injected into the LLM during runtime.
* **`memory.py` & `db.py`**: Natively provisions **persistent semantic vector memory** using Postgres `pgvector`—allowing agents to embed historical bug reports and retrieve them via cosine-similarity metrics without hallucinating.
