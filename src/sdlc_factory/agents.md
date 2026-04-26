# SDLC Factory Source Architecture Sandbox

## Overview
This defines the structural map of the SDLC core systems. This factory is heavily modularized to maintain the Single Responsibility Principle, ensuring discrete boundaries between state management, GenAI synthesis, memory context, and external execution. 

## Component File Map
### 1. `utils.py` (Core Foundation)
Hosts fundamental definitions that all other modules rely on.
- **Components**: `SCHEMAS` (The rigid JSON handoff contracts), `global_logger` configurations, configuration loaders (`get_config()`), and direct disk operations (`read_json`, `workspace_root` routing).

### 2. `db.py` (Persistence Layer)
Handles all remote and explicit memory infrastructure.
- **Components**: `psycopg` connection mapping, HuggingFace `sentence_transformers` models, and `pgvector` integrations (`get_db_connection`, `get_embedding`).

### 3. `state.py` (The Lifecycle Engine)
The beating heart of the SDLC state machine.
- **Components**: Execution of `validate_handoff()` to rigorously check outputs against `utils.py` JSON schemas. Evaluates and bounds error states via `handle_regression()`. Consolidates independent modular agent deployments dynamically via `scatter_architecture` and `gather_modules`.

### 4. `memory.py` (Context Injection)
The vector-semantic logic pipeline interacting with `db.py`.
- **Components**: The `index_codebase` batch worker, dynamic pgvector distance searching (`do_search_codebase`, `do_store_memory`), and the critical `build_context` bridge which parses `API_CONTRACTS.md` to feed structural dependencies to active LLM prompts.

### 5. `agent.py` (Generative AI Runtime)
The autonomous orchestration layer spanning Google GenAI architectures.
- **Components**: The `execute_agent()` subprocess containing `Google GenAI SDK` chat sessions natively. Intercepts GenAI functional schema calls and routes them directly to native endpoints natively bypassing Bash. 

### 6. `cli.py` & `main.py` (Interface Layer)
Strictly the terminal routers.
- **Components**: Wraps the backend via `Typer`. Includes `@app.command()` wrappers that parse string flags and emit terminal printouts. `main.py` serves strictly as a 5-line invocation bootloader.

### 7. `mcp_server.py` (The External Protocol)
The Model Context Protocol hook. 
- **Components**: Converts the underlying pipeline logic from `state.py` and `memory.py` into native `FastMCP` wrappers (`context`, `advance_state`, `store_memory`). This is used universally across the Antigravity proxy network to communicate with the factory backend flawlessly. 

## Agent Rules
- **State Modifications**: Attempting to bypass `state.py` manually to overwrite `.state/current.json` is strictly forbidden. Use the `advance_state` MCP/CLI tools natively so JSON schema compliance kicks in.
- **Context Fallbacks**: If `memory.py` fails to resolve a semantic match, it yields graceful fallbacks. Never enforce hard crash errors if semantic searches come back empty.
- **Internal CLI Hooks**: Favor native execution loops over baseline Bash text pipelines whenever possible. If calling from within GenAI tools, invoke `sdlc-factory` MCP points.

## General Workflow
1. Identify target module. 
2. Retain pure logic separations (do not mix Typer terminal printouts into db connections, for example).
3. Validate state boundaries on every change via rigid `validate_handoff()` JSON typing.
