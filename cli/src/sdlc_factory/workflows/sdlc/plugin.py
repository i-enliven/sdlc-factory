import json
import shutil
import typer
from pathlib import Path
from typing import Optional, Dict, Any, List

from sdlc_factory.workflows.base import WorkflowPlugin
from sdlc_factory.utils import global_logger, read_json, write_json, get_workspace

class SdlcWorkflow(WorkflowPlugin):
    @property
    def name(self) -> str:
        return "sdlc"

    @property
    def agents_dir(self) -> Path:
        # Resolve path relative to this file
        return Path(__file__).parent / "agents"

    @property
    def schemas(self) -> Dict[str, Dict[str, Any]]:
        return {
            "PLANNING": {"file": "spec_payload.json", "schema": {"oneOf": [{"type": "object", "required": ["status", "phase_completed", "product_spec_hash", "identified_modules"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "PLANNING"}, "product_spec_hash": {"type": "string"}, "identified_modules": {"type": "array", "items": {"type": "string"}}}}, {"type": "object", "required": ["status", "phase_completed", "rfc_file_path"], "properties": {"status": {"const": "rfc_requested"}, "phase_completed": {"const": "PLANNING"}, "rfc_file_path": {"const": "docs/RFC.md"}}}]}},
            "ARCHITECTURE": {"file": "arch_payload.json", "schema": {"oneOf": [{"type": "object", "required": ["status", "phase_completed", "api_contracts_hash", "vertical_slices"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "ARCHITECTURE"}, "api_contracts_hash": {"type": "string"}, "vertical_slices": {"type": "array", "items": {"type": "object", "required": ["module_name", "assigned_agent"], "properties": {"module_name": {"type": "string"}, "assigned_agent": {"type": "string"}, "context_queries": {"type": "array", "items": {"type": "string"}}}}}}},{"type": "object","required": ["status", "phase_completed", "rfc_file_path"],"properties": {"status": {"const": "rfc_requested"},"phase_completed": {"const": "ARCHITECTURE"},"rfc_file_path": {"const": "docs/RFC.md"}}}]}},
            "TEST_DESIGN": {"file": "test_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "test_files_created"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "TEST_DESIGN"}, "module_id": {"type": "string"}, "test_files_created": {"type": "array", "items": {"type": "string"}}}}},
            "CODING": {"file": "code_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "execution_metrics", "artifacts", "qa_handoff"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "CODING"}, "module_id": {"type": "string"}, "execution_metrics": {"type": "object", "required": ["tool_calls_made", "compilation_passed"], "properties": {"tool_calls_made": {"type": "number"}, "compilation_passed": {"const": True}}}, "artifacts": {"type": "array"}, "qa_handoff": {"type": "object", "required": ["test_target", "exec_command"], "properties": {"test_target": {"type": "string"}, "exec_command": {"type": "string"}}}}}},
            "INTEGRATION_ASSEMBLY": {"file": "code_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "execution_metrics", "artifacts", "qa_handoff"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "INTEGRATION_ASSEMBLY"}, "module_id": {"type": "string"}, "execution_metrics": {"type": "object", "required": ["tool_calls_made", "compilation_passed"], "properties": {"tool_calls_made": {"type": "number"}, "compilation_passed": {"const": True}}}, "artifacts": {"type": "array"}, "qa_handoff": {"type": "object", "required": ["test_target", "exec_command"], "properties": {"test_target": {"type": "string"}, "exec_command": {"type": "string"}}}}}},
            "QA_REVIEW": {"file": "qa_report.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "all_tests_passed"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "QA_REVIEW"}, "module_id": {"type": "string"}, "all_tests_passed": {"const": True}, "failing_tests": {"type": "array"}}}},
            "INTEGRATION_TESTING": {"file": "integration_report.json", "schema": {"type": "object", "required": ["status", "phase_completed", "e2e_metrics", "assembled_artifacts_hash"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "INTEGRATION_TESTING"}, "e2e_metrics": {"type": "object", "required": ["scenarios_executed", "critical_paths_passed"], "properties": {"scenarios_executed": {"type": "number"}, "critical_paths_passed": {"const": True}}}, "assembled_artifacts_hash": {"type": "string"}}}},
            "DEPLOY": {"file": "deploy_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "artifact_path", "archive_type", "artifact_hash", "entry_command"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "DEPLOY"}, "artifact_path": {"type": "string"}, "archive_type": {"enum": ["tar.gz", "zip"]}, "artifact_hash": {"type": "string"}, "entry_command": {"type": "string"}}}},
            "MONITOR": {"file": "health_status.json", "schema": {"type": "object", "required": ["status", "phase_completed", "health_check_passed", "final_resolution"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "MONITOR"}, "health_check_passed": {"const": True}, "smoke_test_output": {"type": "string"}, "final_resolution": {"const": "RESOLVED"}}}},
            "REGRESSION": {"file": "regression_report.json", "schema": {"type": "object", "required": ["status", "indictment_metadata", "diagnostic_trace"], "properties": {"status": {"const": "success"}, "indictment_metadata": {"type": "object", "required": ["source_phase", "target_phase", "error_category"], "properties": {"source_phase": {"type": "string"}, "target_phase": {"type": "string"}, "error_category": {"enum": ["COMPILE_ERROR", "LOGIC_MISMATCH", "CONTRACT_VIOLATION", "INFRA_FAILURE"]}}}, "diagnostic_trace": {"type": "object", "required": ["observed_behavior", "stack_trace"]}}}}
        }

    @property
    def agents_list(self) -> List[str]:
        return ["planner", "architect", "tester", "coder", "deployer", "monitor"]

    def get_pending_task(self, agent: str, workspace_root: Path) -> Optional[dict]:
        phase_map = {
            "planner": ["PLANNING"], "architect": ["ARCHITECTURE"], 
            "tester": ["TEST_DESIGN", "QA_REVIEW", "INTEGRATION_TESTING"], 
            "coder": ["CODING", "INTEGRATION_ASSEMBLY"], "deployer": ["DEPLOY"], "monitor": ["MONITOR"]
        }
        target_phases = phase_map.get(agent, [])

        for ws_path in workspace_root.glob("*"):
            if not ws_path.is_dir(): continue
            state_file = ws_path / ".state" / "current.json"
            if not state_file.exists(): continue
            
            state = read_json(state_file)
            if state.get("workflow", "sdlc") != "sdlc":
                continue # Skip workspaces that aren't using this workflow

            current_phase = state.get("phase")
            if current_phase in target_phases:
                return {
                    "task_id": ws_path.name,
                    "workspace": str(ws_path),
                    "assigned_module": state.get("module_id", "SYSTEM"),
                    "phase": current_phase
                }
        return None

    def get_phase_context(self, phase: str, workspace: Path) -> str:
        phase_context = ""
        phase_files = {
            "PLANNING": ["handoff/RAW_REQUIREMENTS.md", "RAW_REQUIREMENTS.md", "docs/RFC.md", "docs/PROD_SPEC.md", "handoff/regression_report.json"],
            "ARCHITECTURE": ["handoff/spec_payload.json", "docs/PROD_SPEC.md", "handoff/regression_report.json"],
            "TEST_DESIGN": ["handoff/arch_payload.json", "handoff/regression_report.json"],
            "CODING": ["handoff/test_payload.json", "handoff/regression_report.json"],
            "INTEGRATION_ASSEMBLY": ["handoff/code_payload.json", "docs/API_CONTRACTS.md", "handoff/regression_report.json"],
            "QA_REVIEW": ["handoff/code_payload.json", "handoff/regression_report.json"],
            "INTEGRATION_TESTING": ["handoff/qa_report.json", "docs/PROD_SPEC.md", "docs/API_CONTRACTS.md", "handoff/regression_report.json"],
            "DEPLOY": ["handoff/integration_report.json", "handoff/regression_report.json"],
            "MONITOR": ["handoff/deploy_payload.json"]
        }

        if phase in phase_files:
            for file_path_str in phase_files[phase]:
                file_path = workspace / file_path_str
                if file_path.exists():
                    try:
                        content = file_path.read_text(encoding="utf-8")
                        phase_context += f"### {file_path.name}\n```\n{content}\n```\n\n"
                    except Exception as e:
                        phase_context += f"### {file_path.name}\nError reading file: {e}\n\n"

        return phase_context

    def on_transition(self, ws: Path, task_id: str, current_phase: str, to_phase: str, state: dict) -> None:
        if current_phase == "ARCHITECTURE" and to_phase == "AWAITING_MODULES":
            self._scatter_architecture(ws, task_id)

        if to_phase == "MODULE_RESOLVED" and "-MOD-" in task_id:
            self._gather_modules(task_id)

        if to_phase == "RESOLVED" and task_id.endswith("-INTEGRATION"):
            self._consolidate_integration(ws, task_id)

    def on_regression(self, ws: Path, task_id: str, to_phase: str, state: dict, max_retries: int) -> tuple[Path, str, dict]:
        """
        Handles SDLC specific regression bubbling.
        Returns potentially updated (ws, task_id, state) if bubbled.
        """
        if to_phase == "CODING" and task_id.endswith("-INTEGRATION"):
            to_phase = "INTEGRATION_ASSEMBLY"
            
        if to_phase in ["PLANNING", "ARCHITECTURE"] and ("-MOD-" in task_id or "-INTEGRATION" in task_id):
            parent_id = task_id.split("-MOD-")[0].replace("-INTEGRATION", "")
            parent_ws = get_workspace(parent_id)
            
            child_reg_file = ws / "handoff" / "regression_report.json"
            if child_reg_file.exists():
                shutil.copy2(child_reg_file, parent_ws / "handoff" / "regression_report.json")
                
            # Pause all other child modules
            for child_ws in parent_ws.parent.glob(f"{parent_id}-*"):
                if child_ws.is_dir() and child_ws.name != parent_id:
                    child_state_file = child_ws / ".state" / "current.json"
                    if child_state_file.exists():
                        child_state = read_json(child_state_file)
                        child_state["phase"] = "PAUSED_REGRESSION"
                        write_json(child_state_file, child_state)
            
            global_logger.warning(f"🔄 BUBBLE UP: Regression from {task_id} escalated to parent {parent_id}")
            
            # Switch context to parent workspace
            ws = parent_ws
            task_id = parent_id
            state = read_json(ws / ".state" / "current.json")
            
        return ws, task_id, state, to_phase

    # --- Internal SDLC Hooks ---
    
    def _scatter_architecture(self, parent_ws: Path, task_id: str):
        arch_data = read_json(parent_ws / "handoff" / "arch_payload.json")
        spawned = []
        
        for slice_data in arch_data.get("vertical_slices", []):
            mod_name = slice_data.get("module_name")
            child_id = f"{task_id}-MOD-{mod_name}"
            child_ws = get_workspace(child_id)
            
            for dir_name in [".state", "handoff", "issues", "src", "tests", "docs", "dist"]:
                (child_ws / dir_name).mkdir(parents=True, exist_ok=True)
                
            write_json(child_ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "TEST_DESIGN", "task_id": child_id, "module_id": mod_name})
            
            try:
                shutil.copy(parent_ws / "docs" / "API_CONTRACTS.md", child_ws / "docs" / "API_CONTRACTS.md")
                shutil.copy(parent_ws / "handoff" / "arch_payload.json", child_ws / "handoff" / "arch_payload.json")
            except FileNotFoundError as e:
                raise Exception(f"Hydration Error: Missing parent artifacts: {e}")
                
            spawned.append(child_id)
            
        global_logger.info(f"[SUCCESS] Spawned child tasks: {', '.join(spawned)}", extra={"color": typer.colors.GREEN})

    def _gather_modules(self, current_task_id: str):
        parent_id = current_task_id.split("-MOD-")[0]
        parent_ws = get_workspace(parent_id)
        
        arch_data = read_json(parent_ws / "handoff" / "arch_payload.json")
        expected_modules = [f"{parent_id}-MOD-{s.get('module_name')}" for s in arch_data.get("vertical_slices", [])]
        
        all_resolved = True
        for mod_id in expected_modules:
            if mod_id == current_task_id:
                continue
                
            mod_state = read_json(get_workspace(mod_id) / ".state" / "current.json")
            if mod_state.get("phase") != "MODULE_RESOLVED":
                all_resolved = False
                break
                
        if all_resolved:
            integration_id = f"{parent_id}-INTEGRATION"
            int_ws = get_workspace(integration_id)
            
            for dir_name in [".state", "handoff", "issues", "src", "tests", "docs", "dist"]:
                (int_ws / dir_name).mkdir(parents=True, exist_ok=True)
                
            write_json(int_ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "INTEGRATION_ASSEMBLY", "task_id": integration_id})
            
            try:
                shutil.copy(parent_ws / "docs" / "API_CONTRACTS.md", int_ws / "docs" / "API_CONTRACTS.md")
                shutil.copy(parent_ws / "docs" / "PROD_SPEC.md", int_ws / "docs" / "PROD_SPEC.md")
            except FileNotFoundError as e:
                global_logger.warning(f"Integration Hydration Warning: Missing parent artifacts: {e}")

            global_logger.info(f"[SUCCESS] All modules resolved! Spawned {integration_id}", extra={"color": typer.colors.MAGENTA})

    def _consolidate_integration(self, ws: Path, task_id: str):
        parent_id = task_id.replace("-INTEGRATION", "")
        parent_ws = get_workspace(parent_id)
        
        for d in ["src", "tests", "dist"]:
            source_dir = ws / d
            if source_dir.exists():
                shutil.copytree(source_dir, parent_ws / d, dirs_exist_ok=True)
                
        for root_item in ws.iterdir():
            if root_item.is_file():
                shutil.copy2(root_item, parent_ws / root_item.name)
        
        parent_state_file = parent_ws / ".state" / "current.json"
        if parent_state_file.exists():
            p_state = read_json(parent_state_file)
            p_state["phase"] = "RESOLVED"
            write_json(parent_state_file, p_state)
            
        global_logger.info(f"📦 [CONSOLIDATION] Bundled all into {parent_id}.", extra={"color": typer.colors.CYAN})
        
        global_logger.info(f"🧹 [CLEANUP] Purging intermediate...", extra={"color": typer.colors.YELLOW})
        for child_ws in parent_ws.parent.glob(f"{parent_id}-*"):
            if child_ws.is_dir() and child_ws.name != parent_id:
                try:
                    shutil.rmtree(child_ws)
                except Exception as e:
                    global_logger.warning(f"Warning: Could not delete {child_ws.name}: {e}")
