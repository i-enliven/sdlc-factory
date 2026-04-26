import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any
import typer

global_logger = logging.getLogger("sdlc_factory")


CONFIG_FILE = Path.home() / ".sdlc-factory.json"

def read_json(path: Path, default: Any = None) -> Any:
    """Safe JSON reader."""
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default if default is not None else {}

def write_json(path: Path, data: dict):
    """Atomic-like JSON writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def abort(msg: str, code: int = 1):
    global_logger.error(f"ERROR: {msg}")
    sys.exit(code)

def get_config() -> dict:
    if not CONFIG_FILE.exists():
        abort("Configuration missing. Check your ~/.sdlc-factory.json")
    return read_json(CONFIG_FILE)

def setup_global_logger():
    log_dir = Path.home() / ".gemini" / "antigravity" / "logs" / "sdlc-factory"
    
    try:
        cfg = get_config()
        if "log_path" in cfg:
            log_dir = Path(cfg["log_path"]).expanduser().resolve()
    except Exception:
        pass

    log_dir.mkdir(parents=True, exist_ok=True)
    app_log = log_dir / "app.log"
    
    global_logger.setLevel(logging.INFO)
    global_logger.propagate = False
    
    if not global_logger.handlers:
        fh = logging.handlers.RotatingFileHandler(app_log, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] [%(levelname)s] %(message)s"))
        global_logger.addHandler(fh)

        class ExcludeAgentFilter(logging.Filter):
            def filter(self, record):
                return not record.name.startswith("sdlc_factory.agent")

        class TyperLoggerHandler(logging.Handler):
            def emit(self, record):
                msg = self.format(record)
                
                truncate_limit = getattr(record, "truncate_console", None)
                if truncate_limit and len(msg) > truncate_limit:
                    msg = msg[:truncate_limit] + "..."
                    
                c = getattr(record, "color", None)
                if c is None:
                    if record.levelno >= logging.ERROR:
                        c = typer.colors.RED
                    elif record.levelno >= logging.WARNING:
                        c = typer.colors.YELLOW
                    else:
                        c = typer.colors.WHITE
                
                is_err = record.levelno >= logging.ERROR
                bold = getattr(record, "bold", False)
                typer.secho(msg, fg=c, err=is_err, bold=bold)

        th = TyperLoggerHandler()
        th.setFormatter(logging.Formatter("%(message)s"))
        th.addFilter(ExcludeAgentFilter())
        global_logger.addHandler(th)

def get_workspace_root() -> Path:
    config = get_config()
    if not config.get("workspace_root"):
        abort("'workspace_root' not set. Check your ~/.sdlc-factory.json")
    return Path(config["workspace_root"])

def get_workspace(task_id: str) -> Path:
    return get_workspace_root() / task_id
