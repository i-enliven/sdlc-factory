import pytest
from pathlib import Path
from sdlc_factory.utils import write_json, read_json
from sdlc_factory.state import (
    validate_handoff, handle_regression, auto_hydrate_payload,
    get_blocked_tasks, get_pending_task, do_advance_state
)
from sdlc_factory.workflows.sdlc.plugin import SdlcWorkflow

def test_auto_hydrate_payload(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / "handoff").mkdir()
    
    mock_schema = {"file": "arch_payload.json"}
    mocker.patch("sdlc_factory.workflows.sdlc.plugin.SdlcWorkflow.schemas", new_callable=mocker.PropertyMock, return_value={"ARCHITECTURE": mock_schema})
    
    auto_hydrate_payload(ws, "ARCHITECTURE", "t1", "sys", "sdlc")
    
    data = read_json(ws / "handoff" / "arch_payload.json")
    assert data["status"] == "success"
    assert data["phase_completed"] == "ARCHITECTURE"

def test_validate_handoff_passes(mocker, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "handoff").mkdir()
    (ws / "handoff" / "requirements.json").write_text('{"phase_completed": "PLANNING", "requirements": []}')
    
    mock_schema = {"file": "requirements.json", "schema": {"type": "object"}}
    mocker.patch("sdlc_factory.workflows.sdlc.plugin.SdlcWorkflow.schemas", new_callable=mocker.PropertyMock, return_value={"PLANNING": mock_schema})
    
    validate_handoff(ws, "PLANNING", "sdlc")

def test_validate_handoff_fails(mocker, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "handoff").mkdir()
    (ws / "handoff" / "requirements.json").write_text('{"bad": "data"}')
    
    mock_schema = {"file": "requirements.json", "schema": {"type": "object", "required": ["req"]}}
    mocker.patch("sdlc_factory.workflows.sdlc.plugin.SdlcWorkflow.schemas", new_callable=mocker.PropertyMock, return_value={"PLANNING": mock_schema})
    
    with pytest.raises(ValueError):
        validate_handoff(ws, "PLANNING", "sdlc")

def test_handle_regression_budget_exceeded(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".state").mkdir()
    (ws / "issues").mkdir()
    (ws / "handoff").mkdir()
    
    state = {"phase": "CODING", "retry_count": 1}
    config = {"max_retry_limit": 2}
    
    handle_regression(ws, state, "PLANNING", "t1", config)
    assert state["phase"] == "BLOCKED"
    assert (ws / "issues" / "ISSUE-FATAL.md").exists()

def test_do_advance_state_consolidate(mocker, tmp_path, mock_config):
    int_ws = tmp_path / "test_workspace" / "t1-INTEGRATION"
    int_ws.mkdir(parents=True)
    (int_ws / ".state").mkdir()
    (int_ws / "src").mkdir()
    (int_ws / "src" / "app.py").write_text("code")
    write_json(int_ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "INTEGRATION_ASSEMBLY"})

    parent = tmp_path / "test_workspace" / "t1"
    parent.mkdir(parents=True)
    (parent / ".state").mkdir()
    write_json(parent / ".state" / "current.json", {"workflow": "sdlc", "phase": "AWAITING_MODULES"})

    mocker.patch("sdlc_factory.state.auto_hydrate_payload")
    mocker.patch("sdlc_factory.state.validate_handoff")

    res = do_advance_state("t1-INTEGRATION", "RESOLVED")
    assert "Consolidated" in res
    assert (parent / "src" / "app.py").exists()

def test_get_blocked_tasks(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / ".state").mkdir()
    (ws / "issues").mkdir()
    write_json(ws / ".state" / "current.json", {"phase": "BLOCKED"})
    (ws / "issues" / "ISSUE-FATAL.md").write_text("bad")
    
    res = get_blocked_tasks()
    assert len(res) == 1
    assert res[0]["task_id"] == "t1"

def test_get_pending_task(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / ".state").mkdir()
    write_json(ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "PLANNING"})
    
    res = get_pending_task("planner")
    assert res["task_id"] == "t1"

def test_do_advance_state(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / ".state").mkdir()
    write_json(ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "PLANNING"})
    
    mocker.patch("sdlc_factory.state.auto_hydrate_payload")
    mocker.patch("sdlc_factory.state.validate_handoff")
    
    do_advance_state("t1", "ARCHITECTURE")
    state = read_json(ws / ".state" / "current.json")
    assert state["phase"] == "ARCHITECTURE"

def test_do_advance_state_regression(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / ".state").mkdir()
    write_json(ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "CODING", "retry_count": 0})
    mocker.patch("sdlc_factory.state.get_config", return_value={"max_retry_limit": 2})
    
    do_advance_state("t1", "ARCHITECTURE", regression=True)
    state = read_json(ws / ".state" / "current.json")
    assert state["phase"] == "ARCHITECTURE"
    assert state["retry_count"] == 1
