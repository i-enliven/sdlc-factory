import pytest
from pathlib import Path
from sdlc_factory.heartbeat import run_heartbeat_cycle
from sdlc_factory.utils import write_json

def test_run_heartbeat_cycle_blocked(mocker):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[{"task_id": "blk1", "issue_file": "ish.md"}])
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent")
    
    assert run_heartbeat_cycle() == True
    # Reasoner is currently commented out, so it shouldn't execute
    mock_execute.assert_not_called()

def test_run_heartbeat_cycle_pending(mocker, tmp_path):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    mocker.patch("sdlc_factory.heartbeat.get_workspace_root", return_value=tmp_path)
    
    # Create fake workspace to trigger active workflow
    ws = tmp_path / "t1"
    ws.mkdir(parents=True)
    write_json(ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "PLANNING"})

    mock_wf = mocker.MagicMock()
    mock_wf.name = "sdlc"
    mock_wf.agents_list = ["planner"]
    mock_wf.get_pending_task.return_value = {"task_id": "t1", "assigned_module": "sys", "workspace": "/tmp", "phase": "PLANNING"}
    mock_wf.schemas = {}
    mock_wf.get_phase_context.return_value = "fake context"
    mocker.patch("sdlc_factory.heartbeat.get_workflow", return_value=mock_wf)
    
    mocker.patch("sdlc_factory.heartbeat.build_context", return_value={"status": "success"})
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent", return_value="done")
    
    assert run_heartbeat_cycle() == True
    mock_execute.assert_called_once()
    assert mock_execute.call_args[0][0] == "planner"

def test_run_heartbeat_cycle_context_fails(mocker, tmp_path):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    mocker.patch("sdlc_factory.heartbeat.get_workspace_root", return_value=tmp_path)
    
    ws = tmp_path / "t1"
    ws.mkdir(parents=True)
    write_json(ws / ".state" / "current.json", {"workflow": "sdlc", "phase": "TEST_DESIGN"})

    mock_wf = mocker.MagicMock()
    mock_wf.name = "sdlc"
    mock_wf.agents_list = ["tester"]
    mock_wf.get_pending_task.return_value = {"task_id": "t1", "assigned_module": "sys", "workspace": "/tmp", "phase": "TEST_DESIGN"}
    mock_wf.schemas = {}
    mock_wf.get_phase_context.return_value = ""
    mocker.patch("sdlc_factory.heartbeat.get_workflow", return_value=mock_wf)
    
    mocker.patch("sdlc_factory.heartbeat.build_context", side_effect=Exception("DB down"))
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent", return_value="done")
    
    assert run_heartbeat_cycle() == True
    mock_execute.assert_called_once()
    assert "Error loading context: DB down" in mock_execute.call_args[0][1]

def test_run_heartbeat_cycle_idle(mocker, tmp_path):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    mocker.patch("sdlc_factory.heartbeat.get_workspace_root", return_value=tmp_path)
    
    mock_wf = mocker.MagicMock()
    mock_wf.name = "sdlc"
    mock_wf.agents_list = ["planner"]
    mock_wf.get_pending_task.return_value = None
    mocker.patch("sdlc_factory.heartbeat.get_workflow", return_value=mock_wf)
    
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent")
    
    assert run_heartbeat_cycle() == False
    mock_execute.assert_not_called()
