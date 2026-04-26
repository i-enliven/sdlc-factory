import json
import os
import shutil
from pathlib import Path
from typer.testing import CliRunner
import sys

from sdlc_factory.cli import app

runner = CliRunner()

def test_config(mocker, tmp_path):
    test_workspace = tmp_path / "test_workspace"
    test_workspace.mkdir()

    result = runner.invoke(app, ["config", "--workspace-root", str(test_workspace)])
    assert result.exit_code == 0

    test_config_file = tmp_path / ".sdlc-factory.json"
    assert test_config_file.exists()

def test_version(mocker):
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    
    mocker.patch("importlib.metadata.version", side_effect=Exception("No pack"))
    import importlib.metadata
    mocker.patch("importlib.metadata.PackageNotFoundError", Exception)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0

def test_init_existing(tmp_path):
    ws_path = tmp_path / "test_workspace" / "test-task"
    ws_path.mkdir(parents=True)
    result = runner.invoke(app, ["init", "--task-id", "test-task"])
    assert result.exit_code == 1

def test_init_missing_file(tmp_path):
    result = runner.invoke(app, ["init", "--task-id", "test-task-missing", "-f", "/fake/file.txt"])
    assert result.exit_code == 1

def test_init(tmp_path):
    result = runner.invoke(app, ["init", "--task-id", "test-task"])
    assert result.exit_code == 0

    ws_path = tmp_path / "test_workspace" / "test-task"
    assert ws_path.exists()
    assert (ws_path / ".state" / "current.json").exists()

def test_init_with_file(tmp_path):
    req_file = tmp_path / "reqs.txt"
    req_file.write_text("my requirements")

    result = runner.invoke(app, ["init", "--task-id", "test-task-2", "-f", str(req_file)])
    assert result.exit_code == 0

def test_query_state_no_agent():
    result = runner.invoke(app, ["query-state"])
    assert result.exit_code == 1

def test_query_state(mocker):
    mocker.patch("sdlc_factory.cli.get_pending_task", return_value={"task_id": "test-task"})
    result = runner.invoke(app, ["query-state", "--agent", "coder"])
    assert result.exit_code == 0
    assert "test-task" in result.stdout

def test_query_state_blocked(mocker):
    mocker.patch("sdlc_factory.cli.get_blocked_tasks", return_value=["t1"])
    result = runner.invoke(app, ["query-state", "--check-blocked"])
    assert result.exit_code == 0
    assert "blocked" in result.stdout

def test_context(mocker):
    mocker.patch("sdlc_factory.cli.build_context", return_value={"ctx": "test"})
    result = runner.invoke(app, ["context", "--task-id", "t1", "--module", "sys"])
    assert result.exit_code == 0
    assert "test" in result.stdout

def test_advance_state(mocker):
    mocker.patch("sdlc_factory.cli.do_advance_state", return_value=True)
    result = runner.invoke(app, ["advance-state", "--task-id", "t1", "--to", "TEST"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.do_advance_state", side_effect=Exception("adv err"))
    result = runner.invoke(app, ["advance-state", "--task-id", "t1", "--to", "TEST"])
    assert result.exit_code == 1

def test_search_codebase(mocker):
    mocker.patch("sdlc_factory.cli.do_search_codebase", return_value=[{"filepath": "f", "content": "c"}])
    result = runner.invoke(app, ["search-codebase", "--query", "hello"])
    assert result.exit_code == 0
    assert "Results" in result.stdout
    
    mocker.patch("sdlc_factory.cli.do_search_codebase", return_value=[])
    result = runner.invoke(app, ["search-codebase", "--query", "hello"])
    assert result.exit_code == 0

def test_index_codebase(mocker):
    mocker.patch("sdlc_factory.cli.do_index_codebase", return_value=True)
    result = runner.invoke(app, ["index-codebase", "--repo-dir", "/tmp"])
    assert result.exit_code == 0

def test_store_memory(mocker):
    mocker.patch("sdlc_factory.cli.do_store_memory", return_value="Stored")
    result = runner.invoke(app, ["store-memory", "--agent", "coder", "--task-context", "ctx", "--resolution", "res"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.do_store_memory", side_effect=Exception("mem err"))
    result = runner.invoke(app, ["store-memory", "--agent", "coder", "--task-context", "ctx", "--resolution", "res"])
    assert result.exit_code == 1

def test_heartbeat(mocker):
    mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", return_value=True)
    result = runner.invoke(app, ["heartbeat"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", return_value=False)
    result = runner.invoke(app, ["heartbeat"])
    assert result.exit_code == 0

def test_task_interactive(mocker):
    mocker.patch("sdlc_factory.agent.execute_agent", return_value="done")
    result = runner.invoke(app, ["task", "--agent", "coder", "--prompt", "hi"])
    assert result.exit_code == 0
    assert "done" in result.stdout
    
    result = runner.invoke(app, ["task", "--agent", "coder"], input="   \n")
    assert result.exit_code == 1
    
    result = runner.invoke(app, ["task", "--agent", "coder"], input="done\n")
    assert result.exit_code == 0

def test_run_cmd(mocker):
    mock_run = mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", side_effect=KeyboardInterrupt)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
