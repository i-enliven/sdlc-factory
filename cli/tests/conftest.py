import pytest
import json
from pathlib import Path

@pytest.fixture(autouse=True)
def mock_config(mocker, tmp_path):
    mock_file = tmp_path / ".sdlc-factory.json"
    config_data = {
        "workspace_root": str(tmp_path / "test_workspace"),
        "log_path": str(tmp_path / "test_logs"),
        "connection_string": "mock-postgres"
    }
    
    mock_file.write_text(json.dumps(config_data))
    
    mocker.patch("sdlc_factory.utils.CONFIG_FILE", mock_file)
    try:
        mocker.patch("sdlc_factory.cli.CONFIG_FILE", mock_file)
    except AttributeError:
        pass
    
    mocker.patch("sdlc_factory.utils.get_config", return_value=config_data)
    try:
        mocker.patch("sdlc_factory.cli.get_config", return_value=config_data)
    except AttributeError:
        pass
        
    return config_data

@pytest.fixture
def mock_db(mocker):
    mock_conn = mocker.MagicMock()
    mock_cursor = mocker.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mocker.patch("sdlc_factory.db.psycopg.connect", return_value=mock_conn)
    return mock_conn, mock_cursor
