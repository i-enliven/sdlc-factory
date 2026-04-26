import json
import typer
import uuid
from datetime import datetime
from typing import Optional
from pathlib import Path

from sdlc_factory.utils import SCHEMAS, global_logger
from sdlc_factory.state import get_pending_task, get_blocked_tasks
from sdlc_factory.memory import build_context
from sdlc_factory.agent import execute_agent

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
        global_logger.info(f"🧠 Dispatching task to reasoner for {task['task_id']}...", extra={"color": typer.colors.CYAN})
        # execute_agent("reasoner", prompt)
        return True # Exit cycle to enforce strict cooldown

    # 2. Standard Queue for Worker Agents
    agents = ["planner", "architect", "tester", "coder", "deployer", "monitor"]
    
    for agent in agents:
        task = get_pending_task(agent)
        if task:
            target_schema = SCHEMAS.get(task.get("phase"), {}).get("schema", {})
            schema_str = json.dumps(target_schema, indent=2) if target_schema else "No strict schema defined."
            
            # Pre-fetch the exact fast-dieted boundaries natively
            try:
                hydrated_context = build_context(task["task_id"], task["assigned_module"], agent=agent)
                context_str = json.dumps(hydrated_context, indent=2)
            except Exception as e:
                context_str = f"Error loading context: {str(e)}. Proceed with caution."

            phase = task.get("phase")
            workspace = Path(task["workspace"])
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
            global_logger.info(f"🚀 Dispatching {agent} | Phase: {task['phase']} (Module: {task['assigned_module']}) | Session: {session_id}", extra={"color": typer.colors.GREEN})
            result = execute_agent(agent, prompt, session_id=session_id, is_resume=is_resume)
            typer.secho(f"\n🤖 Agent Reply:\n{result}\n", fg=typer.colors.GREEN)
            return True # Exit cycle to enforce strict cooldown

    return False # No tasks found
