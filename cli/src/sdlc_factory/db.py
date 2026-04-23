import os
import logging
import psycopg
from pgvector.psycopg import register_vector
from sdlc_factory.utils import get_config, abort

def get_db_connection():
    config = get_config()
    conn_str = config.get("connection_string")
    if not conn_str:
        raise Exception("connection_string is empty in configuration.")
    conn = psycopg.connect(conn_str)
    register_vector(conn)
    return conn

def get_embedding(text: str) -> list[float]:
    """Generates embeddings using Gemini's native API."""
    from google import genai
    
    config = get_config()
    api_key = config.get("vertex_api_key") or config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    
    if not api_key:
        raise Exception("Gemini/Vertex API key is missing.")
        
    client = genai.Client(api_key=api_key, http_options={'timeout': 30000})
    
    try:
        response = client.models.embed_content(
            model='gemini-embedding-001',
            contents=text,
            config={'output_dimensionality': 768}
        )
        return response.embeddings[0].values
    except Exception as e:
        raise Exception(f"Gemini Embedding failed: {e}")
