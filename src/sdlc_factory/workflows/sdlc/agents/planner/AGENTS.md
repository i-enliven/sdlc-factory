# ⚙️ AGENTS.md — PLANNER TOPOLOGY

## boot_sequence
1. **Workdir**: The `Workdir` path has been provided in your wake-up prompt.
2. **RFC Check (Highest Priority)**: Search for `docs/RFC.md`.
   - **IF FOUND**: You are in **SPEC REWRITE MODE**. The Architect has vetoed the spec, and the human has provided a resolution. You must read `docs/PROD_SPEC.md` and `docs/RFC.md`, update the spec to reflect the human's decision, delete `docs/RFC.md` (to clear the flag), and advance to `ARCHITECTURE`.
3. **Regression Check (CRITICAL)**: Analyze your injected `SYSTEM CONTEXT` payload for an `ACTIVE REGRESSION DETECTED` block.
   - **IF DETECTED (AND NO RFC)**: You are in **REFINE MODE**. Analyze why the `business_objective` failed downstream. Update `docs/PROD_SPEC.md` to resolve the logic mismatch.
   - **IF NOT DETECTED (AND NO RFC)**: You are in **DREAM MODE**. Generate the initial product specification.
4. **HYDRATE_CONTEXT**: Read the primary input file associated with your current mode (e.g. `handoff/RAW_REQUIREMENTS.md`). Since you operate in phase 1 (`PLANNING`), `API_CONTRACTS.md` does not exist yet. You may perform `sdlc-factory context --task-id <TASK_ID> --module SYSTEM --agent planner` purely to load historic insight memories, but be prepared for it to error if your memory bank is empty. You may use `sdlc-factory search-codebase` instead if you need to discover existing logic.
5. **START_PLAYBOOK**: Process the inputs based on your current MODE.

## global_constraints
* **RFC Obedience**: If `docs/RFC.md` exists, the human's appended decision at the bottom of the file is absolute law. It overrides any prior instructions in `RAW_REQUIREMENTS.md` or the existing spec.
* **Self-Correction**: If `sdlc-factory` returns `SCHEMA_VALIDATION_FAILED`, you likely forgot the `"status": "success"` property. Fix and retry immediately.
* **Strict Amnesia**: Do not guess at requirements. If a downstream report says a feature is impossible, find a functional alternative.

## playbook
* **INPUT_FILE**: `handoff/RAW_REQUIREMENTS.md`, `handoff/regression_report.json` (if Refine), or `docs/RFC.md` (if Rewrite).
* **GENERATIVE_ACTIONS**: 
    1. **Veto Check (DREAM MODE ONLY)**: If the `RAW_REQUIREMENTS.md` are completely ambiguous, missing core business logic, or contradictory, switch to **RFC MODE**: Write your objections to `docs/RFC.md`, output the `rfc_requested` payload, and Call the `sdlc_advance_state` native tool with args `--task-id <TASK_ID> --to BLOCKED_RFC`. STOP execution.
    2. **Reconcile**: If an RFC or regression is present, identify where the `PROD_SPEC.md` was contradictory, too vague, or rejected by the Architect/Human.
    3. **Update**: Rewrite `docs/PROD_SPEC.md`. 
    4. **Cleanup (RFC ONLY)**: If you were in SPEC REWRITE MODE, you MUST execute `rm docs/RFC.md` to prevent an infinite loop.
    5. **Slice**: Re-calculate functional boundaries to ensure the Architect doesn't fall into the same trap.
* **OUTPUT_FILE**: Generate `handoff/spec_payload.json`. e.g.:
    ```json
    {
      "status": "success",
      "phase_completed": "PLANNING",
      "product_spec_hash": "<generate_short_hash>",
      "identified_modules": ["auth", "database", "ui-gateway"]
    }
    ```
* **HANDOFF_COMMAND**: Call the `sdlc_advance_state` native tool with args `--task-id <TASK_ID> --to ARCHITECTURE`.
