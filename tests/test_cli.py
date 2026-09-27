import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typer.testing import CliRunner
import sys

import pytest

from sdlc_factory.cli import app
from sdlc_factory.providers import store
from sdlc_factory.providers.copilot import PROVIDER_ID

runner = CliRunner()

def test_config(mocker, tmp_path):
    test_workspace = tmp_path / "test_workspace"
    test_workspace.mkdir()

    result = runner.invoke(app, ["config", "--workspace-root", str(test_workspace)])
    assert result.exit_code == 0

    test_config_file = tmp_path / ".sdlc-factory.json"
    assert test_config_file.exists()

def test_version(mocker):
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    
    mocker.patch("importlib.metadata.version", side_effect=Exception("No pack"))
    import importlib.metadata
    mocker.patch("importlib.metadata.PackageNotFoundError", Exception)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0

def test_init_existing(tmp_path):
    ws_path = tmp_path / "test_workspace" / "test-task"
    ws_path.mkdir(parents=True)
    result = runner.invoke(app, ["init", "--task-id", "test-task"])
    assert result.exit_code == 1

def test_init_missing_file(tmp_path):
    result = runner.invoke(app, ["init", "--task-id", "test-task-missing", "-f", "/fake/file.txt"])
    assert result.exit_code == 1

def test_init(tmp_path):
    result = runner.invoke(app, ["init", "--task-id", "test-task"])
    assert result.exit_code == 0

    ws_path = tmp_path / "test_workspace" / "test-task"
    assert ws_path.exists()
    assert (ws_path / ".state" / "current.json").exists()

def test_init_with_file(tmp_path):
    req_file = tmp_path / "reqs.txt"
    req_file.write_text("my requirements")

    result = runner.invoke(app, ["init", "--task-id", "test-task-2", "-f", str(req_file)])
    assert result.exit_code == 0

def test_query_state_no_agent():
    result = runner.invoke(app, ["query-state"])
    assert result.exit_code == 0
    assert "planner" in result.stdout

def test_query_state(mocker):
    mocker.patch("sdlc_factory.cli.get_pending_task", return_value={"task_id": "test-task"})
    result = runner.invoke(app, ["query-state", "--agent", "coder"])
    assert result.exit_code == 0
    assert "test-task" in result.stdout

def test_query_state_blocked(mocker):
    mocker.patch("sdlc_factory.cli.get_blocked_tasks", return_value=["t1"])
    result = runner.invoke(app, ["query-state", "--check-blocked"])
    assert result.exit_code == 0
    assert "blocked" in result.stdout

def test_context(mocker):
    mocker.patch("sdlc_factory.cli.build_context", return_value={"ctx": "test"})
    result = runner.invoke(app, ["context", "--task-id", "t1", "--module", "sys"])
    assert result.exit_code == 0
    assert "test" in result.stdout

