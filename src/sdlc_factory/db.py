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
    """Generates embeddings using vLLM/OpenAI API."""
    from openai import OpenAI
    import os

    config = get_config()
    api_key = config.get("vertex_api_key") or config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    # Use an embedding-specific base_url if provided, otherwise default to Google's OpenAI-compatible endpoint.
    # This prevents embedding requests from hitting the vLLM text-generation node.
    base_url = config.get("embedding_base_url", "https://generativelanguage.googleapis.com/v1beta/openai/")

    api_timeout = float(config.get("api_timeout", 600.0))
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=api_timeout)

    try:
        response = client.embeddings.create(
            model='gemini-embedding-001', # Change this if vLLM uses a different embedding model
            input=text,
            dimensions=768
        )
        return response.data[0].embedding
    except Exception as e:
        raise Exception(f"Embedding failed: {e}")
