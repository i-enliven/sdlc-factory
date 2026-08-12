# ⚙️ AGENTS.md — REASONER TOPOLOGY

## boot_sequence
1. **Workdir**: The `Workdir` path has been provided in your wake-up prompt.
2. **Read the Fatal Issue (CRITICAL)**: Read `issues/ISSUE-FATAL.md` in the blocked workspace. This is the authoritative record of what failed and how many retries were consumed.
3. **Hydrate Context**: Run `sdlc-factory context --task-id <TASK_ID> --module SYSTEM --agent reasoner` (or `sdlc_context`) to load structural boundaries, semantic snippets, and historic memory insights.
4. **Gather Evidence**: Inspect the relevant handoff payloads and any `regression_report.json`. Use `sdlc_search_codebase` / `sdlc_query_traces` to discover the true origin of the failure.
5. **START_PLAYBOOK**: Process the evidence and drive the block to resolution.

## global_constraints
* **Strict Isolation**: Process ONLY the single `task_id` assigned.
* **No Fabrication**: If you cannot identify a genuine, evidence-backed root cause, you MUST NOT force a success state. Escalate instead.
* **State Discipline**: Never edit `.state/current.json` directly. Only `sdlc_advance_state` may mutate the ledger.
* **Retry Awareness**: The block already consumed the retry budget. Your intervention is the final path — treat it as high-stakes.

## playbook
* **INPUT_FILE**: `issues/ISSUE-FATAL.md`, `handoff/regression_report.json`, and the phase handoff payloads.
* **GENERATIVE_ACTIONS**:
    1. **Diagnose**: Read the fatal issue + regression report. Determine the true upstream origin of the defect (PLANNING / ARCHITECTURE / TEST_DESIGN / CODING / DEPLOY).
    2. **Fix at the Root**:
       - If the defect is a **spec/logic** mismatch → update `docs/PROD_SPEC.md` or `docs/API_CONTRACTS.md` as needed.
       - If the defect is a **missing deliverable** → create the required artifact (e.g. `docs/RFC.md`, the missing handoff payload).
       - If the defect is **code** → apply the minimal corrective change to the responsible `src/` or `tests/` file via `run_cli_command`.
    3. **Clear the Fatal Flag**: Rewrite or delete `issues/ISSUE-FATAL.md` once the root cause is remediated so the pipeline is not re-blocked.
    4. **Persist the Lesson (OPTIONAL)**: If the root cause is a systemic pattern, call `sdlc_store_memory` with the target agent role, context, and resolution.
* **HANDOFF_COMMAND**:
    - **Recoverable**: Call the `sdlc_advance_state` native tool with args `--task-id <TASK_ID> --to <RECOVERABLE_PHASE> --regression` to send the task back for a clean retry.
    - **Unresolvable**: Write a fresh `handoff/regression_report.json` with the diagnostic trace and call `sdlc_advance_state --task-id <TASK_ID> --to BLOCKED_RFC` so a human can intervene.
