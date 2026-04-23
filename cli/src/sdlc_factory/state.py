import json
import shutil
import typer
from pathlib import Path
from typing import Optional
from jsonschema import validate, ValidationError

from sdlc_factory.utils import (
    SCHEMAS, global_logger, get_config, read_json, 
    write_json, abort, get_workspace_root, get_workspace
)

def auto_hydrate_payload(ws: Path, phase: str, task_id: str, module_id: str):
    """Ensures amnesic agents don't fail schema checks due to boilerplate fields."""
    contract = SCHEMAS.get(phase)
    if not contract:
        return

    target_file = ws / "handoff" / contract["file"]
    data = read_json(target_file, default={})
    
    updates = {
        "status": "success",
        "phase_completed": phase,
        "task_id": task_id,
        "module_id": module_id
    }
    
    if phase == "MONITOR":
        updates["final_resolution"] = "RESOLVED"
    
    modified = False
    for key, val in updates.items():
        if key not in data:
            data[key] = val
            modified = True
            
    if modified:
        write_json(target_file, data)

def validate_handoff(ws: Path, phase: str):
    """Enforces strict JSON schema validation before state advancement."""
    contract = SCHEMAS.get(phase)
    if not contract:
        return

    target_file = ws / "handoff" / contract["file"]
    if not target_file.exists():
        raise ValueError(f"Required file '{contract['file']}' is missing from handoff directory.")

    payload_data = read_json(target_file)
    try:
        validate(instance=payload_data, schema=contract["schema"])
    except ValidationError as e:
        err_msg = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "file": contract["file"],
            "message": f"Validation failed in '{contract['file']}': {e.message}",
            "path": list(e.path)
        }
        raise ValueError(json.dumps(err_msg, indent=2))

def handle_regression(ws: Path, state: dict, to_phase: str, task_id: str, config: dict):
    """Processes regression requests, managing the global error budget."""
    new_retry_count = state.get("retry_count", 0) + 1
    state["retry_count"] = new_retry_count
    
    max_retries = config.get("max_retry_limit", 2)
    state_file = ws / ".state" / "current.json"

    if new_retry_count >= max_retries:
        state["phase"] = "BLOCKED"
        
        reg_report_path = ws / "handoff" / "regression_report.json"
        diag_info = reg_report_path.read_text() if reg_report_path.exists() else "No diagnostic report provided."

        fatal_content = f"""# 🚨 FATAL REGRESSION: {task_id}\n**Status:** BLOCKED\n**Target Phase Failed:** {to_phase}\n**Retry Count:** {new_retry_count}\n\n## Diagnostic Trace\n```json\n{diag_info}\n```"""
        (ws / "issues" / "ISSUE-FATAL.md").write_text(fatal_content)
        
        write_json(state_file, state)
        global_logger.error(f"🛑 CRITICAL: Task {task_id} exceeded retry limit. State set to BLOCKED.", extra={"bold": True})
    else:
        contract = SCHEMAS.get(state.get("phase"))
        if contract:
            handoff_file = ws / "handoff" / contract["file"]
            if handoff_file.exists():
                handoff_file.unlink()
        
        state["phase"] = to_phase
        write_json(state_file, state)
        global_logger.warning(f"⚠️ REGRESSION: {task_id} sent back to {to_phase} (Budget used: {new_retry_count}/{max_retries})")

def scatter_architecture(parent_ws: Path, task_id: str):
    """Spawns child workspaces for dynamically identified architectural modules."""
    arch_data = read_json(parent_ws / "handoff" / "arch_payload.json")
    spawned = []
    
    for slice_data in arch_data.get("vertical_slices", []):
        mod_name = slice_data.get("module_name")
        child_id = f"{task_id}-MOD-{mod_name}"
        child_ws = get_workspace(child_id)
        
        for dir_name in [".state", "handoff", "issues", "src", "tests", "docs", "dist"]:
            (child_ws / dir_name).mkdir(parents=True, exist_ok=True)
            
        write_json(child_ws / ".state" / "current.json", {"phase": "TEST_DESIGN", "task_id": child_id, "module_id": mod_name})
        
        try:
            shutil.copy(parent_ws / "docs" / "API_CONTRACTS.md", child_ws / "docs" / "API_CONTRACTS.md")
            shutil.copy(parent_ws / "handoff" / "arch_payload.json", child_ws / "handoff" / "arch_payload.json")
        except FileNotFoundError as e:
            raise Exception(f"Hydration Error: Missing parent artifacts: {e}")
            
        spawned.append(child_id)
        
    global_logger.info(f"[SUCCESS] Spawned child tasks: {', '.join(spawned)}", extra={"color": typer.colors.GREEN})

def gather_modules(current_task_id: str):
    """Checks if all sibling modules are resolved, and spawns the integration phase if true."""
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
            
        write_json(int_ws / ".state" / "current.json", {"phase": "INTEGRATION_ASSEMBLY", "task_id": integration_id})
        
        try:
            shutil.copy(parent_ws / "docs" / "API_CONTRACTS.md", int_ws / "docs" / "API_CONTRACTS.md")
            shutil.copy(parent_ws / "docs" / "PROD_SPEC.md", int_ws / "docs" / "PROD_SPEC.md")
        except FileNotFoundError as e:
            global_logger.warning(f"Integration Hydration Warning: Missing parent artifacts: {e}")

        global_logger.info(f"[SUCCESS] All modules resolved! Spawned {integration_id}", extra={"color": typer.colors.MAGENTA})


