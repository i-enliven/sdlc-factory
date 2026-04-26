from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from pathlib import Path

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
