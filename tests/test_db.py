import pytest
from sdlc_factory.db import get_db_connection, get_embedding

def test_get_db_connection(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={"connection_string": "postgresql://user:pass@localhost:5432/db"})
    mock_connect = mocker.patch("sdlc_factory.db.psycopg.connect")
    mock_register = mocker.patch("sdlc_factory.db.register_vector")

    conn = get_db_connection()
    mock_connect.assert_called_once_with("postgresql://user:pass@localhost:5432/db", connect_timeout=5)
    mock_register.assert_called_once_with(mock_connect.return_value)
    assert conn == mock_connect.return_value

def test_get_db_connection_missing_string(mocker):
    mocker.patch("sdlc_factory.db.get_config", return_value={})
    with pytest.raises(Exception, match="connection_string is empty"):
        get_db_connection()

def test_get_embedding(mocker):
    # Reset the global to ensure we test the initialization
    import sdlc_factory.db
    sdlc_factory.db._EMBEDDING_MODEL = None
    
    mocker.patch("torch.cuda.is_available", return_value=False)
    mock_transformer_class = mocker.patch("sentence_transformers.SentenceTransformer")
    mock_transformer = mock_transformer_class.return_value
    
    import numpy as np
    mock_transformer.encode.return_value = np.array([0.1, 0.2, 0.3])

    vector = get_embedding("hello world")
    assert vector == [0.1, 0.2, 0.3]
    mock_transformer_class.assert_called_once_with('sentence-transformers/all-mpnet-base-v2', device='cpu')
    mock_transformer.encode.assert_called_once_with("hello world")

def test_get_embedding_api_failure(mocker):
    # Reset the global
    import sdlc_factory.db
    sdlc_factory.db._EMBEDDING_MODEL = None
    
    mocker.patch("torch.cuda.is_available", return_value=False)
    mock_transformer_class = mocker.patch("sentence_transformers.SentenceTransformer")
    mock_transformer = mock_transformer_class.return_value
    mock_transformer.encode.side_effect = Exception("Model down")
    
    with pytest.raises(Exception, match="Local Embedding failed: Model down"):
        get_embedding("hello")
