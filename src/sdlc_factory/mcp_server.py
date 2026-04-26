import sys
import os

# TRITON-PATCH: Prepend the local 'cli/src' tree to the PYTHON PATH so the MCP environment
# utilizes the freshly edited local directory logic over the globally installed /usr/local/lib/ environment!
LOCAL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if LOCAL_PATH not in sys.path:
    sys.path.insert(0, LOCAL_PATH)

import json
from typing import Optional
from mcp.server.fastmcp import FastMCP
from sdlc_factory.utils import setup_global_logger
from sdlc_factory.state import get_pending_task, get_blocked_tasks, do_advance_state
from sdlc_factory.memory import build_context, do_search_codebase, do_store_memory
# Initialize fundamental logger and config routing context
setup_global_logger()

mcp = FastMCP("sdlc-factory")

@mcp.tool()
def query_state(agent: str = None, check_blocked: bool = False) -> str:
    """Invokes 'sdlc-factory query-state'. Returns the pending task or blocked items."""
    if check_blocked:
        blocked = get_blocked_tasks()
        if blocked:
            return json.dumps({"status": "blocked", "tasks": blocked}, indent=2)
        return json.dumps({"status": "no_blocked_tasks"})
        
    if not agent:
        return json.dumps({"status": "error", "message": "Must provide --agent unless using check_blocked"})
        
    task = get_pending_task(agent)
    if task:
        task["status"] = "success"
        return json.dumps(task, indent=2)
    return json.dumps({"status": "no_tasks"})

@mcp.tool()
def context(task_id: str, module: str, agent: str = None) -> str:
    """Invokes 'sdlc-factory context'. Retrieves structural, semantic, and historic memory context for a task."""
    try:
        ctx = build_context(task_id, module, agent=agent)
        return json.dumps(ctx, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def advance_state(task_id: str, to: str, regression: bool = False) -> str:
    """Invokes 'sdlc-factory advance-state'. Advances the SDLC pipeline phase natively."""
    try:
        msg = do_advance_state(task_id, to=to, regression=regression)
        return json.dumps({"status": "success", "message": msg})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def search_codebase(query: str, limit: int = 3) -> str:
    """Invokes 'sdlc-factory search-codebase'. Finds relevant codebase file embeddings via pgvector."""
    try:
        results = do_search_codebase(query, limit=limit)
        if not results:
            return json.dumps({"status": "no_results"})
        return json.dumps({"status": "success", "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def store_memory(agent: str, task_context: str, resolution: str) -> str:
    """Invokes 'sdlc-factory store-memory'. Stores an explicitly vectorized insight securely into Postgres."""
    try:
        msg = do_store_memory(agent, task_context, resolution)
        return json.dumps({"status": "success", "message": msg})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    mcp.run()
