import json
from pathlib import Path
import re
import sys

# Import sdcl_factory functions
sys.path.append("/home/ienliven/Projects/sdlc-factory/src")
from sdlc_factory.db import get_db_connection, get_embedding

def recover():
    sessions_dir = Path.home() / ".sdlc-factory" / "sessions"
    memories = set()

    # Search session files for tool call arguments to sdlc_store_memory
    for session_file in sessions_dir.glob("*.session"):
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            for item in data:
                parts = item.get("parts") or []
                for part in parts:
                    if not part: continue
                    fn_call = part.get("function_call")
                    if fn_call and fn_call.get("name") == "sdlc_store_memory":
                        args = fn_call.get("args") or {}
                        agent = args.get("agent")
                        task_ctx = args.get("task_context")
                        resolution = args.get("resolution")
                        if agent and task_ctx and resolution:
                            memories.add((agent, task_ctx, resolution))
                            
                # Also check text for Historic Insight
                for part in parts:
                    if not part: continue
                    text = part.get("text")
                    if text and "### Historic Insight (" in text:
                        matches = re.findall(r"### Historic Insight \(([^)]+)\)\n\*\*Context\*\*: (.*?)\n\*\*Resolution\*\*:\n```\n(.*?)\n```", text, re.DOTALL)
                        for agent, ctx, res in matches:
                            memories.add((agent, ctx, res))
        except Exception as e:
            print(f"Failed to read {session_file}: {e}")

    print(f"Recovered {len(memories)} unique memories.")
    
    conn = get_db_connection()
    with conn.cursor() as cur:
        for agent, ctx, res in memories:
            try:
                emb = get_embedding(res)
                cur.execute("INSERT INTO agent_memories (agent_role, task_context, resolution, embedding) VALUES (%s, %s, %s, %s)", 
                           (agent, ctx, res, emb))
                print(f"Inserted memory for {agent}")
            except Exception as e:
                print(f"Failed to insert memory for {agent}: {e}")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    recover()