def get_blocked_tasks() -> list:
    """Internal helper to fetch all BLOCKED tasks."""
    workspace_root = get_workspace_root()
    blocked = []
    if not workspace_root.exists(): return blocked
    
    for ws_path in workspace_root.glob("*"):
        if not ws_path.is_dir(): continue
        state = read_json(ws_path / ".state" / "current.json")
        if state.get("phase") == "BLOCKED":
            issue_file = ws_path / "issues" / "ISSUE-FATAL.md"
            blocked.append({
                "task_id": ws_path.name,
                "workspace": str(ws_path),
                "issue_file": str(issue_file)
            })
    return blocked

def get_pending_task(agent: str) -> Optional[dict]:
    """Internal helper to fetch the first available task for a specific agent."""
    workspace_root = get_workspace_root()
    if not workspace_root.exists(): return None
    
    phase_map = {
        "planner": ["PLANNING"], "architect": ["ARCHITECTURE"], 
        "tester": ["TEST_DESIGN", "QA_REVIEW", "INTEGRATION_TESTING"], 
        "coder": ["CODING", "INTEGRATION_ASSEMBLY"], "deployer": ["DEPLOY"], "monitor": ["MONITOR"]
    }
    target_phases = phase_map.get(agent, [])

    for ws_path in workspace_root.glob("*"):
        if not ws_path.is_dir(): continue
        state = read_json(ws_path / ".state" / "current.json")
        current_phase = state.get("phase")
        if current_phase in target_phases:
            return {
                "task_id": ws_path.name,
                "workspace": str(ws_path),
                "assigned_module": state.get("module_id", "SYSTEM"),
                "phase": current_phase
            }
    return None

def do_advance_state(task_id: str, to: str, regression: bool = False) -> str:
    ws = get_workspace(task_id)
    state_file = ws / ".state" / "current.json"
    
    if not state_file.exists():
        raise Exception(f"State file missing for {task_id}")
        
    state = read_json(state_file)
    current_phase = state.get("phase")

    if regression:
        if to == "CODING" and task_id.endswith("-INTEGRATION"):
            to = "INTEGRATION_ASSEMBLY"
            
        if to in ["PLANNING", "ARCHITECTURE"] and ("-MOD-" in task_id or "-INTEGRATION" in task_id):
            parent_id = task_id.split("-MOD-")[0].replace("-INTEGRATION", "")
            parent_ws = get_workspace(parent_id)
            
            child_reg_file = ws / "handoff" / "regression_report.json"
            if child_reg_file.exists():
                shutil.copy2(child_reg_file, parent_ws / "handoff" / "regression_report.json")
                
            for child_ws in get_workspace_root().glob(f"{parent_id}-*"):
                if child_ws.is_dir() and child_ws.name != parent_id:
                    child_state_file = child_ws / ".state" / "current.json"
                    if child_state_file.exists():
                        child_state = read_json(child_state_file)
                        child_state["phase"] = "PAUSED_REGRESSION"
                        write_json(child_state_file, child_state)
            
            global_logger.warning(f"🔄 BUBBLE UP: Regression from {task_id} escalated to parent {parent_id}")
            
            ws = parent_ws
            task_id = parent_id
            state_file = ws / ".state" / "current.json"
            state = read_json(state_file)

        handle_regression(ws, state, to, task_id, get_config())
        return "Regression handled"

    auto_hydrate_payload(ws, current_phase, task_id, state.get("module_id", "SYSTEM"))
    validate_handoff(ws, current_phase)

    if current_phase == "ARCHITECTURE" and to == "AWAITING_MODULES":
        scatter_architecture(ws, task_id)

    if to == "MODULE_RESOLVED" and "-MOD-" in task_id:
        gather_modules(task_id)

    # --- REGRESSION CLEANUP ---
    # If we made it here, the payload is valid and it's a successful step forward.
    regression_file = ws / "handoff" / "regression_report.json"
    if regression_file.exists():
        regression_file.unlink()
        global_logger.info(f"🧹 [CLEANUP] Resolved regression. Removed report.", extra={"color": typer.colors.YELLOW})
    # --------------------------

    if to == "RESOLVED" and task_id.endswith("-INTEGRATION"):
        import shutil
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
        for child_ws in get_workspace_root().glob(f"{parent_id}-*"):
            if child_ws.is_dir() and child_ws.name != parent_id:
                try:
                    shutil.rmtree(child_ws)
                except Exception as e:
                    global_logger.warning(f"Warning: Could not delete {child_ws.name}: {e}")
                    
        return "Consolidated workspace"

    state["phase"] = to
    state["retry_count"] = 0
    write_json(state_file, state)
    global_logger.info(f"[SUCCESS] Schema validated. State advanced from {current_phase} to {to}", extra={"color": typer.colors.GREEN})
    return f"Advanced from {current_phase} to {to}"
