import sys
import json
import typer
from typing import Optional
from pathlib import Path
from importlib import metadata
import time

from sdlc_factory.utils import global_logger, abort, get_workspace, get_workspace_root, setup_global_logger, get_config
from sdlc_factory.state import get_pending_task, get_blocked_tasks, do_advance_state
from sdlc_factory.memory import build_context, do_index_codebase, do_search_codebase, do_store_memory
from sdlc_factory.heartbeat import run_heartbeat_cycle

app = typer.Typer(help="SDLC Factory - Passive State Ledger")

@app.callback()
def main_callback():
    setup_global_logger()

@app.command(name="version")
def show_version():
    try:
        ver = metadata.version("sdlc-factory")
        print(f"sdlc-factory version {ver}")
    except metadata.PackageNotFoundError:
        print("sdlc-factory version unknown (not installed)")

@app.command()
def config(workspace_root: str = typer.Option(..., help="Absolute path to the SDLC Factory shared workspace")):
    from sdlc_factory.utils import CONFIG_FILE, write_json
    cfg = {"workspace_root": str(Path(workspace_root).expanduser().resolve())}
    write_json(CONFIG_FILE, cfg)
    global_logger.info(f"Configuration written to {CONFIG_FILE}", extra={"color": typer.colors.GREEN})

@app.command()
def init(
    task_id: str = typer.Option(...),
    requirements_file: Optional[Path] = typer.Option(None, "-f", "--file", help="Path to initial requirements file")
):
    ws = get_workspace(task_id)
    if ws.exists():
        abort(f"Workspace {task_id} already exists.")
        
    for dir_name in [".state", "handoff", "issues", "src", "tests", "docs", "dist"]:
        (ws / dir_name).mkdir(parents=True)
        
    if requirements_file:
        if not requirements_file.exists():
            abort(f"Requirements file not found at: {requirements_file}")
        import shutil
        dest = ws / "handoff" / "RAW_REQUIREMENTS.md"
        shutil.copy2(requirements_file, dest)
        global_logger.info(f"Copied requirements from {requirements_file} to {dest}", extra={"color": typer.colors.CYAN})
        
    from sdlc_factory.utils import write_json
    write_json(ws / ".state" / "current.json", {"phase": "PLANNING", "task_id": task_id})
    global_logger.info(f"[SUCCESS] Initialized workspace for {task_id}", extra={"color": typer.colors.GREEN})

@app.command()
def query_state(agent: str = typer.Option(None), check_blocked: bool = typer.Option(False, "--check-blocked")):
    if check_blocked:
        blocked = get_blocked_tasks()
        if blocked:
            print(json.dumps({"status": "blocked", "tasks": blocked}, indent=2))
        raise typer.Exit(0)

    if not agent:
        abort("Must provide --agent unless using --check-blocked")

    task = get_pending_task(agent)
    if task:
        task["status"] = "success"
        print(json.dumps(task, indent=2))
    raise typer.Exit(0)

@app.command()
def context(task_id: str = typer.Option(...), module: str = typer.Option(...), agent: str = typer.Option(None, help="The active agent role (e.g. coder) for memory resolution.")):
    print(json.dumps(build_context(task_id, module, agent), indent=2))

@app.command()
def advance_state(task_id: str = typer.Option(...), to: str = typer.Option(...), regression: bool = typer.Option(False, "--regression")):
    try:
        do_advance_state(task_id, to, regression)
    except Exception as e:
        abort(str(e))

@app.command()
def index_codebase(repo_dir: str = typer.Option(...)):
    do_index_codebase(repo_dir)

@app.command()
def search_codebase(query: str = typer.Option(...), limit: int = typer.Option(3)):
    results = do_search_codebase(query, limit)
    if not results:
        global_logger.warning("No results found.")
        sys.exit(0)
        
    global_logger.info(f"--- Top {len(results)} Results ---", extra={"color": typer.colors.CYAN, "bold": True})
    for r in results:
        global_logger.info(f"\n File: {r['filepath']}", extra={"color": typer.colors.GREEN, "bold": True})
        print(f"```\n{r['content']}\n```")

@app.command()
def store_memory(agent: str = typer.Option(...), task_context: str = typer.Option(...), resolution: str = typer.Option(...)):
    """Stores an explicitly vectorized insight securely into Postgres pgvector."""
    try:
        msg = do_store_memory(agent, task_context, resolution)
        global_logger.info(msg, extra={"color": typer.colors.GREEN})
    except Exception as e:
        abort(f"RAG Persistence Failure: {e}")

@app.command()
def heartbeat(resume: Optional[str] = typer.Option(None, "--resume", help="Session UUID to resume")):
    """Executes a single pulse of the SDLC Factory autonomous heartbeat."""
    executed = run_heartbeat_cycle(resume_session_id=resume)
    if not executed:
        global_logger.info("💤 No tasks found. Pipeline is idle.")
@app.command()
def task(agent: str = typer.Option(...), prompt: str = typer.Option(None), resume: Optional[str] = typer.Option(None, "--resume", help="Session UUID to resume")):
    """Runs a specific agent in an ad-hoc loop for a given prompt, skipping the heartbeat playbook."""
    if not prompt and not resume:
        typer.secho("🤖 Enter your multi-line prompt. Press Ctrl+D (EOF) when finished:", fg=typer.colors.CYAN, bold=True)
        prompt = sys.stdin.read().strip()
        
    if not prompt and not resume:
        abort("Prompt cannot be empty.")
        
    from sdlc_factory.agent import execute_agent
    
    # Append a generic workdir verification so it uses current directory properly
    if prompt:
        prompt = f"*Workdir*: {Path.cwd().resolve()}\n\n" + prompt
    
    result = execute_agent(agent, prompt, exclude_files=["AGENTS.md", "PROTOCOL.md"], session_id=resume, is_resume=bool(resume))
    typer.secho(f"\n🤖 Agent Reply:\n{result}\n", fg=typer.colors.GREEN)


@app.command()
def run(interval: int = typer.Option(30, help="Seconds to wait between idle heartbeat pulses"), resume: Optional[str] = typer.Option(None, "--resume", help="Session UUID to resume")):
    """Runs the SDLC Factory heartbeat continuously as a long-lived daemon."""
    global_logger.info(f"🚀 Starting continuous SDLC Factory heartbeat...", extra={"color": typer.colors.MAGENTA, "bold": True})
    try:
        while True:
            executed = run_heartbeat_cycle(resume_session_id=resume)
            resume = None # Only resume the first cycle
            if executed:
                time.sleep(2) 
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        global_logger.info("\n🛑 Gracefully shutting down the factory...", extra={"color": typer.colors.YELLOW})
        sys.exit(0)
