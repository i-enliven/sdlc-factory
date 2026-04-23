import pytest
from sdlc_factory.db import get_db_connection, get_embedding

def test_get_db_connection(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={"connection_string": "postgresql://user:pass@localhost:5432/db"})
    mock_connect = mocker.patch("sdlc_factory.db.psycopg.connect")
    mock_register = mocker.patch("sdlc_factory.db.register_vector")

    conn = get_db_connection()
    mock_connect.assert_called_once_with("postgresql://user:pass@localhost:5432/db")
    mock_register.assert_called_once_with(mock_connect.return_value)
    assert conn == mock_connect.return_value

def test_get_db_connection_missing_string(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={})
    with pytest.raises(Exception, match="connection_string is empty"):
        get_db_connection()

def test_get_embedding(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={"gemini_api_key": "test_key"})
    mock_client_class = mocker.patch("google.genai.Client")
    mock_client = mock_client_class.return_value
    mock_response = mocker.MagicMock()
    mock_response.embeddings = [mocker.MagicMock(values=[0.1, 0.2, 0.3])]
    mock_client.models.embed_content.return_value = mock_response

    vector = get_embedding("hello world")
    assert vector == [0.1, 0.2, 0.3]
    mock_client.models.embed_content.assert_called_once()

def test_get_embedding_missing_key(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={})
    mocker.patch("os.environ.get", return_value=None)
    with pytest.raises(Exception, match="API key is missing"):
        get_embedding("test")

def test_get_embedding_api_failure(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={"gemini_api_key": "test_key"})
    mock_client_class = mocker.patch("google.genai.Client")
    mock_client = mock_client_class.return_value
    mock_client.models.embed_content.side_effect = Exception("API down")
    
    with pytest.raises(Exception, match="Embedding failed: API down"):
        get_embedding("hello")
