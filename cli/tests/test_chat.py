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
    
    mocker.patch("sdlc_factory.chat.types.Content.model_validate", side_effect=lambda x: x, create=True)
    session_file.write_text(json.dumps([{"role": "user", "parts": [{"text": "hi"}]}]), encoding="utf-8")
    
    mocker.patch("sdlc_factory.chat.get_config", return_value={"sessions_root": str(tmp_path), "models": {"testagent": {"model": "gemini-2.5-flash"}}})
    
    mock_client_class = mocker.patch("google.genai.Client", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_client.chats.create.return_value = mock_chat
    
    mock_response = mocker.MagicMock()
    mock_response.function_calls = []
    mock_candidate = mocker.MagicMock()
    mock_part = mocker.MagicMock()
    mock_part.text = "agent reply"
    mock_candidate.content.parts = [mock_part]
    mock_response.candidates = [mock_candidate]
    mock_chat.send_message.return_value = mock_response
    
    mocker.patch('builtins.input', side_effect=["hello", EOFError])
    
    run_chat_session("testagent-123")
    
    mock_chat.send_message.assert_called_once_with("hello")

def test_run_chat_session_with_tool(mocker, tmp_path):
    session_dir = tmp_path
    session_file = session_dir / "testagent-123.session"
    
    mocker.patch("sdlc_factory.chat.types.Content.model_validate", side_effect=lambda x: x, create=True)
    session_file.write_text("[]", encoding="utf-8")
    
    mocker.patch("sdlc_factory.chat.get_config", return_value={"sessions_root": str(tmp_path)})
    
    mock_client_class = mocker.patch("google.genai.Client", create=True)
    mock_client = mock_client_class.return_value
    mock_chat = mocker.MagicMock()
    mock_client.chats.create.return_value = mock_chat
    
    mocker.patch('builtins.input', side_effect=["save this", EOFError])
    
    mocker.patch("sdlc_factory.chat.sdlc_store_memory", return_value="saved!")
    mocker.patch("sdlc_factory.chat.types.Part.from_function_response", return_value="faked", create=True)
    
    mock_response_1 = mocker.MagicMock()
    mock_call = mocker.MagicMock()
    mock_call.name = "sdlc_store_memory"
    mock_call.args = {"k": "v"}
    mock_response_1.function_calls = [mock_call]
    mock_response_1.candidates = []
    
    mock_response_2 = mocker.MagicMock()
    mock_response_2.function_calls = []
    mock_candidate_2 = mocker.MagicMock()
    mock_part_2 = mocker.MagicMock()
    mock_part_2.text = "saved successfully"
    mock_candidate_2.content.parts = [mock_part_2]
    mock_response_2.candidates = [mock_candidate_2]
    
    mock_chat.send_message.side_effect = [mock_response_1, mock_response_2]
    
    run_chat_session("testagent-123")
    
    assert mock_chat.send_message.call_count == 2
