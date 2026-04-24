import pytest
from sdlc_factory.utils import write_json
from sdlc_factory.memory import (
    check_regression, build_context, do_index_codebase, 
    do_search_codebase, do_store_memory, get_db_connection, get_embedding
)

def test_check_regression(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "handoff").mkdir()
    assert "No active regression" in check_regression(ws)
    reg_file = ws / "handoff" / "regression_report.json"
    reg_file.write_text('{"error": "bad"}')
    assert "ACTIVE REGRESSION DETECTED" in check_regression(ws)

def test_build_context(mocker, mock_config, mock_db, tmp_path):
    ws = tmp_path / "test_workspace" / "task-1"
    ws.mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "handoff").mkdir()
    (ws / "docs" / "API_CONTRACTS.md").write_text("## > BEGIN_ENVIRONMENT\nenv\n## > END_ENVIRONMENT\n\n## > BEGIN_MODULE: api\ncode\n## > END_MODULE")
    write_json(ws / "handoff" / "arch_payload.json", {})
    mocker.patch("sdlc_factory.memory.get_embedding", return_value=[0.1])
    ctx = build_context("task-1", "api", "coder")
    assert ctx["status"] == "success"

def test_build_context_no_db(mocker, mock_config, tmp_path):
    ws = tmp_path / "test_workspace" / "task-2"
    ws.mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "handoff").mkdir()
    write_json(ws / "handoff" / "arch_payload.json", {})
    ctx = build_context("task-2", "api")
    assert ctx["status"] == "success"

def test_do_index_codebase(mocker, tmp_path, mock_db):
    conn, cursor = mock_db
    f = tmp_path / "test.py"
    f.write_text("def hello():\n  print('world')\n")
    mocker.patch("sdlc_factory.memory.get_embedding", return_value=[0.1])
    mocker.patch("sdlc_factory.memory.get_db_connection", return_value=conn)
    do_index_codebase(str(tmp_path))
    cursor.execute.assert_called()

def test_do_search_codebase(mocker, mock_db):
    conn, cursor = mock_db
    mocker.patch("sdlc_factory.memory.get_embedding", return_value=[0.1])
    mocker.patch("sdlc_factory.memory.get_db_connection", return_value=conn)
    cursor.fetchall.return_value = [("test.py", "content")]
    res = do_search_codebase("hello")
    assert len(res) == 1

def test_do_store_memory(mocker, mock_db):
    conn, cursor = mock_db
    mocker.patch("sdlc_factory.memory.get_embedding", return_value=[0.1])
    mocker.patch("sdlc_factory.memory.get_db_connection", return_value=conn)
    msg = do_store_memory("coder", "task_ctx", "resolution_data")
    assert "[coder MEMORY STORED]" in msg
def test_do_index_codebase_exceptions(mocker, tmp_path, mock_db):
    conn, cursor = mock_db
    (tmp_path / "small.md").write_text("tiny\n\nfile")
    
    # Path outside workspace
    from pathlib import Path
    mock_rglob = mocker.patch("pathlib.Path.rglob", return_value=[Path("/tmp/outside.md"), tmp_path / "small.md", tmp_path / "error.md"])
    mocker.patch("sdlc_factory.memory.get_db_connection", return_value=conn)
    
    # mock Exception to trigger logger
    def mock_get_embedding(text):
        if "error" in text:
            raise Exception("API error")
        return [0.1]
    
    (tmp_path / "error.md").write_text("this has the word error inside it, long enough to pass")
    mocker.patch("sdlc_factory.memory.get_embedding", side_effect=mock_get_embedding)
    
    do_index_codebase(str(tmp_path))
