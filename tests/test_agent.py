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
    
    mock_client_class = mocker.patch("google.genai.Client", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_client.chats.create.return_value = mock_chat
    
    mock_response = mocker.MagicMock()
    mock_response.function_calls = []
    
    mock_candidate = mocker.MagicMock()
    mock_part = mocker.MagicMock()
    mock_part.text = "hello from agent"
    mock_candidate.content.parts = [mock_part]
    mock_response.candidates = [mock_candidate]
    mock_chat.send_message.return_value = mock_response
    
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
    mocker.patch("sdlc_factory.agent.run_cli_command", return_value="ls output")
    
    mock_client_class = mocker.patch("google.genai.Client", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_client.chats.create.return_value = mock_chat

    mocker.patch("sdlc_factory.agent.types.Part.from_function_response", return_value="faked", create=True)
    mocker.patch("sdlc_factory.agent.using_session", return_value=mocker.MagicMock())

    mock_response_1 = mocker.MagicMock()
    mock_call = mocker.MagicMock()
    mock_call.name = "run_cli_command"
    mock_call.args = {"command": "ls"}
    mock_response_1.function_calls = [mock_call]
    mock_candidate_1 = mocker.MagicMock()
    mock_part_1 = mocker.MagicMock()
    mock_part_1.text = "I will run ls"
    mock_candidate_1.content.parts = [mock_part_1]
    mock_response_1.candidates = [mock_candidate_1]
    
    mock_response_2 = mocker.MagicMock()
    mock_response_2.function_calls = []
    mock_candidate_2 = mocker.MagicMock()
    mock_part_2 = mocker.MagicMock()
    mock_part_2.text = "ls finished"
    mock_candidate_2.content.parts = [mock_part_2]
    mock_response_2.candidates = [mock_candidate_2]
    
    mock_chat.send_message.side_effect = [mock_response_1, mock_response_2]
    
    res = execute_agent("testagent", "do ls")
    assert res == "ls finished"
    assert mock_chat.send_message.call_count == 2
