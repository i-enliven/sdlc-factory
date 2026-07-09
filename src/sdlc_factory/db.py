import os
import logging
import psycopg
import time
from pgvector.psycopg import register_vector
from sdlc_factory.utils import get_config, abort, global_logger

def get_db_connection():
    config = get_config()
    conn_str = config.get("connection_string")
    if not conn_str:
        raise Exception("connection_string is empty in configuration.")
    backoff_intervals = [5, 10, 20, 40, 80]
    for idx, wait_time in enumerate(backoff_intervals):
        try:
            conn = psycopg.connect(conn_str, connect_timeout=5)
            register_vector(conn)
            return conn
        except psycopg.OperationalError:
            global_logger.warning(f"⚠️ PostgreSQL is unreachable. Retrying in {wait_time} seconds (Attempt {idx+1}/{len(backoff_intervals)})...")
            time.sleep(wait_time)
            
    try:
        conn = psycopg.connect(conn_str, connect_timeout=5)
        register_vector(conn)
        return conn
    except psycopg.OperationalError as e:
        raise Exception(f"Failed to connect to PostgreSQL after exponential backoff: {e}")

# Cache the model globally so it isn't loaded from disk on every invocation
_EMBEDDING_MODEL = None

def get_embedding(text: str) -> list[float]:
    """Generates embeddings using local SentenceTransformers."""
    global _EMBEDDING_MODEL
    
    if _EMBEDDING_MODEL is None:
        from sentence_transformers import SentenceTransformer
        import torch
        
        # Load the model. all-mpnet-base-v2 naturally outputs 768 dimensions.
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _EMBEDDING_MODEL = SentenceTransformer('sentence-transformers/all-mpnet-base-v2', device=device)
        
    try:
        # Generate the embedding
        embedding = _EMBEDDING_MODEL.encode(text)
        return embedding.tolist()
    except Exception as e:
        raise Exception(f"Local Embedding failed: {e}")
def do_query_traces(query_type: str, session_id: str = None, agent_name: str = None, limit: int = 5, include_prompts: bool = False) -> list[dict]:
    """Queries OpenTelemetry spans from the database safely with truncation."""
    import json
    
    if limit > 20:
        limit = 20
        
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            base_query = """
                SELECT id, name, status_code, status_message, start_time,
                       attributes->'session'->>'id' as session_id
            """
            
            if include_prompts:
                # Safely truncate the massive LLM prompt and output fields to 1000 chars max
                base_query += """,
                       SUBSTRING(attributes->>'llm.prompt' FROM 1 FOR 1000) as llm_prompt,
                       SUBSTRING(attributes->>'llm.output' FROM 1 FOR 1000) as llm_output,
                       SUBSTRING(events::text FROM 1 FOR 1000) as events_sample
                """
                
            base_query += "\nFROM spans\nWHERE 1=1"
            
            params = []
            
            if query_type == "errors":
                base_query += " AND status_code = 'ERROR'"
            elif query_type == "llm":
                base_query += " AND name = 'ChatCompletion'"
                
            if session_id:
                base_query += " AND attributes->'session'->>'id' = %s"
                params.append(session_id)
                
            if agent_name:
                base_query += " AND attributes->'session'->>'id' LIKE %s"
                params.append(f"{agent_name}-%")
                
            base_query += "\nORDER BY start_time DESC\nLIMIT %s"
            params.append(limit)
            
            cur.execute(base_query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            
            results = []
            for row in rows:
                row_dict = dict(zip(columns, row))
                # Convert datetime objects to string for JSON serialization
                if 'start_time' in row_dict and row_dict['start_time']:
                    row_dict['start_time'] = row_dict['start_time'].isoformat()
                results.append(row_dict)
                
            return results
    finally:
        conn.close()

def do_execute_sql(query: str, parameters: list = None) -> list[dict]:
    """Executes a raw SQL query safely, ensuring all strings are truncated to prevent context overflow."""
    import json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, parameters or [])
            # Some queries might not return data
            if not cur.description:
                conn.commit()
                return [{"status": "success", "message": "Query executed successfully with no returned rows."}]
            
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            
            results = []
            for row in rows:
                row_dict = {}
                for col_name, val in zip(columns, row):
                    if val is None:
                        row_dict[col_name] = None
                    elif isinstance(val, (dict, list)):
                        # Safely truncate JSON outputs
                        val_str = json.dumps(val)
                        row_dict[col_name] = val_str[:1500] + ("...[TRUNCATED]" if len(val_str) > 1500 else "")
                    elif isinstance(val, str):
                        row_dict[col_name] = val[:1500] + ("...[TRUNCATED]" if len(val) > 1500 else "")
                    else:
                        row_dict[col_name] = str(val)[:1500]
                results.append(row_dict)
                
            return results
    except Exception as e:
        # Prevent huge SQL error dumps
        raise Exception(f"SQL Execution Error: {str(e)[:500]}")
    finally:
        conn.close()
