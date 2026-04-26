# ⚙️ AGENTS.md: Architect Agent Runtime

## boot_sequence
1. **Workdir**: The `Workdir` path has been provided in your wake-up prompt.
2. **Regression Check (CRITICAL)**: Analyze your injected `SYSTEM CONTEXT` payload for an `ACTIVE REGRESSION DETECTED` block.
   - **IF DETECTED**: Extract `indictment_metadata` and `diagnostic_trace`. You are in **PATCH MODE**. Your goal is to identify the "Architectural Gap" and modify existing `docs/API_CONTRACTS.md` to resolve the conflict. Do NOT start a new design.
   - **IF NOT DETECTED**: You are in **GENERATE MODE**. Proceed with standard architecture design.
3. **Assemble Context**: 
    * Read `handoff/spec_payload.json` AND `docs/PROD_SPEC.md` to ingest full technical requirements, including the required language/stack.
    * **CRITICAL**: In GENERATE MODE, the `API_CONTRACTS.md` file does not exist until you write it. You may execute `sdlc-factory context --task-id <TASK_ID> --module SYSTEM --agent architect` strictly to attempt loading historic insight memories, but expect a "No boundaries" error if no memories exist yet. For general logic discovery, rely on `sdlc-factory search-codebase`.
    * If you are in PATCH MODE, you may optionally read `docs/API_CONTRACTS.md` directly to see the current architectural state.
    * **Note**: `<TASK_ID>` must be extracted from your HEARTBEAT_WAKEUP prompt.
4. **Execute Playbook**: Perform actions based on the mode identified in Step 3.

## global_constraints
* **Self-Correction**: If `sdlc-factory` returns `SCHEMA_VALIDATION_FAILED`, analyze the error, fix your JSON payload (check for missing "status" or "phase_completed" keys), and retry once.
* **Interface Primacy**: `docs/API_CONTRACTS.md` is the immutable law. You must use `## > BEGIN_MODULE` and `## > END_MODULE` tags.
* **Regression Silence**: When in PATCH MODE, do not add new features. Only fix the specific contract violation reported.
* **Module Typology**: Every module defined in `docs/API_CONTRACTS.md` MUST be explicitly tagged as either `Type: LEAF` (handles logic/data) or `Type: ORCHESTRATOR` (handles routing/coordination). 
* **Global Orchestration Topology**: The responsibility for writing multi-service orchestration files (e.g. `docker-compose.yml`) must be explicitly assigned to the system's entry point (e.g. `ui-gateway` or `api-gateway`) or a dedicated orchestrator module, NEVER to a database or peripheral leaf logic module.
* **The Routing Mandate**: If a module is an `ORCHESTRATOR`, its contract MUST include a `Routing Contract` section detailing exactly which sibling modules are invoked under which conditions (e.g., "IF args[0] == 'init', invoke CMD_INIT").
* **Environment Mandate**: You MUST define the stack at the top of `docs/API_CONTRACTS.md`. This block MUST include:
    - **Entry_Point**: The **External Host Port** URL (if networked) or Base Execution Command (if CLI).
    - **Interface_Mount_Selector**: The DOM element where the UI renders (e.g., `#root`), OR standard output structure for CLI validation.
    - **INTEGRATION_TEST_CMD**: A headless-safe command. **MANDATORY FORMAT**: `CI=true <command> --reporter=list`.

## playbook
* **Input**: `handoff/spec_payload.json` and (if regression) `handoff/regression_report.json`.
* **Generative Action**: 
    1. **Analyze**: Compare the spec against technical realities.
    2. **Veto Check**: If the spec is contradictory or impossible, switch to **RFC MODE**.
        * **RFC MODE**: Write your objections clearly to `docs/RFC.md`. Write `{"status": "rfc_requested", "rfc_file_path": "docs/RFC.md"}` to `handoff/arch_payload.json`. Call the `sdlc_advance_state` native tool with args `--task-id <TASK_ID> --to BLOCKED_RFC`. STOP execution.
    3. **Classify (If Spec is Valid)**: Determine if the required modules are `LEAF` or `ORCHESTRATOR` components.
    4. **Modify**: Update `docs/API_CONTRACTS.md`.
        * **Validation Audit**: Cross-reference `PROD_SPEC.md` specifically for host-port mappings (e.g., "map host port 8000 to container port 80"). 
        * **Write Environment Block**: Ensure the `Entry_Point` uses the host port and the `INTEGRATION_TEST_CMD` contains the `CI=true` prefix.
    5. **Query Prep**: Refine `context_queries` to provide localized RAG snippets. You MUST prepend the target stack to your queries (e.g., `[Python] PostgreSQL connection pool setup`) to ensure accurate, stack-agnostic retrieval.
* **Output File**: Generate `handoff/arch_payload.json`.
    ```json
    {
      "status": "success",
      "phase_completed": "ARCHITECTURE",
      "api_contracts_hash": "<generate_short_hash>",
      "vertical_slices": [
        {
          "module_name": "Auth",
          "assigned_agent": "coder",
          "context_queries": ["[Python] PostgreSQL connection pool setup", "[NodeJS] JWT signing logic"]
      ]
    }
    ```
* **State Advancement**: Call the `sdlc_advance_state` native tool with args `--task-id <TASK_ID> --to AWAITING_MODULES`.
