import pytest
import json
import subprocess
from sdlc_factory.tools import (
    sdlc_query_state, sdlc_context,
    sdlc_advance_state, sdlc_search_codebase, sdlc_store_memory
)
def test_sdlc_query_state_blocked(mocker):
    mocker.patch("sdlc_factory.tools.get_blocked_tasks", return_value=["task1"])
    res = json.loads(sdlc_query_state(check_blocked=True))
    assert res["status"] == "blocked"
    assert "task1" in res["tasks"]

def test_sdlc_query_state_pending(mocker):
    mocker.patch("sdlc_factory.tools.get_pending_task", return_value={"task_id": "t1"})
    res = json.loads(sdlc_query_state(agent="coder"))
    assert res["status"] == "success"
    assert res["task_id"] == "t1"

def test_sdlc_query_state_missing(mocker):
    res = json.loads(sdlc_query_state(agent=None))
    assert res["status"] == "error"

def test_sdlc_context(mocker):
    mocker.patch("sdlc_factory.tools.build_context", return_value={"data": "test"})
    res = json.loads(sdlc_context("t1", "sys"))
    assert res["data"] == "test"

def test_sdlc_context_exception(mocker):
    mocker.patch("sdlc_factory.tools.build_context", side_effect=Exception("kaboom"))
    res = json.loads(sdlc_context("t1", "sys"))
    assert res["status"] == "error"

def test_sdlc_advance_state(mocker):
    mocker.patch("sdlc_factory.tools.do_advance_state", return_value="advanced")
    res = json.loads(sdlc_advance_state("t1", "TEST"))
    assert res["status"] == "success"

def test_sdlc_advance_state_exception(mocker):
    mocker.patch("sdlc_factory.tools.do_advance_state", side_effect=Exception("kaboom"))
    res = json.loads(sdlc_advance_state("t1", "TEST"))
    assert res["status"] == "error"

def test_sdlc_search_codebase(mocker):
    mocker.patch("sdlc_factory.tools.do_search_codebase", return_value=["r1"])
    res = json.loads(sdlc_search_codebase("find"))
    assert res["status"] == "success"
    assert res["results"] == ["r1"]

def test_sdlc_search_codebase_exception(mocker):
    mocker.patch("sdlc_factory.tools.do_search_codebase", side_effect=Exception("kaboom"))
    res = json.loads(sdlc_search_codebase("find"))
    assert res["status"] == "error"

def test_sdlc_store_memory(mocker):
    mocker.patch("sdlc_factory.tools.do_store_memory", return_value="stored")
    res = json.loads(sdlc_store_memory("coder", "ctx", "res"))
    assert res["status"] == "success"

def test_sdlc_store_memory_exception(mocker):
    mocker.patch("sdlc_factory.tools.do_store_memory", side_effect=Exception("kaboom"))
    res = json.loads(sdlc_store_memory("coder", "ctx", "res"))
    assert res["status"] == "error"
