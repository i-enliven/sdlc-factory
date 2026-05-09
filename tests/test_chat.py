import pytest
import json
from pathlib import Path
from sdlc_factory.chat import run_chat_session

def test_run_chat_session_missing_file(mocker, tmp_path):
    mocker.patch("sdlc_factory.chat.get_config", return_value={"sessions_root": str(tmp_path)})
    mock_abort = mocker.patch("sdlc_factory.chat.abort", side_effect=Exception("Aborted"))
    
    with pytest.raises(Exception, match="Aborted"):
        run_chat_session("testagent-123")
        
    mock_abort.assert_called_once()
    assert "Session file not found" in mock_abort.call_args[0][0]

def test_run_chat_session_basic(mocker, tmp_path):
    session_dir = tmp_path
    session_file = session_dir / "testagent-123.session"
    
    
    session_file.write_text(json.dumps([{"role": "user", "parts": [{"text": "hi"}]}]), encoding="utf-8")
    
    mocker.patch("sdlc_factory.chat.get_config", return_value={"sessions_root": str(tmp_path), "models": {"testagent": {"model": "gemini-3.1-pro-preview-customtools"}}})
    
    mock_client_class = mocker.patch("sdlc_factory.chat.OpenAI", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_response = mocker.MagicMock()
    mock_response.choices = [mocker.MagicMock()]
    mock_response.choices[0].message.tool_calls = []
    mock_response.choices[0].message.content = "agent reply"
    mock_client.chat.completions.create.return_value = mock_response
    
    mocker.patch('builtins.input', side_effect=["hello", EOFError])
    
    run_chat_session("testagent-123")
    
    assert mock_client.chat.completions.create.call_count == 1

def test_run_chat_session_with_tool(mocker, tmp_path):
    session_dir = tmp_path
    session_file = session_dir / "testagent-123.session"
    
    
    session_file.write_text("[]", encoding="utf-8")
    
    mocker.patch("sdlc_factory.chat.get_config", return_value={"sessions_root": str(tmp_path)})
    
    mock_client_class = mocker.patch("sdlc_factory.chat.OpenAI", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_chat = mock_client.chat.completions
    
    mocker.patch('builtins.input', side_effect=["save this", EOFError])
    
    mocker.patch("sdlc_factory.chat.sdlc_store_memory", return_value="saved!")
    
    
    mock_response_1 = mocker.MagicMock()
    mock_call = mocker.MagicMock()
    mock_call.function.name = "sdlc_store_memory"
    mock_call.function.arguments = '{"k": "v"}'
    mock_response_1.choices = [mocker.MagicMock()]
    mock_response_1.choices[0].message.tool_calls = [mock_call]
    mock_response_1.choices[0].message.content = ""
    
    mock_response_2 = mocker.MagicMock()
    mock_response_2.choices = [mocker.MagicMock()]
    mock_response_2.choices[0].message.tool_calls = []
    mock_response_2.choices[0].message.content = "saved successfully"
    
    mock_client.chat.completions.create.side_effect = [mock_response_1, mock_response_2]
    
    run_chat_session("testagent-123")
    
    assert mock_client.chat.completions.create.call_count == 2
