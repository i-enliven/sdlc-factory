import pytest
from pathlib import Path
from sdlc_factory.agent import execute_agent

def test_execute_agent_basic(mocker, tmp_path):
    agent_dir = tmp_path / "testagent"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("soul data")
    
    mocker.patch("sdlc_factory.agent.get_config", return_value={"sessions_root": str(tmp_path), "models": {"testagent": {"model": "gemini-3.1-pro-preview-customtools"}}})
    mock_workflow = mocker.MagicMock()
    mock_workflow.agents_dir = tmp_path
    mocker.patch("sdlc_factory.workflows.get_workflow", return_value=mock_workflow)
    mocker.patch("sdlc_factory.telemetry.setup_telemetry")
    
    mock_client_class = mocker.patch("sdlc_factory.agent.OpenAI", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_response = mocker.MagicMock()
    mock_response.choices = [mocker.MagicMock()]
    mock_response.choices[0].message.tool_calls = []
    mock_response.choices[0].message.content = "hello from agent"
    mock_client.chat.completions.create.return_value = mock_response
    
    mocker.patch("sdlc_factory.agent.using_session", return_value=mocker.MagicMock())
    
    res = execute_agent("testagent", "do the thing")
    assert res == "hello from agent"

def test_execute_agent_function_call(mocker, tmp_path):
    agent_dir = tmp_path / "testagent"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("soul data")
    
    mocker.patch("sdlc_factory.agent.get_config", return_value={"sessions_root": str(tmp_path)})
    mock_workflow = mocker.MagicMock()
    mock_workflow.agents_dir = tmp_path
    mocker.patch("sdlc_factory.workflows.get_workflow", return_value=mock_workflow)
    mocker.patch("sdlc_factory.telemetry.setup_telemetry")
    mock_workflow.process_tool_call.return_value = ("faked", tmp_path)
    
    mock_client_class = mocker.patch("sdlc_factory.agent.OpenAI", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_chat = mock_client.chat.completions


    mocker.patch("sdlc_factory.agent.using_session", return_value=mocker.MagicMock())

    mock_response_1 = mocker.MagicMock()
    mock_call = mocker.MagicMock()
    mock_call.function.name = "run_cli_command"
    mock_call.function.arguments = '{"command": "ls"}'
    mock_response_1.choices = [mocker.MagicMock()]
    mock_response_1.choices[0].message.tool_calls = [mock_call]
    mock_response_1.choices[0].message.content = "I will run ls"
    
    mock_response_2 = mocker.MagicMock()
    mock_response_2.choices = [mocker.MagicMock()]
    mock_response_2.choices[0].message.tool_calls = []
    mock_response_2.choices[0].message.content = "ls finished"
    
    mock_client.chat.completions.create.side_effect = [mock_response_1, mock_response_2]
    
    res = execute_agent("testagent", "do ls")
    assert res == "ls finished"
    assert mock_client.chat.completions.create.call_count == 2
