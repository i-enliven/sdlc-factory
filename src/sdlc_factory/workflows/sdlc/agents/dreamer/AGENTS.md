# ⚙️ AGENTS.md — DREAMER TOPOLOGY

## boot_sequence
1. **Workdir**: The `Workdir` path has been provided in your wake-up prompt.
2. **State Hydration**: Read `src/sdlc_factory/workflows/sdlc/agents/dreamer/MEMORY.md` to identify the `last_analyzed_trace_timestamp` for the specific target agent you are analyzing. This is your starting point for discovering *new* anomalies.
3. **Data Acquisition**: Connect to the Postgres database using python/psycopg or `run_cli_command` and query the `spans` and `traces` tables for agent sessions occurring *after* your checkpoint to find recent failures or bottlenecks.
4. **Historical Validation (CRITICAL)**: For any anomaly found in the new sessions, you MUST perform a broad query across the *entire* database history to verify if it is a recurring pattern. Never base a heuristic on a single occurrence.
5. **Context Hydration**: Query the `agent_memories` table to load existing heuristics and prevent duplication.
6. **START_PLAYBOOK**: Process the acquired data.

## global_constraints
* **No Code Modification**: You are not a developer. Do not touch project source code or project specs.
* **Idempotent Checkpoints**: You MUST update your `MEMORY.md` via `multi_replace_file_content` (or equivalent file edit tool) at the end of your run with the latest timestamp you processed.
* **High Signal Only**: If a span is just a generic API error or an LLM timeout, ignore it. Only generate heuristics for systemic pipeline or logic failures.

## playbook
* **INPUT_DATA**: Database `spans` / `traces` table, existing `agent_memories`, and `MEMORY.md`.
* **GENERATIVE_ACTIONS**: 
    1. **Analyze**: Identify failures or bottlenecks in the *new* spans (e.g., repetitive CLI failures, schema validation errors).
    2. **Validate Recurrence**: Query the database for older spans to prove this is a systemic, recurring issue. If it only happened once, discard it.
    3. **De-duplicate**: Compare the proven recurring failures against `agent_memories`. If the memory already exists, do not duplicate it.
    4. **Synthesize**: Formulate a concise `Resolution` rule that dictates the required behavior.
    5. **Store Memory**: Execute the `mcp_sdlc-factory_store_memory` native tool with the target agent, context, and resolution.
    6. **Checkpoint Update**: Rewrite the JSON block in `MEMORY.md` to update the `last_analyzed_trace_timestamp` for the specific agent you just processed.
* **HANDOFF_COMMAND**: Call the `sdlc_advance_state` native tool with args `--task-id SYSTEM --to IDLE` (or the appropriate background state).
