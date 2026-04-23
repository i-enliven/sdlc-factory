-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create the table for codebase embeddings
CREATE TABLE codebase_embeddings (
    id SERIAL PRIMARY KEY,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(768) -- Gemini embeddings are 768 dimensions
);
CREATE INDEX ON codebase_embeddings USING hnsw (embedding vector_cosine_ops);

-- Create the table for agent insights and memory
CREATE TABLE agent_memories (
    id SERIAL PRIMARY KEY,
    agent_role TEXT NOT NULL,
    task_context TEXT NOT NULL,
    resolution TEXT NOT NULL,
    embedding VECTOR(768)
);
CREATE INDEX ON agent_memories USING hnsw (embedding vector_cosine_ops);

