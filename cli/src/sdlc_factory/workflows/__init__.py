from typing import Dict, Optional
from sdlc_factory.workflows.base import WorkflowPlugin
from sdlc_factory.utils import abort

_WORKFLOW_REGISTRY: Dict[str, WorkflowPlugin] = {}

def register_workflow(plugin: WorkflowPlugin):
    """Registers a workflow plugin globally."""
    _WORKFLOW_REGISTRY[plugin.name] = plugin

def get_workflow(name: str) -> WorkflowPlugin:
    """Retrieves a workflow plugin by name, falling back to SDLC if not found."""
    if name not in _WORKFLOW_REGISTRY:
        if name == "sdlc":
            # Lazy load built-in SDLC
            from sdlc_factory.workflows.sdlc.plugin import SdlcWorkflow
            register_workflow(SdlcWorkflow())
        else:
            abort(f"Workflow '{name}' not found. Did you install the plugin?")
            
    return _WORKFLOW_REGISTRY[name]

# Expose base class and registry functions
__all__ = ["WorkflowPlugin", "register_workflow", "get_workflow"]
