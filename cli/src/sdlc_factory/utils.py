import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any
import typer

global_logger = logging.getLogger("sdlc_factory")

# ==========================================
# STRICT DATA CONTRACTS (JSON SCHEMAS)
# ==========================================
SCHEMAS = {
    "PLANNING": {"file": "spec_payload.json", "schema": {"oneOf": [{"type": "object", "required": ["status", "phase_completed", "product_spec_hash", "identified_modules"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "PLANNING"}, "product_spec_hash": {"type": "string"}, "identified_modules": {"type": "array", "items": {"type": "string"}}}}, {"type": "object", "required": ["status", "phase_completed", "rfc_file_path"], "properties": {"status": {"const": "rfc_requested"}, "phase_completed": {"const": "PLANNING"}, "rfc_file_path": {"const": "docs/RFC.md"}}}]}},
    "ARCHITECTURE": {"file": "arch_payload.json", "schema": {"oneOf": [{"type": "object", "required": ["status", "phase_completed", "api_contracts_hash", "vertical_slices"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "ARCHITECTURE"}, "api_contracts_hash": {"type": "string"}, "vertical_slices": {"type": "array", "items": {"type": "object", "required": ["module_name", "assigned_agent"], "properties": {"module_name": {"type": "string"}, "assigned_agent": {"type": "string"}, "context_queries": {"type": "array", "items": {"type": "string"}}}}}}},{"type": "object","required": ["status", "phase_completed", "rfc_file_path"],"properties": {"status": {"const": "rfc_requested"},"phase_completed": {"const": "ARCHITECTURE"},"rfc_file_path": {"const": "docs/RFC.md"}}}]}},
    "TEST_DESIGN": {"file": "test_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "test_files_created"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "TEST_DESIGN"}, "module_id": {"type": "string"}, "test_files_created": {"type": "array", "items": {"type": "string"}}}}},
    "CODING": {"file": "code_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "execution_metrics", "artifacts", "qa_handoff"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "CODING"}, "module_id": {"type": "string"}, "execution_metrics": {"type": "object", "required": ["tool_calls_made", "compilation_passed"], "properties": {"tool_calls_made": {"type": "number"}, "compilation_passed": {"const": True}}}, "artifacts": {"type": "array"}, "qa_handoff": {"type": "object", "required": ["test_target", "exec_command"], "properties": {"test_target": {"type": "string"}, "exec_command": {"type": "string"}}}}}},
    "INTEGRATION_ASSEMBLY": {"file": "code_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "execution_metrics", "artifacts", "qa_handoff"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "INTEGRATION_ASSEMBLY"}, "module_id": {"type": "string"}, "execution_metrics": {"type": "object", "required": ["tool_calls_made", "compilation_passed"], "properties": {"tool_calls_made": {"type": "number"}, "compilation_passed": {"const": True}}}, "artifacts": {"type": "array"}, "qa_handoff": {"type": "object", "required": ["test_target", "exec_command"], "properties": {"test_target": {"type": "string"}, "exec_command": {"type": "string"}}}}}},
    "QA_REVIEW": {"file": "qa_report.json", "schema": {"type": "object", "required": ["status", "phase_completed", "module_id", "all_tests_passed"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "QA_REVIEW"}, "module_id": {"type": "string"}, "all_tests_passed": {"const": True}, "failing_tests": {"type": "array"}}}},
    "INTEGRATION_TESTING": {"file": "integration_report.json", "schema": {"type": "object", "required": ["status", "phase_completed", "e2e_metrics", "assembled_artifacts_hash"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "INTEGRATION_TESTING"}, "e2e_metrics": {"type": "object", "required": ["scenarios_executed", "critical_paths_passed"], "properties": {"scenarios_executed": {"type": "number"}, "critical_paths_passed": {"const": True}}}, "assembled_artifacts_hash": {"type": "string"}}}},
    "DEPLOY": {"file": "deploy_payload.json", "schema": {"type": "object", "required": ["status", "phase_completed", "artifact_path", "archive_type", "artifact_hash", "entry_command"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "DEPLOY"}, "artifact_path": {"type": "string"}, "archive_type": {"enum": ["tar.gz", "zip"]}, "artifact_hash": {"type": "string"}, "entry_command": {"type": "string"}}}},
    "MONITOR": {"file": "health_status.json", "schema": {"type": "object", "required": ["status", "phase_completed", "health_check_passed", "final_resolution"], "properties": {"status": {"const": "success"}, "phase_completed": {"const": "MONITOR"}, "health_check_passed": {"const": True}, "smoke_test_output": {"type": "string"}, "final_resolution": {"const": "RESOLVED"}}}},
    "REGRESSION": {"file": "regression_report.json", "schema": {"type": "object", "required": ["status", "indictment_metadata", "diagnostic_trace"], "properties": {"status": {"const": "success"}, "indictment_metadata": {"type": "object", "required": ["source_phase", "target_phase", "error_category"], "properties": {"source_phase": {"type": "string"}, "target_phase": {"type": "string"}, "error_category": {"enum": ["COMPILE_ERROR", "LOGIC_MISMATCH", "CONTRACT_VIOLATION", "INFRA_FAILURE"]}}}, "diagnostic_trace": {"type": "object", "required": ["observed_behavior", "stack_trace"]}}}}
}

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
