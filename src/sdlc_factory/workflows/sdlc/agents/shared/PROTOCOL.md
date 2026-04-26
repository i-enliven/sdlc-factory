# 📜 SDLC Protocol

## 1. System Philosophy
This protocol governs the SDLC Factory autonomous software development lifecycle. Agents operate as a decentralized, event-driven microservice network. They are **stateless and amnesic**, possessing no conversational memory of prior executions. 

## 2. Directory Structure & State Management
The workdir is strictly flattened to minimize deep pathing overhead. Complex applications utilize **Vertical Slicing**, breaking features into isolated child workdirs managed by the orchestrator tool.

```text
<TASK_ID>/                              # Parent or Child task execution environment - your Workdir
    .state/                             
        current.json                    # The passive state ledger
    dist/                               # Language-agnostic build artifacts
        artifact.tar.gz                 # Compressed app bundle (src + deps)
        metadata.json                   # Execution contract (how to run after extraction)
    handoff/                            # Strictly typed M2M JSON payloads
    issues/                             # Escalated error reports
    src/                                # Implementation code
    tests/                              # Test suites
    docs/
        API_CONTRACTS.md                # The absolute source of truth for interfaces
        PROD_SPEC.md                    # Structured product requirements
```

## 3. The Decentralized State Pipeline
Agents do not self-trigger. They are invoked by their native SDLC Factory heartbeat, fetch context, and perform their phase. Once complete, they advance the ledger to the next phase and return to sleep.

| Phase | Agent | Scope & Action | Output (to `handoff/`) | Next State |
| :--- | :--- | :--- | :--- | :--- |
| **1. Planning** | `planner` | Global Requirements (Parent Workdir) | `spec_payload.json` | `ARCHITECTURE` |
| **2. Architecture** | `architect` | Defines `API_CONTRACTS` & Module Slices | `arch_payload.json` | `AWAITING_MODULES` (Spawns Children) |
| **3. Test Design** | `tester` | Writes isolated module tests (Child Workdir) | `test_payload.json` | `CODING` |
| **4. Coding** | `coder` | Implements localized module (Child Workdir) | `code_payload.json` | `QA_REVIEW` |
| **5. QA Review** | `tester` | Runs localized unit tests (Child Workdir) | `qa_report.json` | `MODULE_RESOLVED` (Gathers if all siblings done) |
| **6. Integration** | `tester` | Runs E2E suites (e.g., Playwright for UI, subprocess for CLI) across assembled app | `integration_report.json` | `HUMAN_QA` |
| **7. Human QA** | `human` | Human visual/functional verification. Manually runs `advance-state`. | *none* | `DEPLOY` |
| **8. Deploy** | `deployer` | **Heuristic Packaging**: Analyzes stack and creates `dist/artifact.tar.gz`. | `deploy_payload.json` | `MONITOR` |
| **9. Monitor** | `monitor` | **Sandbox Validation**: Extracts archive and executes `entry_command`. | `health_status.json` | `RESOLVED` |

## 4. Packaging Strategy: Archive-First
The `deployer` must produce a standalone archive.
1. **Heuristic Scan**: Identify stack (Node, Python, Go, etc.) via `src/` manifests.
2. **Consolidate**: Build production assets and resolve local dependencies.
3. **Compress**: Bundle the file tree into `dist/artifact.tar.gz`.
4. **Contract**: Define the `entry_command` in `dist/metadata.json` assuming extraction to a clean root.
5. **Asset Compilation**: If the stack requires compilation (e.g., Vite/React UI or compiled binaries), the `deployer` MUST execute the build step (e.g., `pnpm build`) and package the resulting built artifacts for the execution environment, rather than serving raw unbuilt source code.

