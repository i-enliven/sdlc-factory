import json
from typing import Optional
import urllib.request
import urllib.parse
from sdlc_factory.utils import get_config

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

def sdlc_web_search(query: str, domain: Optional[str] = None) -> str:
    """Invokes 'sdlc-factory web-search'. Performs a web search using the internal SearxNG instance."""
    try:
        config = get_config()
        base_url = config.get("searxng_url", "http://sagittarius-a.mara-balance.ts.net:8080")
        
        search_query = query
        if domain:
            search_query += f" site:{domain}"
            
        params = urllib.parse.urlencode({
            'q': search_query,
            'format': 'json'
        })
        url = f"{base_url.rstrip('/')}/search?{params}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30.0) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        results = data.get("results", [])
        top_results = []
        for r in results[:5]:
            top_results.append({
                "title": r.get("title"),
                "url": r.get("url"),
                "content": r.get("content")
            })
            
        return json.dumps({"status": "success", "results": top_results}, indent=2) if top_results else json.dumps({"status": "no_results"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def sdlc_query_traces(query_type: str = "recent", session_id: Optional[str] = None, agent_name: Optional[str] = None, limit: int = 5, include_prompts: bool = False) -> str:
    """Invokes 'sdlc-factory query-traces'. Safely queries OpenTelemetry spans."""
    try:
        from sdlc_factory.db import do_query_traces
        results = do_query_traces(query_type, session_id=session_id, agent_name=agent_name, limit=limit, include_prompts=include_prompts)
        return json.dumps({"status": "success", "results": results}, indent=2) if results else json.dumps({"status": "no_results"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def sdlc_execute_sql(query: str, parameters: Optional[list] = None) -> str:
    """Invokes 'sdlc-factory execute-sql'. Safely executes raw SQL."""
    try:
        from sdlc_factory.db import do_execute_sql
        results = do_execute_sql(query, parameters)
        return json.dumps({"status": "success", "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
