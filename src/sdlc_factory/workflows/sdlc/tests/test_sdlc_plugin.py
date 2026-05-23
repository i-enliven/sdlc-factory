import pytest
from pathlib import Path
from sdlc_factory.utils import write_json
from sdlc_factory.workflows.sdlc.plugin import SdlcWorkflow

def test_scatter_architecture(mocker, tmp_path, mock_config):
    ws = tmp_path / "test_workspace" / "t1"
    ws.mkdir(parents=True)
    (ws / "handoff").mkdir()
    (ws / "docs").mkdir()
    (ws / ".state").mkdir()
    write_json(ws / ".state" / "current.json", {"phase": "ARCHITECTURE"})
    
    (ws / "docs" / "API_CONTRACTS.md").write_text("contracts")
    write_json(ws / "handoff" / "arch_payload.json", {"vertical_slices": [{"module_name": "api"}]})
    
    wf = SdlcWorkflow()
    wf._scatter_architecture(ws, "t1")
    
    child_ws = tmp_path / "test_workspace" / "t1-MOD-api"
    assert child_ws.exists()
    assert (child_ws / "docs" / "API_CONTRACTS.md").exists()

def test_gather_modules(tmp_path, mock_config):
    parent = tmp_path / "test_workspace" / "t1"
    parent.mkdir(parents=True)
    (parent / "handoff").mkdir()
    (parent / "docs").mkdir()
    (parent / ".state").mkdir()
    (parent / "docs" / "API_CONTRACTS.md").write_text("api")
    (parent / "docs" / "PROD_SPEC.md").write_text("spec")
    write_json(parent / "handoff" / "arch_payload.json", {"vertical_slices": [{"module_name": "api"}]})
    
    child = tmp_path / "test_workspace" / "t1-MOD-api"
    child.mkdir(parents=True)
    (child / ".state").mkdir()
    write_json(child / ".state" / "current.json", {"phase": "MODULE_RESOLVED"})
    
    wf = SdlcWorkflow()
    wf._gather_modules("t1-MOD-api")
    
    int_ws = tmp_path / "test_workspace" / "t1-INTEGRATION"
    assert int_ws.exists()
    assert (int_ws / "docs" / "API_CONTRACTS.md").exists()

def test_integration_transition_module_resolved(tmp_path, mock_config):
    int_ws = tmp_path / "test_workspace" / "t1-INTEGRATION"
    int_ws.mkdir(parents=True)
    (int_ws / ".state").mkdir()
    state = {"workflow": "sdlc", "phase": "QA_REVIEW"}
    write_json(int_ws / ".state" / "current.json", state)
    
    wf = SdlcWorkflow()
    wf.on_transition(int_ws, "t1-INTEGRATION", "QA_REVIEW", "MODULE_RESOLVED", state)
    
    assert state["phase"] == "INTEGRATION_TESTING"
