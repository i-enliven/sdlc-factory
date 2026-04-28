import json
import shutil
import typer
from pathlib import Path
from typing import Optional
from jsonschema import validate, ValidationError

from sdlc_factory.utils import (
    global_logger, get_config, read_json, 
    write_json, abort, get_workspace_root, get_workspace
)
from sdlc_factory.workflows import get_workflow

def auto_hydrate_payload(ws: Path, phase: str, task_id: str, module_id: str, workflow_name: str):
    """Ensures amnesic agents don't fail schema checks due to boilerplate fields."""
    workflow = get_workflow(workflow_name)
    contract = workflow.schemas.get(phase)
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

def validate_handoff(ws: Path, phase: str, workflow_name: str):
    """Enforces strict JSON schema validation before state advancement."""
    workflow = get_workflow(workflow_name)
    contract = workflow.schemas.get(phase)
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
        workflow_name = state.get("workflow", "sdlc")
        workflow = get_workflow(workflow_name)
        contract = workflow.schemas.get(state.get("phase"))
        if contract:
            handoff_file = ws / "handoff" / contract["file"]
            if handoff_file.exists():
                handoff_file.unlink()
        
        state["phase"] = to_phase
        write_json(state_file, state)
        global_logger.warning(f"⚠️ REGRESSION: {task_id} sent back to {to_phase} (Budget used: {new_retry_count}/{max_retries})")

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

def get_active_workflows() -> set:
    """Returns a set of all active workflow names based on existing workspaces."""
    active_workflows = set(["sdlc"])
    workspace_root = get_workspace_root()
    if workspace_root.exists():
        for ws_path in workspace_root.glob("*"):
            if ws_path.is_dir():
                state_file = ws_path / ".state" / "current.json"
                if state_file.exists():
                    state = read_json(state_file)
                    active_workflows.add(state.get("workflow", "sdlc"))
    return active_workflows

def get_pending_task(agent: str) -> Optional[dict]:
    """Internal helper to fetch the first available task for a specific agent across all active workflows."""
    for wf_name in get_active_workflows():
        try:
            workflow = get_workflow(wf_name)
            task = workflow.get_pending_task(agent, get_workspace_root())
            if task:
                return task
        except Exception:
            continue
    return None

def do_advance_state(task_id: str, to: str, regression: bool = False) -> str:
    ws = get_workspace(task_id)
    state_file = ws / ".state" / "current.json"
    
    if not state_file.exists():
        raise Exception(f"State file missing for {task_id}")
        
    state = read_json(state_file)
    current_phase = state.get("phase")
    workflow_name = state.get("workflow", "sdlc")
    workflow = get_workflow(workflow_name)

    if regression:
        # Delegate regression bubbling to the workflow
        ws, task_id, state, to = workflow.on_regression(ws, task_id, to, state, get_config().get("max_retry_limit", 2))
        
        # Core regression budget handling
        handle_regression(ws, state, to, task_id, get_config())
        return "Regression handled"

    auto_hydrate_payload(ws, current_phase, task_id, state.get("module_id", "SYSTEM"), workflow_name)
    validate_handoff(ws, current_phase, workflow_name)

    # Trigger custom transition hooks
    workflow.on_transition(ws, task_id, current_phase, to, state)

    # --- REGRESSION CLEANUP ---
    regression_file = ws / "handoff" / "regression_report.json"
    if regression_file.exists():
        regression_file.unlink()
        global_logger.info(f"🧹 [CLEANUP] Resolved regression. Removed report.", extra={"color": typer.colors.YELLOW})
    # --------------------------

    # Notice: If `on_transition` handles consolidation, we don't return early here
    # unless we want to bypass writing the new phase to the *current* state file.
    # The original SDLC logic returned early on consolidation because the current workspace
    # was being "bundled into" the parent workspace.
    # We will let the plugin's `on_transition` handle the logic, but if the task ends with `-INTEGRATION`,
    # the workspace might be purged. If the workspace is purged, we shouldn't write to `state_file`.
    if not state_file.exists():
        return "Consolidated workspace (handled by plugin hook)"

    state["phase"] = to
    state["retry_count"] = 0
    write_json(state_file, state)
    global_logger.info(f"[SUCCESS] Schema validated. State advanced from {current_phase} to {to}", extra={"color": typer.colors.GREEN})
    return f"Advanced from {current_phase} to {to}"
