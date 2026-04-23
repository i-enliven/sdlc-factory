import pytest
import json
from unittest.mock import patch

with patch('sdlc_factory.utils.setup_global_logger'):
    from sdlc_factory.mcp_server import mcp, query_state, context, advance_state, search_codebase, store_memory

def test_mcp_query_state(mocker):
    mocker.patch("sdlc_factory.mcp_server.get_pending_task", return_value={"task_id": "t1"})
    res = json.loads(query_state("coder"))
    assert res["status"] == "success"
    assert res["task_id"] == "t1"

    mocker.patch("sdlc_factory.mcp_server.get_blocked_tasks", return_value=["b1"])
    res = json.loads(query_state(check_blocked=True))
    assert res["status"] == "blocked"

def test_mcp_query_state_empty(mocker):
    mocker.patch("sdlc_factory.mcp_server.get_pending_task", return_value=None)
    mocker.patch("sdlc_factory.mcp_server.get_blocked_tasks", return_value=None)
    res = json.loads(query_state("coder"))
    assert res["status"] == "no_tasks"

def test_mcp_context(mocker):
    mocker.patch("sdlc_factory.mcp_server.build_context", return_value={"data": "test"})
    res = json.loads(context("t1", "sys"))
    assert res["data"] == "test"

    mocker.patch("sdlc_factory.mcp_server.build_context", side_effect=Exception("kaboom"))
    res = json.loads(context("t1", "sys"))
    assert res["status"] == "error"

def test_mcp_advance_state(mocker):
    mocker.patch("sdlc_factory.mcp_server.do_advance_state", return_value="success")
    res = json.loads(advance_state("t1", "TEST"))
    assert res["status"] == "success"

    mocker.patch("sdlc_factory.mcp_server.do_advance_state", side_effect=Exception("advance error"))
    res = json.loads(advance_state("t1", "TEST"))
    assert res["status"] == "error"

def test_mcp_search_codebase(mocker):
    mocker.patch("sdlc_factory.mcp_server.do_search_codebase", return_value=["r1"])
    res = json.loads(search_codebase("query"))
    assert res["status"] == "success"

    mocker.patch("sdlc_factory.mcp_server.do_search_codebase", return_value=[])
    res = json.loads(search_codebase("query"))
    assert res["status"] == "no_results"

    mocker.patch("sdlc_factory.mcp_server.do_search_codebase", side_effect=Exception("search error"))
    res = json.loads(search_codebase("query"))
    assert res["status"] == "error"

def test_mcp_store_memory(mocker):
    mocker.patch("sdlc_factory.mcp_server.do_store_memory", return_value="ok")
    res = json.loads(store_memory("coder", "ctx", "res"))
    assert res["status"] == "success"

    mocker.patch("sdlc_factory.mcp_server.do_store_memory", side_effect=Exception("mem err"))
    res = json.loads(store_memory("coder", "ctx", "res"))
    assert res["status"] == "error"