def test_advance_state(mocker):
    mocker.patch("sdlc_factory.cli.do_advance_state", return_value=True)
    result = runner.invoke(app, ["advance-state", "--task-id", "t1", "--to", "TEST"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.do_advance_state", side_effect=Exception("adv err"))
    result = runner.invoke(app, ["advance-state", "--task-id", "t1", "--to", "TEST"])
    assert result.exit_code == 1

def test_search_codebase(mocker):
    mocker.patch("sdlc_factory.cli.do_search_codebase", return_value=[{"filepath": "f", "content": "c"}])
    result = runner.invoke(app, ["search-codebase", "--query", "hello"])
    assert result.exit_code == 0
    assert "Results" in result.stdout
    
    mocker.patch("sdlc_factory.cli.do_search_codebase", return_value=[])
    result = runner.invoke(app, ["search-codebase", "--query", "hello"])
    assert result.exit_code == 0

def test_index_codebase(mocker):
    mocker.patch("sdlc_factory.cli.do_index_codebase", return_value=True)
    result = runner.invoke(app, ["index-codebase", "--repo-dir", "/tmp"])
    assert result.exit_code == 0

def test_store_memory(mocker):
    mocker.patch("sdlc_factory.cli.do_store_memory", return_value="Stored")
    result = runner.invoke(app, ["store-memory", "--agent", "coder", "--task-context", "ctx", "--resolution", "res"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.do_store_memory", side_effect=Exception("mem err"))
    result = runner.invoke(app, ["store-memory", "--agent", "coder", "--task-context", "ctx", "--resolution", "res"])
    assert result.exit_code == 1

def test_heartbeat(mocker):
    mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", return_value=True)
    result = runner.invoke(app, ["heartbeat"])
    assert result.exit_code == 0
    
    mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", return_value=False)
    result = runner.invoke(app, ["heartbeat"])
    assert result.exit_code == 0

def test_task_interactive(mocker):
    mocker.patch("sdlc_factory.agent.execute_agent", return_value="done")
    result = runner.invoke(app, ["task", "--agent", "coder", "--prompt", "hi"])
    assert result.exit_code == 0
    assert "done" in result.stdout
    
    result = runner.invoke(app, ["task", "--agent", "coder"], input="   \n")
    assert result.exit_code == 1
    
    result = runner.invoke(app, ["task", "--agent", "coder"], input="done\n")
    assert result.exit_code == 0

def test_run_cmd(mocker):
    mock_run = mocker.patch("sdlc_factory.cli.run_heartbeat_cycle", side_effect=KeyboardInterrupt)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0


# --- Copilot CLI (T5) ---
#
# The commands are thin wrappers, so these tests drive the real login flow through
# a fake HTTP seam and a real auth.json under tmp_path: no socket is opened, and
# the assertions look at what the user actually sees plus what lands on disk.

COPILOT_PROXY_TOKEN = "tid=abc;exp=1800000000;proxy-ep=proxy.individual.githubcopilot.com"
COPILOT_BASE_URL = "https://api.individual.githubcopilot.com"
COPILOT_MODEL_IDS = ["gemini-2.5-pro", "gpt-5", "gpt-5-codex", "grok-4"]
COPILOT_MODEL_APIS = {
    "gemini-2.5-pro": {"api": "completions"},
    "gpt-5": {"api": "responses"},
    "gpt-5-codex": {"api": "responses"},
    "grok-4": {"api": "responses"},
}
NO_EXPIRY = object()  # sentinel: write the credential without an "expires" key


class FakeCopilotHttp:
    """Scripted replacement for the copilot HTTP seam (see tests/test_copilot_auth.py)."""

    def __init__(self, host="github.com", token=COPILOT_PROXY_TOKEN, deny=False):
        self.host = host
        self.token = token
        self.polls = 0
        self.deny = deny
        self.policy_calls = []

    def post_form(self, url, data, headers=None):
        if url == f"https://{self.host}/login/device/code":
            return {"device_code": "dev-code", "user_code": "ABCD-1234",
                    "verification_uri": f"https://{self.host}/login/device",
                    "interval": 1, "expires_in": 900}
        if url == f"https://{self.host}/login/oauth/access_token":
            self.polls += 1
            if self.deny:
                return {"error": "access_denied", "error_description": "The user denied the request"}
            if self.polls == 1:
                return {"error": "authorization_pending"}
            return {"access_token": "gh-token"}
        raise AssertionError(f"unexpected POST_FORM {url}")

    def get(self, url, headers=None):
        if url == f"https://api.{self.host}/copilot_internal/v2/token":
            return {"token": self.token, "expires_at": 1800000000}
        if url == f"{COPILOT_BASE_URL}/models":
            return {"data": [{"id": model_id, "model_picker_enabled": True,
                              "policy": {"state": "unconfigured" if model_id == "grok-4" else "enabled"},
                              "capabilities": {"supports": {"tool_calls": True}}}
                             for model_id in COPILOT_MODEL_IDS]}
        raise AssertionError(f"unexpected GET {url}")

    def post_json(self, url, payload, headers=None):
        assert payload == {"state": "enabled"}, url
        self.policy_calls.append(url)
        return {"state": "enabled"}


@pytest.fixture
def copilot_home(tmp_path, monkeypatch, mocker):
    """auth.json under tmp_path, and a poll loop that does not really sleep."""
    path = tmp_path / ".sdlc-factory" / "auth.json"
    monkeypatch.setattr(store, "AUTH_FILE", path)
    mocker.patch("sdlc_factory.providers.copilot.time.sleep")
    return path


def seed_copilot_credential(path, expires=NO_EXPIRY, models=COPILOT_MODEL_APIS):
    credential = {
        "refresh": "gh-token",
        "access": COPILOT_PROXY_TOKEN,
        "enterprise_url": None,
        "available_model_ids": sorted(models),
        "models": models,
    }
    if expires is not NO_EXPIRY:
        credential["expires"] = expires
    store.write_auth({"vllm": {"api_key": "keep-me"}, PROVIDER_ID: credential})
    return path


def test_copilot_login_prints_the_device_code_and_model_ids(copilot_home, mocker):
    fake = mocker.patch("sdlc_factory.providers.copilot.DEFAULT_HTTP", FakeCopilotHttp())

    result = runner.invoke(app, ["copilot-login"], input="\n")

    assert result.exit_code == 0, result.stdout
    assert "ABCD-1234" in result.stdout
    assert "https://github.com/login/device" in result.stdout
    assert "4 models available" in result.stdout
    listed = [line.strip() for line in result.stdout.splitlines() if line.strip() in COPILOT_MODEL_IDS]
    assert listed == sorted(COPILOT_MODEL_IDS)
    # the unconfigured model got a policy call, the already-enabled ones did not
    assert fake.policy_calls == [f"{COPILOT_BASE_URL}/models/grok-4/policy"]

    entry = json.loads(copilot_home.read_text(encoding="utf-8"))[PROVIDER_ID]
    assert entry["refresh"] == "gh-token"
    assert entry["available_model_ids"] == sorted(COPILOT_MODEL_IDS)


def test_copilot_login_enterprise_flag_skips_the_prompt(copilot_home, mocker):
    mocker.patch("sdlc_factory.providers.copilot.DEFAULT_HTTP", FakeCopilotHttp(host="company.ghe.com"))

    result = runner.invoke(app, ["copilot-login", "--enterprise", "company.ghe.com"], input="")

    assert result.exit_code == 0, result.stdout
    assert json.loads(copilot_home.read_text(encoding="utf-8"))[PROVIDER_ID]["enterprise_url"] == "company.ghe.com"


def test_copilot_login_aborts_on_non_interactive_stdin(copilot_home, mocker):
    mocker.patch("sdlc_factory.providers.copilot.DEFAULT_HTTP", FakeCopilotHttp())

    result = runner.invoke(app, ["copilot-login"], input="")

    assert result.exit_code == 1
    assert "--enterprise" in result.output
    assert "Traceback" not in result.output
    assert not copilot_home.exists()


def test_copilot_login_aborts_when_the_user_denies_the_device_flow(copilot_home, mocker):
    fake = FakeCopilotHttp(deny=True)
    mocker.patch("sdlc_factory.providers.copilot.DEFAULT_HTTP", fake)

    result = runner.invoke(app, ["copilot-login"], input="\n")

    assert result.exit_code == 1
    assert "access_denied" in result.output
    assert "Traceback" not in result.output
    assert not copilot_home.exists()


def test_copilot_status_without_a_credential(copilot_home):
    result = runner.invoke(app, ["copilot-status"])

    assert result.exit_code == 0
    assert "copilot-login" in result.stdout


def test_copilot_status_reports_a_live_token(copilot_home):
    expires_ms = int((time.time() + 3600) * 1000)
    seed_copilot_credential(copilot_home, expires_ms)

    result = runner.invoke(app, ["copilot-status"])

    assert result.exit_code == 0
    assert datetime.fromtimestamp(expires_ms / 1000).strftime("%Y-%m-%d %H:%M") in result.stdout
    assert "(valid)" in result.stdout
    assert COPILOT_BASE_URL in result.stdout
    assert "4 available" in result.stdout
    assert "completions=1" in result.stdout
    assert "responses=3" in result.stdout
    assert "EXPIRED" not in result.stdout


def test_copilot_status_reports_an_expired_token(copilot_home):
    expires_ms = int((time.time() - 3600) * 1000)
    seed_copilot_credential(copilot_home, expires_ms)

    result = runner.invoke(app, ["copilot-status"])

    assert result.exit_code == 0
    assert "EXPIRED" in result.stdout
    assert datetime.fromtimestamp(expires_ms / 1000).strftime("%Y-%m-%d %H:%M") in result.stdout


@pytest.mark.parametrize("expires,expired", [(NO_EXPIRY, True), (None, True), ("tomorrow", True),
                                             (10 ** 20, False)])  # 10**20: out of range for localtime()
def test_copilot_status_survives_an_unusable_expiry(copilot_home, expires, expired):
    """A hand-edited or half-written credential must not crash the status command."""
    seed_copilot_credential(copilot_home, expires)

    result = runner.invoke(app, ["copilot-status"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.stdout
    assert ("EXPIRED" in result.stdout) is expired


def test_copilot_status_infers_the_api_without_a_catalog_block(copilot_home):
    """A credential predating the per-model catalog still gets a usable summary."""
    store.write_auth({PROVIDER_ID: {
        "access": COPILOT_PROXY_TOKEN,
        "expires": int((time.time() + 3600) * 1000),
        "available_model_ids": COPILOT_MODEL_IDS,
    }})

    result = runner.invoke(app, ["copilot-status"])

    assert result.exit_code == 0, result.stdout
    assert "4 available" in result.stdout
    assert "completions=1" in result.stdout
    assert "responses=3" in result.stdout


def test_copilot_logout_keeps_the_other_providers(copilot_home):
    seed_copilot_credential(copilot_home, int((time.time() + 3600) * 1000))

    result = runner.invoke(app, ["copilot-logout"])

    assert result.exit_code == 0
    assert "github-copilot" in result.stdout
    assert json.loads(copilot_home.read_text(encoding="utf-8")) == {"vllm": {"api_key": "keep-me"}}


def test_copilot_logout_without_any_stored_credential(copilot_home):
    result = runner.invoke(app, ["copilot-logout"])

    assert result.exit_code == 0
    assert "nothing to remove" in result.stdout
    assert store.read_auth() == {}
