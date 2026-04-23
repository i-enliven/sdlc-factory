# 🛠️ Skill: SDLC Factory (`sdlc-factory`)

**CRITICAL PATHING NOTE:** The CLI is installed at `/usr/local/bin/sdlc-factory`. If the standard `sdlc-factory` command is not in your environment `$PATH`, you MUST use the absolute path: `/usr/local/bin/sdlc-factory`. Do not manually parse directories if the CLI is available.

## 1. Overview
The `sdlc-factory` is the deterministic state machine and context-isolation engine for the SDLC Factory autonomous development pipeline. You must use this CLI tool to fetch your localized context, verify your task boundaries, and advance the system state once your work is complete.

**Crucial Constraint:** You do not manage the pipeline. You only read your context, perform your specific generative phase, and signal completion. The `sdlc-factory` will automatically handle routing, spawning sub-workspaces for modular slices, and gathering them for integration.

## 2. Available Commands
You interact with the `sdlc-factory` using your standard local execution tool (e.g., `group:runtime` or `exec` running CLI bash commands).

### A. Context Extraction (`context`)
**Use Case:** Executed immediately upon waking up to fetch your specific instructions, existing codebase snippets, and historic semantic memories relevant to your role.
**Syntax:** `sdlc-factory context --task-id <TASK_ID> --module <MODULE_ID> --agent <YOUR_ROLE>`
**Returns:** A JSON envelope containing the structural boundaries, legacy code snippets, AND localized vector insights (`memory_insights`) curated specifically for your role.

### B. Insight Memory Persistence (`store-memory`)
**Use Case:** Executed exactly when you successfully resolve a complex architecture bug, system configuration fault, or logic regression to permanently save the insight into Postgres pgvector.
**Syntax:** `sdlc-factory store-memory --agent <YOUR_ROLE> --task-context "<explicit bug context>" --resolution "<explicit code resolution>"`
**Returns:** A success confirmation bridging the vector natively.

### C. Active Semantic Search (`search-codebase`)
**Use Case:** Executed when your provided context is insufficient, or when you need to discover existing utility functions, variable definitions, or architectural patterns in the legacy codebase. Do NOT use this to replace your primary `context` command; use it only to fill specific knowledge gaps.
**Syntax:** `sdlc-factory search-codebase --query "<natural_language_search_string>" --limit <int>`
**Example:** `sdlc-factory search-codebase --query "How are JWT tokens verified in the middleware?" --limit 2`
**Returns:** A formatted string containing the top matching file paths and their associated code snippets based on vector similarity.

### D. State Advancement (`advance-state`)
**Use Case:** Executed when you have successfully finished your phase and outputted your required JSON handoff file into the `handoff/` directory.
**Syntax:** `sdlc-factory advance-state --task-id <TASK_ID> --to <NEXT_PHASE> [--regression]`
**Returns:** Success confirmation or a strict JSON schema validation error. If you receive a schema validation error, you MUST meticulously fix your JSON payload in `handoff/` and retry the exact same command. Do not give up; continue iterating on the file until the JSON successfully validates!
**Regression Flag:** If an upstream evaluation fails (e.g., tests fail in QA), you must pass the `--regression` flag. When using this flag, you must provide a `regression_report.json` file instead of your standard phase payload.

### E. State Querying (`query-state`)
*Note: This is primarily used by your gatekeeper heartbeat script, but can be used for self-diagnostic routing.*
**Syntax:** `sdlc-factory query-state --agent <AGENT_NAME>`
**Returns:** JSON routing payload containing active `task_id`, `workspace` path, and assigned `module_id`.

## 3. Strict Execution Rules
1. **JSON Only:** All files you write to `handoff/` must be strictly typed JSON matching the system schemas. No conversational markdown.
2. **Never Self-Assign:** You cannot change your own module or task ID. You must strictly obey the `<MODULE_ID>` passed to you by the gatekeeper.
3. **Persistent Self-Correction:** If `advance-state` fails due to schema invalidation or malformed JSON, you are strictly forbidden from giving up. You must analyze the error trace, reconstruct the payload accurately, and rerun the command until you successfully advance the state. You should only ever write to `issues/ISSUE-FATAL.md` for unrecoverable logic blockers, NOT for JSON schema boundaries.

## 4. Execution Examples

### Example 1: The Coder fetching its isolated prompt
$ sdlc-factory context --task-id EPIC-123-MOD-AUTH --module AuthService --agent coder
```json
{
  "status": "success",
  "module_id": "AuthService",
  "extracted_context": "## > BEGIN_MODULE: AuthService\n**Description**: Handles JWT generation.\n### Interface: `src/auth.ts`\n...\n## > END_MODULE: AuthService"
}
```

### Example 2: The Architect spawning modular child workspaces
After writing the `arch_payload.json` with the Vertical Slices defined:
```bash
$ sdlc-factory advance-state --task-id EPIC-123 --to AWAITING_MODULES
[SUCCESS] State updated. Spawned child workspaces: EPIC-123-MOD-AUTH, EPIC-123-MOD-PAYMENT.
```

### Example 3: The Coder completing its task
After writing the strictly typed `code_payload.json` to the `handoff/` directory:
```bash
$ sdlc-factory advance-state --task-id EPIC-123-MOD-AUTH --to QA_REVIEW
[SUCCESS] Schema validated. State advanced to QA_REVIEW.
```
