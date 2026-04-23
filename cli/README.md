# SDLC Factory Core Backend

The Python-based backend that drives the SDLC pipeline, memory states, CLI commands, and Model Context Protocol integrations. It natively interacts with PostgreSQL/pgvector for long-term insight persistence. 

## Development

1. **Install Python Packages**
   We utilize `uv` to drastically speed up virtual environment locking. 
   ```bash
   uv sync
   ```

2. **Database Initialization**
   Run the local Dockerized Postgres database for semantic vectors:
   ```bash
   docker compose up -d
   ```

3. **Running the Pipeline**
   ```bash
   uv run sdlc-factory
   ```

4. **Testing Code Quality**
   Verify framework robustness by running the test suite with coverage reporting:
   ```bash
   uv run pytest tests/ --cov=src/sdlc_factory --cov-report=term-missing
   ```
