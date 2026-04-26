# 🛠️ Skill: SDLC Factory (`sdlc-factory`)

**CRITICAL EXECUTION NOTE:** You are provided with **Native Function Tools** (e.g., `sdlc_advance_state`, `run_cli_command`, `sdlc_search_codebase`). You MUST prefer these function tools over manual shell execution. They are faster, handle multi-line inputs better, and provide deterministic results.

## 1. Overview
The `sdlc-factory` is the deterministic state machine and context-isolation engine for the SDLC Factory autonomous development pipeline. You must use this system to fetch recovery context, discover codebase patterns, and advance the system state once your work is complete.

**Crucial Constraint:** You are **Pre-Hydrated**. Your initial prompt already contains your module boundaries and curated snippets. You do not manage the pipeline; you only signal completion.

## 2. Primary Function Tools (Preferred)
You should interact with the factory primarily through your assigned function tools.

### A. State Advancement (`sdlc_advance_state`)
**Use Case:** Executed when you have successfully finished your phase and outputted your required JSON handoff file into the `handoff/` directory.
**Returns:** Success confirmation or a strict JSON schema validation error. If you receive an error, you MUST fix your JSON payload and retry until it validates.
**Regression Flag:** If an upstream evaluation fails (e.g., tests fail in QA), you must pass the `regression=True` flag and provide a `regression_report.json`.

### B. On-Demand Discovery (`sdlc_search_codebase`)
**Use Case:** Your primary tool for discovering logic, utilities, or patterns NOT included in your initial pre-hydrated context. Essential for brownfield projects to align with existing conventions.
**Returns:** A formatted string containing the top matching file paths and snippets based on vector similarity.

### C. Insight Memory Persistence (`sdlc_store_memory`)
**Use Case:** Executed when you resolve a complex architecture bug or configuration fault to permanently save the insight.
**Mandate:** You MUST prepend the exact tech stack to your `task_context` (e.g., `[Python/uv] Database pool exhausted` or `[Go/CLI] Binary panic`) to prevent cross-polluting the global memory vector for different architectures.
**Returns:** A success confirmation bridging the vector natively.

## 3. Secondary CLI Commands
If function tools are unavailable, you may use the standard CLI via `run_cli_command`.

### A. Manual Search (`sdlc-factory search-codebase`)
**Syntax:** `sdlc-factory search-codebase --query "<natural_language_search_string>" --limit <int>`

## 4. Strict Execution Rules
1. **JSON Only:** All files you write to `handoff/` must be strictly typed JSON matching the system schemas. No conversational markdown.
2. **Never Self-Assign:** You cannot change your own module or task ID. 
3. **Persistent Self-Correction:** If `sdlc_advance_state` fails, you are strictly forbidden from giving up. Analyze the error trace, fix the payload, and rerun.

## 5. Execution Examples (Native Logic)

### Example 1: The Coder completing its task
After writing the strictly typed `code_payload.json` to the `handoff/` directory:
**Tool Call:** `sdlc_advance_state(task_id="EPIC-123-MOD-AUTH", to="QA_REVIEW")`
**Result:** `[SUCCESS] Schema validated. State advanced to QA_REVIEW.`

### Example 2: Discovering a legacy utility in a brownfield project
**Tool Call:** `sdlc_search_codebase(query="How are JWT tokens verified in the middleware?")`
**Result:** Returns file paths and code snippets for JWT verification logic.
