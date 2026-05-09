from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
import logging

class WorkflowPlugin(ABC):
    """
    Abstract base class for SDLC Factory workflows.
    Defines the contract for custom autonomous loops.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The identifier of the workflow (e.g., 'sdlc')."""
        pass

    @property
    @abstractmethod
    def agents_dir(self) -> Path:
        """The absolute path to the directory containing agent instruction files (SOUL.md)."""
        pass

    @property
    @abstractmethod
    def schemas(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns a mapping of phases to their validation schemas.
        Example: {
            "PLANNING": {"file": "spec_payload.json", "schema": {...}}
        }
        """
        pass

    @property
    @abstractmethod
    def agents_list(self) -> List[str]:
        """Returns the ordered list of agents that the heartbeat should pulse over."""
        pass

    @abstractmethod
    def get_pending_task(self, agent: str, workspace_root: Path) -> Optional[dict]:
        """
        Resolves the next available task for the given agent.
        """
        pass

    @abstractmethod
    def get_phase_context(self, phase: str, workspace: Path) -> str:
        """
        Returns formatted context (e.g., file contents) needed for the given phase.
        """
        pass

    @abstractmethod
    def on_transition(self, ws: Path, task_id: str, current_phase: str, to_phase: str, state: dict) -> None:
        """
        Hook triggered during a state transition. Allows for scattering, gathering,
        or any custom logic when advancing phases.
        """
        pass

    @abstractmethod
    def on_regression(self, ws: Path, task_id: str, to_phase: str, state: dict, max_retries: int) -> None:
        """
        Hook triggered when an agent escalates a regression.
        Handles error budgeting, trace writing, and workspace bubbling.
        """
        pass
    @property
    @abstractmethod
    def tools(self) -> List[Dict[str, Any]]:
        """Returns a list of workflow-specific tools for the agent."""
        pass

    def process_tool_call(self, call: Any, session_cwd: Path, cli_timeout: int, log_prefix: str, agent_tracer: logging.Logger) -> Tuple[Dict[str, Any], Path]:
        """
        Processes a workflow-specific tool call by dynamically dispatching to `handle_<tool_name>`.
        Returns a tuple of (tool_response_dict, updated_session_cwd).
        """
        from sdlc_factory.utils import global_logger
        
        handler_name = f"handle_{call.function.name}"
        handler = getattr(self, handler_name, None)
        
        if handler and callable(handler):
            return handler(call, session_cwd, cli_timeout, log_prefix, agent_tracer)
            
        # Fallback for unrecognized tool in this workflow
        output = f"SYSTEM ERROR: Tool '{call.function.name}' is not handled by this workflow."
        global_logger.warning(f"❌ Unhandled plugin tool '{call.function.name}'")
        agent_tracer.info(f"[OUTPUT]:\n{output}\n")
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.function.name,
            "content": output
        }, session_cwd
