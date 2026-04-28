import json
from typing import Optional

from sdlc_factory.state import get_pending_task, get_blocked_tasks, do_advance_state
from sdlc_factory.memory import build_context, do_search_codebase, do_store_memory
def sdlc_query_state(agent: Optional[str] = None, check_blocked: bool = False) -> str:
    """Invokes 'sdlc-factory query-state'. Returns the pending task or blocked items."""
    if check_blocked:
        blocked = get_blocked_tasks()
        return json.dumps({"status": "blocked", "tasks": blocked}, indent=2) if blocked else json.dumps({"status": "no_blocked_tasks"})
    if not agent:
        return json.dumps({"status": "error", "message": "Must provide agent unless check_blocked"})
    task = get_pending_task(agent)
    if task:
        task["status"] = "success"
        return json.dumps(task, indent=2)
    return json.dumps({"status": "no_tasks"})

def sdlc_context(task_id: str, module: str, agent: Optional[str] = None) -> str:
    """Invokes 'sdlc-factory context'. Retrieves structural, semantic, and historic memory context for a task."""
    try:
        return json.dumps(build_context(task_id, module, agent=agent), indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def sdlc_advance_state(task_id: str, to: str, regression: bool = False) -> str:
    """Invokes 'sdlc-factory advance-state'. Advances the SDLC pipeline phase natively."""
    try:
        return json.dumps({"status": "success", "message": do_advance_state(task_id, to=to, regression=regression)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def sdlc_search_codebase(query: str, limit: int = 3) -> str:
    """Invokes 'sdlc-factory search-codebase'. Finds relevant codebase file embeddings via pgvector."""
    try:
        results = do_search_codebase(query, limit=limit)
        return json.dumps({"status": "success", "results": results}, indent=2) if results else json.dumps({"status": "no_results"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def sdlc_store_memory(agent: str, task_context: str, resolution: str) -> str:
    """Invokes 'sdlc-factory store-memory'. Stores an explicitly vectorized insight securely into Postgres."""
    try:
        return json.dumps({"status": "success", "message": do_store_memory(agent, task_context, resolution)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
