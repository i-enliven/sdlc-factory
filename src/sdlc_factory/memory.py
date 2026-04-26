import re
from pathlib import Path
from typing import Optional
import typer

from sdlc_factory.utils import read_json, get_workspace, global_logger
from sdlc_factory.db import get_db_connection, get_embedding


def check_regression(ws: Path) -> str:
    """Checks for an active regression report and formats it for context injection."""
    regression_file = ws / "handoff" / "regression_report.json"
    if regression_file.exists():
        try:
            data = regression_file.read_text(encoding="utf-8")
            return f"### ACTIVE REGRESSION DETECTED\n```json\n{data}\n```\n"
        except Exception as e:
            return f"### ACTIVE REGRESSION DETECTED\nError reading regression file: {e}\n"
    return "No active regression reported."

def build_context(task_id: str, module: str, agent: Optional[str] = None) -> dict:
    ws = get_workspace(task_id)
    
    # 1. Fetch Regression Context
    regression_context = check_regression(ws)
    
    structural_context = ""
    contracts_file = ws / "docs" / "API_CONTRACTS.md"
    if contracts_file.exists():
        full_text = contracts_file.read_text()
        
        env_match = re.search(r"## > BEGIN_ENVIRONMENT\n(.*?)\n## > END_ENVIRONMENT", full_text, re.DOTALL)
        if env_match:
            env_body = env_match.group(1).strip()
            structural_context += f"## > BEGIN_ENVIRONMENT\n{env_body}\n## > END_ENVIRONMENT\n\n"

        match = re.search(rf"## > BEGIN_MODULE: {module}\n(.*?)\n## > END_MODULE", full_text, re.DOTALL)
        if match: 
            module_body = match.group(1).strip()
            structural_context += f"## > BEGIN_MODULE: {module}\n{module_body}\n## > END_MODULE: {module}"

            if "Type:** ORCHESTRATOR" in module_body:
                all_modules = re.findall(r"## > BEGIN_MODULE: ([a-zA-Z0-9_-]+)\n(.*?)\n## > END_MODULE", full_text, re.DOTALL)
                dependencies_context = []

                for mod_name, mod_body in all_modules:
                    if mod_name == module: continue
                    if mod_name in module_body:
                        sig_match = re.search(r"\*\*Signature:\*\*\s*([^\n]+)", mod_body)
                        signature = sig_match.group(1).strip() if sig_match else "Signature not defined"
                        dependencies_context.append(f"- **{mod_name}**: {signature}")

                if dependencies_context:
                    structural_context += "\n\n### Injected Routing Dependencies (For Mocking Boundaries)\n"
                    structural_context += "\n".join(dependencies_context)

    semantic_context = []
    arch_data = read_json(ws / "handoff" / "arch_payload.json")
    queries = next((s.get("context_queries", []) for s in arch_data.get("vertical_slices", []) if s.get("module_name") == module), [])
            
    if queries:
        with get_db_connection().cursor() as cur:
            for query in queries:
                cur.execute("SELECT file_path, content FROM codebase_embeddings ORDER BY embedding <=> %s::vector LIMIT 3", (get_embedding(query),))
                for filepath, chunk in cur.fetchall():
                    semantic_context.append(f"### File: {filepath}\n```\n{chunk}\n```")

    memory_context = []
    if agent:
        try:
            with get_db_connection().cursor() as cur:
                cur.execute("SELECT task_context, resolution FROM agent_memories WHERE agent_role = %s ORDER BY embedding <=> %s::vector LIMIT 3", (agent, get_embedding(module)))
                for task_ctx, resolution in cur.fetchall():
                    memory_context.append(f"### Historic Insight ({agent})\n**Context**: {task_ctx}\n**Resolution**:\n```\n{resolution}\n```")
        except Exception as e:
            memory_context.append(f"Memory Retrieval Failed: {e}")

    if not structural_context:
        structural_context = "No structural boundaries found (API_CONTRACTS.md missing or module not defined)."
    if not semantic_context:
        semantic_context = ["No legacy semantic snippet queries found for this module."]
    if not memory_context:
        memory_context = [f"No historic pgvector insights found for {agent or 'this role'}."]

    return {
        "status": "success", 
        "module_id": module, 
        "regression_context": regression_context,
        "structural_context": structural_context, 
        "semantic_context": "\n\n".join(semantic_context),
        "memory_insights": "\n\n".join(memory_context)
    }

def do_index_codebase(repo_dir: str):
    target = Path(repo_dir).expanduser().resolve()
    conn = get_db_connection()
    BANNED_DIRS = {'node_modules', 'venv', '.venv', 'env', '.env', '__pycache__', 'dist', 'build', 'site-packages', 'sdlc_factory.egg-info'}
    
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE codebase_embeddings")
        
        for filepath in target.rglob("*"):
            try: rel_path = filepath.relative_to(target)
            except ValueError: continue

            if any(part.startswith('.') for part in rel_path.parts) or any(part in BANNED_DIRS for part in rel_path.parts):
                continue

            if filepath.is_file() and filepath.suffix in ['.ts', '.js', '.py', '.go', '.md']:
                try:
                    content = filepath.read_text(encoding='utf-8')
                    for i, raw_chunk in enumerate(content.split("\n\n")):
                        cleaned = '\n'.join(line.rstrip() for line in re.sub(r'\n{3,}', '\n\n', raw_chunk).split('\n')).strip()
                        if len(cleaned) < 15 or sum(c.isalnum() for c in cleaned) < 10:
                            continue
                        cur.execute("INSERT INTO codebase_embeddings (file_path, chunk_index, content, embedding) VALUES (%s, %s, %s, %s)", (str(rel_path), i, cleaned, get_embedding(cleaned)))
                except Exception as e:
                    global_logger.error(f"Error processing {rel_path}: {e}")
        conn.commit()
    global_logger.info("[SUCCESS] Codebase indexed.", extra={"color": typer.colors.GREEN})

def do_search_codebase(query: str, limit: int = 3) -> list:
    with get_db_connection().cursor() as cur:
        cur.execute("SELECT file_path, content FROM codebase_embeddings ORDER BY embedding <=> %s::vector LIMIT %s", (get_embedding(query), limit))
        results = cur.fetchall()
    return [{"filepath": fp, "content": ct} for fp, ct in results] if results else []

def do_store_memory(agent: str, task_context: str, resolution: str) -> str:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agent_memories (agent_role, task_context, resolution, embedding) VALUES (%s, %s, %s, %s)", 
                   (agent, task_context, resolution, get_embedding(resolution)))
    conn.commit()
    conn.close()
    return f"[{agent} MEMORY STORED]: Embedded dimension coordinates written to DB natively."
