import pytest
from sdlc_factory.workflows.sdlc.tools import run_cli_command

def test_run_cli_command(mocker):
    out = run_cli_command("echo hello")
    assert out.strip() == "hello"

def test_run_cli_command_timeout(mocker):
    out = run_cli_command("sleep 2", timeout=1)
    assert "timed out" in out

def test_run_cli_command_exception(mocker):
    mocker.patch("sdlc_factory.workflows.sdlc.tools.subprocess.Popen", side_effect=Exception("kaboom"))
    out = run_cli_command("fail")
    assert "Error executing command: kaboom" in out