## 5. Tool Execution & Resource Discipline
* **Context Primacy (MANDATORY):** You are a **Pre-Hydrated** agent. Your wake-up prompt contains a `SYSTEM CONTEXT` block with your specific module boundaries, environment rules, and curated code snippets (if defined). You MUST rely on this as your primary source of truth for the current task.
* **On-Demand Discovery:** The `sdlc_search_codebase` tool is your primary mechanism for discovering logic, utilities, or patterns in **brownfield projects** that are NOT included in your initial pre-hydrated context. Use it proactively to fill knowledge gaps, discover legacy dependencies, or align with existing project conventions.
* **Native Tool Preference:** When available, use the native function tools (e.g., `sdlc_advance_state`, `sdlc_context`, `sdlc_search_codebase`) instead of manual shell strings. They are faster, more reliable, and provide better error handling.
* **Execution Efficiency:** Before invoking any tool, ensure you have formulated a precise, targeted query. Do not execute rapid, sequential tool calls trying to guess a file structure. Read, reason, execute once.
* **Consolidate Discovery:** Batch reconnaissance (e.g., `ls src/ docs/ tests/` or `tree -L 2`) into single calls. 
* **High-Context Reading:** Use `grep -Hn ".*" file1 file2` to read multiple files; this provides filenames and line numbers in a single stream, preventing context fragmentation.
* **Idempotent Chaining:** Chain operations with `&&` (e.g., `mkdir -p path && touch path/file`) to ensure state persistence without requiring a secondary verification turn.
* **Atomic Operations:** Chain creation and verification (e.g., `mkdir -p src/ && ls src/`) to confirm persistence without a second tool turn.
* **Batch Reading:** When inspecting known implementation details, use a single `grep -Hn ".*" file1 file2 file3` call for all relevant files to reduce round-trip overhead.
* **Idempotency:** Use `mkdir -p` and `rm -f` to eliminate unnecessary "existence check" tool calls.
* **CLI Persistence (MANDATORY):** All file persistence is strictly manual. You MUST generate files securely via the terminal using your `run_cli_command` tool (e.g., `cat << 'EOF' > file.json.`) before advancing the state. Conversational output does NOT write to the disk.
* **On-the-Fly Recovery:** If the `sdlc_advance_state` tool throws a `SCHEMA_VALIDATION_FAILED` error, do not panic or regress. Simply fix your JSON payload via the CLI and retry the state advancement tool.
* **Headless Execution Strictness (MANDATORY):** Agents operating in any environment (UI, CLI, API) must ensure all test runners and execution commands run in strict headless CI/non-interactive mode (e.g., `CI=true`, `--reporter=list`). Interactive prompts will fatally hang the shell. Furthermore, do NOT use `--with-deps` during Playwright installation (e.g. `npx playwright install --with-deps chromium`), as it triggers `sudo` and steals TTY input. Use `npx playwright install chromium` exclusively.
* **Execution Completion (Yielding):** Do not run placeholder CLI commands like `echo "Done"` to signal you have finished a task. When your work is complete and you have invoked `sdlc_advance_state` (or finalized your necessary tool calls), simply yield (i.e., stop executing tools and conclude your response) to allow the native factory heartbeat to process your completion and save token context.
* **Verbose Command Detachment (MANDATORY):** For commands that produce extreme, continuous console output (like `pnpm test`, watch scripts, or noisy compilations), you MUST detach them using `nohup` or pipe their output to `> /dev/null 2>&1` to prevent overflowing the tool output buffers and crashing the context window.
* **Minified File Safeguard:** NEVER use `head`, `curl` or `cat` to inspect compiled/minified assets (e.g., `.js`, `.css`, `.map` files in `dist/` or live webserver). Minified files often contain their entire payload on a single line, meaning `head -n 10` will return the entire multi-megabyte string and crash your context window. If you must inspect a built asset, use `grep -o` with a specific target string and pipe it to `wc -l` to count occurrences, or rely strictly on E2E testing tools like Playwright to verify functionality.

## 6. Communication & Context Fast-Dieting
1. **Rationalized Execution:** Agents must produce Action Rationale in natural language to formulate tool calls, but all final deliverables strictly conform to JSON, pseudo-code, or source code.
2. **Zero-Ambiguity Contracts:** `API_CONTRACTS.md` is the immutable law. The `architect` defines exact boundaries (e.g., `## > BEGIN_MODULE: Auth`).
3. **Isolated Context:** Upon waking, the primary model receives a Fast-Dieted context with ONLY its assigned module boundaries from `API_CONTRACTS.md` AND its semantic `memory_insights` vectors, preventing token bloat.

## 7. Semantic Mastery & Long Term Memory
Agents have access to a dynamic RAG memory vector cluster (`pgvector`).
1. **Explicit Storage (Stack-Aware)**: Because the factory is stack-agnostic, you MUST explicitly prepend the tech stack to your context (e.g., `[React/Vite] <issue>` or `[Python/CLI] <issue>`) to avoid polluting the global vector space. If an agent resolves a highly complex configuration/regression, they MUST push the exact insight into long-term memory via the CLI: `sdlc-factory store-memory --agent <your-role> --task-context "[<Tech Stack>] <issue>" --resolution "<solution>"`.
2. **Contextual Retrieval**: These memories are autonomously stitched into your initial payload or whenever you run the `context` command with your `--agent` parameter.

## 8. Error Handling: The Regression Rule
1. **No Cross-Phase Local Retries**: If you are in an *evaluation* phase (e.g., `tester` in QA_REVIEW, or `monitor` in MONITOR) and you find an issue in the artifact, do not attempt to fix the source code locally. You must indict the phase and trigger a regression. However, if you are actively *generating* the artifact (e.g., `coder` in CODING), you should run tests locally and iterate your code until they pass before handing off.
2. **Indictment**: Identify the responsible upstream phase (e.g., Monitor fails -> Indict `DEPLOY` or `CODING`).
3. **Regression Handback**: 
    - Explicitly write a diagnostic `handoff/regression_report.json` via the CLI with the stack trace.
    - Call the `sdlc_advance_state` native tool with args `{"task_id": "<ID>", "to": "<PHASE>", "regression": true}`.
    - **Bubble-Up Logic:** If a child module regresses to a global phase (`PLANNING` or `ARCHITECTURE`), the CLI will automatically escalate the regression to the parent workspace and place all child modules into a `PAUSED_REGRESSION` state, freezing their execution until the parent resolves the global architecture.
4. **Escalation**: If a task regresses to the same phase twice, write to `issues/ISSUE-FATAL.md` and set state to `BLOCKED`.

## 9. The Request for Comments (RFC) Escape Hatch
If either the `planner` (during requirements gathering) or the `architect` (during technical decomposition) detects ambiguous constraints, contradictory requirements, or missing logic, they are empowered to halt the pipeline.
1. **Trigger**: The Agent writes questions/objections to `docs/RFC.md`, outputs an `rfc_requested` payload, and advances the state to `BLOCKED_RFC`.
2. **Human Resolution**: The human reads `RFC.md` and appends their decision or clarification directly to the bottom of the file.
3. **Correction Regression**: The human executes `sdlc-factory advance-state --task-id <TASK_ID> --to PLANNING --regression`.
4. **Spec Rewrite**: The `planner` wakes up, reads both `PROD_SPEC.md` and the human's answers in `RFC.md` (if any), and rewrites the `PROD_SPEC.md` to reflect the new reality before advancing back to `ARCHITECTURE`.
