import pytest
from sdlc_factory.heartbeat import run_heartbeat_cycle

def test_run_heartbeat_cycle_blocked(mocker):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[{"task_id": "blk1", "issue_file": "ish.md"}])
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent")
    
    assert run_heartbeat_cycle() == True
    # Reasoner is currently commented out, so it shouldn't execute
    mock_execute.assert_not_called()

def test_run_heartbeat_cycle_pending(mocker):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    
    def mock_get_pending(agent):
        if agent == "planner":
            return {"task_id": "t1", "assigned_module": "sys", "workspace": "/tmp", "phase": "PLANNING"}
        return None
        
    mocker.patch("sdlc_factory.heartbeat.get_pending_task", side_effect=mock_get_pending)
    mocker.patch("sdlc_factory.heartbeat.build_context", return_value={"status": "success"})
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent", return_value="done")
    
    assert run_heartbeat_cycle() == True
    mock_execute.assert_called_once()
    assert mock_execute.call_args[0][0] == "planner"

def test_run_heartbeat_cycle_context_fails(mocker):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    def mock_get_pending(agent):
        if agent == "tester":
            return {"task_id": "t1", "assigned_module": "sys", "workspace": "/tmp", "phase": "TEST_DESIGN"}
        return None
    mocker.patch("sdlc_factory.heartbeat.get_pending_task", side_effect=mock_get_pending)
    mocker.patch("sdlc_factory.heartbeat.build_context", side_effect=Exception("DB down"))
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent", return_value="done")
    
    assert run_heartbeat_cycle() == True
    mock_execute.assert_called_once()
    assert "Error loading context: DB down" in mock_execute.call_args[0][1]

def test_run_heartbeat_cycle_idle(mocker):
    mocker.patch("sdlc_factory.heartbeat.get_blocked_tasks", return_value=[])
    mocker.patch("sdlc_factory.heartbeat.get_pending_task", return_value=None)
    mock_execute = mocker.patch("sdlc_factory.heartbeat.execute_agent")
    
    assert run_heartbeat_cycle() == False
    mock_execute.assert_not_called()
