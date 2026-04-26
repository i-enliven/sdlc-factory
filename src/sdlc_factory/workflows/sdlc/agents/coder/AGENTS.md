# ⚙️ AGENTS.md — CODER TOPOLOGY

## boot_sequence
1. **Workdir**: The `Workdir` path has been provided in your wake-up prompt.
2. **Regression Check**: Analyze the `SYSTEM CONTEXT` payload for `ACTIVE REGRESSION DETECTED`.
3. **Hydrate Context**: Your wake-up prompt contains the `SYSTEM CONTEXT` defining your isolated requirements. Rely strictly on this payload.
4. **Execute Playbook**: Transition immediately to the phase defined below.

## global_constraints
* **Strict Isolation**: Process ONLY the single `task_id` assigned.
* **Repetition Safeguard (ANTI-LOOP):** You are strictly forbidden from executing the exact same command string three times in a row.
* **Directory Awareness:** Use `mkdir -p` before writing to sub-directories.
* **Environment Strictness:** Code and commands must strictly conform to the `BEGIN_ENVIRONMENT` block.
* **Background Process Mandate:** Use `nohup` for servers to prevent executor hangs.
* **Infrastructure State Mandate:** Check for port collisions and poisoned volumes if servers fail to start.
* **Headless Test Mandate:** All test commands (UI, API, CLI) must be run in headless/non-interactive mode (e.g. `CI=true` or `--reporter=list`).
* **Compliance Audit Mandate**: Before `advance-state`, you MUST perform a "Functional Mapping" between your code and the specific requirements explicitly injected in your payload. 
    1. **Metadata Check**: Do titles, headers, and labels match the spec exactly?
    2. **Precision Check**: Does the data output (JSON/UI/STDOUT) match the precision requirements?
    3. **Interactivity Check**: Are all toggles, filters, or flags functional and wired to state?

## playbook
### PHASE: CODING
* **Prerequisite**: Read `handoff/test_payload.json`.
* **Generative Action**: 
    1. Implement logic in `src/` per the Environment Contract.
    2. Ensure exports precisely match the contract subset injected in your environment payload.
    3. Run local build/compilation to verify artifacts.
    4. Execute tests in `tests/`. Iterate on code until all tests strictly pass.
    5. **Compliance Audit**: Read your implementation and verify it satisfies the requirements injected in your boot prompt for: a) Primary Entrypoint functionality (Page Title, CLI help output, API schema), b) Data precision (e.g., 4 decimal places), and c) Interactive elements (UI controls, CLI flags).
* **Required Output**: Write `handoff/code_payload.json`.
* **State Advancement**: Call the `sdlc_advance_state` native tool with args `--to QA_REVIEW`.

### PHASE: INTEGRATION_ASSEMBLY
* **Generative Action**:
    1. **Flatten Assets**: Copy module logic without redundant nesting.
    2. **Sanitize Environment**: Execute `docker compose down -v` to ensure a clean state.
    3. **Generate Orchestration**: Write orchestration or packaging files (e.g., `docker-compose.yml` for networked stacks, `package.json` bins or `setup.py` for CLIs).
    4. **Verify Connectivity**: Start assembly and run a health check (e.g., `curl /api/health`).
    5. **Final Assembly Validation**: Validate the entry point (e.g., `curl` a web endpoint, execute `<cli> --help`, or run a test query).
* **Required Output**: Write `handoff/code_payload.json` with `"phase_completed": "INTEGRATION_ASSEMBLY"`.
* **State Advancement**: Call the `sdlc_advance_state` native tool with args `--to INTEGRATION_TESTING`.
