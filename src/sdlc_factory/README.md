# Factory Codebase Map

To navigate the internal structure:

- **`agent.py`**: Interacts dynamically with external AI model providers (Google GenAI) executing inference sessions. 
- **`cli.py` / `main.py`**: The terminal endpoints parsing command arguments using Typer.
- **`db.py`**: PostgreSQL database adapters tracking `pgvector` models and sentence transformers.
- **`mcp_server.py`**: The standard Model Context Protocol wrappers opening the pipeline constraints locally to external agents.
- **`memory.py`**: Retrieval logic extracting architecture embeddings into prompts dynamically.
- **`state.py`**: Transition logic determining what Phase is next and rigorously enforcing JSON schemas.
- **`utils.py`**: Global baseline code formatting loggers, schema classes, and disk I/O.
