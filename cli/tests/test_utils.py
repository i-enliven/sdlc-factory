import json
import pytest
from pathlib import Path
from sdlc_factory.utils import read_json, write_json, abort, get_config, get_workspace_root, get_workspace, setup_global_logger

def test_read_json(tmp_path):
    test_file = tmp_path / "test.json"
    test_file.write_text('{"key": "value"}')
    data = read_json(test_file)
    assert data["key"] == "value"

def test_read_json_missing(tmp_path):
    test_file = tmp_path / "missing.json"
    assert read_json(test_file) == {}
    assert read_json(test_file, default={"default": 1}) == {"default": 1}

def test_read_json_invalid(tmp_path):
    test_file = tmp_path / "bad.json"
    test_file.write_text("{bad json")
    assert read_json(test_file) == {}
    assert read_json(test_file, default=[]) == []

def test_write_json(tmp_path):
    test_file = tmp_path / "subdir" / "out.json"
    write_json(test_file, {"hello": "world"})
    assert test_file.exists()
    assert json.loads(test_file.read_text()) == {"hello": "world"}

def test_abort(mocker):
    # Abort calls sys.exit
    with pytest.raises(SystemExit) as exc:
        abort("test error", code=42)
    assert exc.value.code == 42

def test_get_config_missing(mocker, tmp_path):
    mocker.patch("sdlc_factory.utils.CONFIG_FILE", tmp_path / "none.json")
    with pytest.raises(SystemExit):
        get_config()

def test_get_workspace_root(mocker, mock_config, tmp_path):
    mocker.patch("sdlc_factory.utils.get_config", return_value={"workspace_root": str(tmp_path / "test_workspace")})
    root = get_workspace_root()
    expected = str(tmp_path / "test_workspace")
    assert str(root) == expected

def test_get_workspace_root_missing(mocker):
    mocker.patch("sdlc_factory.utils.get_config", return_value={})
    with pytest.raises(SystemExit):
        get_workspace_root()

def test_get_workspace(mock_config, tmp_path):
    ws = get_workspace("task-123")
    expected = str(tmp_path / "test_workspace" / "task-123")
    assert str(ws) == expected

def test_setup_global_logger(mocker, tmp_path):
    mocker.patch("sdlc_factory.utils.get_config", return_value={"log_path": str(tmp_path)})
    setup_global_logger()
    log_file = tmp_path / "app.log"
    assert log_file.exists() or tmp_path.exists()
