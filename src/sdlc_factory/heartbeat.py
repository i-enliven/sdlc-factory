import json
import typer
import uuid
from datetime import datetime
from typing import Optional
from pathlib import Path

from sdlc_factory.utils import global_logger, get_workspace_root, read_json
from sdlc_factory.state import get_blocked_tasks
from sdlc_factory.memory import build_context
from sdlc_factory.agent import execute_agent
from sdlc_factory.workflows import get_workflow

def run_heartbeat_cycle(resume_session_id: Optional[str] = None) -> bool:
    """Executes one pass of the pipeline. Returns True if a task was processed."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    global_logger.debug(f"⏱️  Pulse Executed at {timestamp}")

    # 1. The Reasoner's Domain (Highest Priority)
    blocked_tasks = get_blocked_tasks()
    if blocked_tasks:
        global_logger.error("🔴 BLOCKED STATE DETECTED. Waking Reasoner...", extra={"bold": True})
        task = blocked_tasks[0] # Handle one block at a time
        
        prompt = (
            f"HEARTBEAT_WAKEUP: FATAL PIPELINE BLOCK. Task ID: {task['task_id']}. "
            f"Review issue at: {task['issue_file']}. \n"
            "Execute your assigned playbook immediately to resolve the blockage.\n"
            f"Payload: {json.dumps(task)}"
        )
        global_logger.info(f"🧠 Dispatching task to reasoner for {task['task_id']}...", extra={"color": typer.colors.GREEN})
        # execute_agent("reasoner", prompt)
        return True # Exit cycle to enforce strict cooldown

    # 2. Standard Queue for Worker Agents
    active_workflows = set(["sdlc"])
    workspace_root = get_workspace_root()
    if workspace_root.exists():
        for ws_path in workspace_root.glob("*"):
            if ws_path.is_dir():
                state_file = ws_path / ".state" / "current.json"
                if state_file.exists():
                    state = read_json(state_file)
                    active_workflows.add(state.get("workflow", "sdlc"))

    for wf_name in active_workflows:
        try:
            workflow = get_workflow(wf_name)
        except Exception as e:
            global_logger.warning(f"Could not load workflow plugin '{wf_name}': {e}")
            continue

        for agent in workflow.agents_list:
            if resume_session_id and agent != resume_session_id.split("-")[0]:
                continue
            
            task = workflow.get_pending_task(agent, workspace_root)
            if task:
                target_schema = workflow.schemas.get(task.get("phase"), {}).get("schema", {})
                schema_str = json.dumps(target_schema, indent=2) if target_schema else "No strict schema defined."
                
                # Pre-fetch the exact fast-dieted boundaries natively
                try:
                    hydrated_context = build_context(task["task_id"], task["assigned_module"], agent=agent)
                    context_str = json.dumps(hydrated_context, indent=2)
                except Exception as e:
                    context_str = f"Error loading context: {str(e)}. Proceed with caution."

                phase = task.get("phase")
                workspace = Path(task["workspace"])
                
                phase_context = workflow.get_phase_context(phase, workspace)
                if phase_context:
                    phase_context = f"## PHASE CONTEXT ({phase})\n{phase_context}\n"

                prompt = (
                    "# 🟢 HEARTBEAT_WAKEUP\n"
                    "You have been routed ONE isolated task. \n"
                    f"*Task ID*: {task['task_id']}\n"
                    f"*Module*: {task['assigned_module']}\n"
                    f"*Workdir*: {task['workspace']}\n"
                    f"*Phase*: {task['phase']}\n\n"
                    f"{phase_context}"
                    "## SYSTEM CONTEXT\n"
                    f"```json\n{context_str}\n```\n\n"
                    "The SDLC Protocol and your Playbook are your core instructions.\n"
                    f"CRITICAL: If successful, your handoff JSON must strictly validate against this schema:\n```json\n{schema_str}\n```\n"
                    "If you learn an architecture-critical lesson, permanently save it for your future self using the native `sdlc_store_memory` tool.\n"
                )
                session_id = resume_session_id or f"{agent}-{str(uuid.uuid4())[:6]}"
                is_resume = bool(resume_session_id)
                from sdlc_factory.telemetry import setup_telemetry
                from sdlc_factory.utils import get_config
                setup_telemetry(get_config())
                global_logger.info(f"🚀 Dispatching {agent} (Workflow: {wf_name}) | Phase: {task['phase']} (Module: {task['assigned_module']}) | Session: {session_id}", extra={"color": typer.colors.GREEN})
                result = execute_agent(agent, prompt, exclude_files=None, session_id=session_id, is_resume=is_resume, workflow_name=wf_name)
                typer.secho(f"\n🤖 Agent Reply:\n{result}\n", fg=typer.colors.MAGENTA)
                return True # Exit cycle to enforce strict cooldown

    return False # No tasks found
