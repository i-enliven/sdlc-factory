"""T8 — hardening: mid-run 401 recovery, the auth.json permission warning, and
the session-start line that names what a run actually talks to.

Copilot's bearer token lives ~10 minutes (PLAN.md §5 "token lifetime"), so a run
with ``max_iterations: 150`` *will* cross an expiry between iterations. The rule
under test here is: one 401 per send is this layer's problem to fix — force the
refresh, rewrite ``auth.json``, re-point the live client, retry at once, and give
the attempt back so the exponential backoff budget still covers real API
outages. A second consecutive 401 is not ours to fix and aborts to the CLI.

Everything network-shaped goes through a scripted seam installed as the
provider module's default HTTP (``offline``), so an unscripted call fails the
test instead of opening a socket. ``auth.json`` lives under ``tmp_path``.
"""

import contextlib
import logging
import os
import stat
import time
from types import SimpleNamespace

import httpx
import pytest
import typer
from openai import AuthenticationError, OpenAI

from sdlc_factory import agent as agent_module
from sdlc_factory import chat as chat_module
from sdlc_factory.providers import (
    apply_auth,
    can_refresh_credentials,
    get_provider,
    is_auth_error,
    rebind_client,
    try_recover_auth,
)
from sdlc_factory.providers import copilot as copilot_module
from sdlc_factory.providers import store
from sdlc_factory.providers.base import ResolvedAuth
from sdlc_factory.providers.copilot import (
    INDIVIDUAL_BASE_URL,
    CopilotProvider,
)

COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
MODELS_URL = f"{INDIVIDUAL_BASE_URL}/models"

TOKEN = "tid=abc;exp=1800000000;proxy-ep=proxy.individual.githubcopilot.com"
REFRESHED_TOKEN = "tid=def;exp=1800003600;proxy-ep=proxy.individual.githubcopilot.com"
STALE_BASE_URL = "https://proxy.individual.githubcopilot.com"
TEN_MINUTES_MS = 10 * 60 * 1000


# --- logger capture: ``global_logger`` does not propagate to the root logger ---

@pytest.fixture
def logs():
    """Every record the app logger emits, with INFO enabled for the duration."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("sdlc_factory")
    previous_level = logger.level

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Collector()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def messages_of(records) -> str:
    return "\n".join(record.getMessage() for record in records)


# --- fake HTTP seam, installed as the provider module's default ---

class FakeHttp:
    """Scripted GET responses; anything unscripted fails the test loudly."""

    def __init__(self, get=None):
        self.calls: list[tuple] = []
        self._get = get or {}

    def get(self, url, headers=None):
        self.calls.append(("get", url, dict(headers or {})))
        if url not in self._get:
            raise AssertionError(f"unexpected GET {url}")
        return self._get[url]

    def post_form(self, url, data, headers=None):
        self.calls.append(("post_form", url, dict(headers or {})))
        raise AssertionError(f"unexpected POST {url}")

    post_json = post_form

    def urls(self) -> list:
        return [url for _kind, url, _headers in self.calls]


def catalog_payload(*model_ids):
    return {"data": [{"id": model_id, "model_picker_enabled": True,
                      "policy": {"state": "enabled"},
                      "capabilities": {"supports": {"tool_calls": True}}}
                     for model_id in model_ids]}


@pytest.fixture
def offline(mocker):
    """Make the real HTTP seam unreachable: refreshes run against ``FakeHttp``.

    Patched at module level rather than per call because production code calls
    ``provider.refresh()`` with no seam to pass — exactly the path under test.
    """
    fake = FakeHttp(get={
        COPILOT_TOKEN_URL: {"token": REFRESHED_TOKEN, "expires_at": int(time.time()) + 600},
        MODELS_URL: catalog_payload("gemini-2.5-pro", "gpt-4.1", "gpt-5"),
    })
    mocker.patch.object(copilot_module, "DEFAULT_HTTP", copilot_module.Http(
        get=fake.get, post_form=fake.post_form, post_json=fake.post_json))
    return fake


# --- auth.json fixture + credential seeding ---

@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / ".sdlc-factory" / "auth.json"
    monkeypatch.setattr(store, "AUTH_FILE", path)
    monkeypatch.setattr(store, "_perm_warning_sent", False)
    return path


def seed_credential(**overrides) -> dict:
    """Write a usable, unexpired ``github-copilot`` credential into auth.json."""
    credential = {
        "refresh": "gh-refresh-token",
        "access": TOKEN,
        "expires": int(time.time() * 1000) + TEN_MINUTES_MS,
        "enterprise_url": None,
        "available_model_ids": ["gemini-2.5-pro", "gpt-5"],
        "models": {"gemini-2.5-pro": {"api": "completions"},
                   "gpt-5": {"api": "responses"}},
    }
    credential.update(overrides)
    data = store.read_auth()
    data["github-copilot"] = credential
    store.write_auth(data)
    return credential


# --- client doubles ---

def auth_error(message="Error code: 401 - {'error': {'code': 'invalid_bearer_token'}}"):
    """The SDK's own 401, as a real request would raise it."""
    request = httpx.Request("POST", f"{INDIVIDUAL_BASE_URL}/chat/completions")
    return AuthenticationError(message, response=httpx.Response(401, request=request),
                               body=None)


def no_stream_response(content="ok"):
    message = SimpleNamespace(
        role="assistant", content=content,
        model_dump=lambda exclude_unset=True: {"role": "assistant", "content": content})
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def responses_reply(text="done"):
    """A non-streamed Responses body: ``send`` maps it to the reply shape above."""
    return {"status": "completed",
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": text}]}]}


class ScriptedClient:
    """Client double whose next send raises/returns the scripted outcome.

    Carries the attributes ``apply_auth`` re-points (``api_key``/``base_url``) so
    a test can see the rebind land, and records each call's kwargs per wire API.
    """

    def __init__(self, completions=(), responses=(), api_key=TOKEN,
                 base_url=STALE_BASE_URL):
        self._outcomes = {"completions": list(completions), "responses": list(responses)}
        self.calls = {"completions": [], "responses": []}
        self.api_key = api_key
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._send("completions")))
        self.responses = SimpleNamespace(create=self._send("responses"))

    def _send(self, kind):
        def call(**kwargs):
            self.calls[kind].append(kwargs)
            outcome = self._outcomes[kind].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return call

    def counts(self) -> dict:
        return {kind: len(calls) for kind, calls in self.calls.items()}


# --- is_auth_error / can_refresh_credentials ---

def test_is_auth_error_recognises_the_sdk_exception():
    assert is_auth_error(auth_error()) is True


@pytest.mark.parametrize("text", [
    "Error code: 401 - unauthorized",
    "Unauthorized",
    "invalid_bearer_token",
    "{'error': {'code': 'authentication_error'}}",
])
def test_is_auth_error_recognises_a_401_in_the_text(text):
    assert is_auth_error(RuntimeError(text)) is True


@pytest.mark.parametrize("text", [
    "Error code: 503 - service unavailable",
    "Request timed out",
    "Error code: 400 - unknown field 'reasoning_item_ids'",
])
def test_is_auth_error_ignores_everything_that_is_not_a_401(text):
    assert is_auth_error(RuntimeError(text)) is False


def test_only_copilot_can_refresh_its_own_credential():
    """vllm/google have no credential to renew, so their 401s stay untouched."""
    assert can_refresh_credentials(get_provider("github-copilot")) is True
    assert can_refresh_credentials(get_provider("vllm")) is False
    assert can_refresh_credentials(get_provider("google")) is False
    assert can_refresh_credentials(None) is False


# --- apply_auth: re-pointing a live client ---

def test_apply_auth_repoints_a_real_openai_client():
    client = OpenAI(base_url=STALE_BASE_URL, api_key="OLD",
                    default_headers={"Editor-Version": "vscode/1.107.0"})
    apply_auth(client, ResolvedAuth(
        base_url=INDIVIDUAL_BASE_URL, api_key="NEW", timeout=600.0,
        headers={"Editor-Version": "vscode/1.108.0", "User-Agent": "GitHubCopilotChat/0.35.0"}))

    assert client.api_key == "NEW"
    assert str(client.base_url).rstrip("/") == INDIVIDUAL_BASE_URL
    # The two header bags the SDK reads when it builds a request, not just the
    # attributes: a stale bearer living there would keep being sent.
    assert client.auth_headers == {"Authorization": "Bearer NEW"}
    assert client.default_headers["Editor-Version"] == "vscode/1.108.0"
    assert client._client.headers["authorization"] == "Bearer NEW"
    assert client._client.headers["User-Agent"] == "GitHubCopilotChat/0.35.0"


def test_apply_auth_keeps_working_when_the_client_has_no_header_bags():
    """A test double (or a future client) without ``_client`` still re-points."""
    client = SimpleNamespace(api_key="OLD", base_url=STALE_BASE_URL)
    apply_auth(client, ResolvedAuth(base_url=INDIVIDUAL_BASE_URL, api_key="NEW",
                                    headers={"A": "b"}, timeout=1.0))
    assert client.api_key == "NEW"
    assert str(client.base_url).rstrip("/") == INDIVIDUAL_BASE_URL


def test_apply_auth_survives_a_header_bag_it_cannot_write():
    client = SimpleNamespace(api_key="OLD", base_url=STALE_BASE_URL,
                             _custom_headers=["not", "a", "mapping"],
                             _client=SimpleNamespace(headers=object()))
    apply_auth(client, ResolvedAuth(base_url=INDIVIDUAL_BASE_URL, api_key="NEW",
                                    headers={"A": "b"}, timeout=1.0))
    assert client.api_key == "NEW"


# --- rebind_client: refresh, persist, re-point ---

def test_rebind_client_refreshes_persists_and_repoints_the_client(auth_file, offline):
    seed_credential()
    client = ScriptedClient(api_key=TOKEN, base_url=STALE_BASE_URL)

    auth = rebind_client(client, CopilotProvider(), {"model": "gemini-2.5-pro"}, {})

    assert offline.urls() == [COPILOT_TOKEN_URL, MODELS_URL]
    assert offline.calls[0][2]["Authorization"] == "Bearer gh-refresh-token"
    assert auth.api_key == REFRESHED_TOKEN
    assert auth.base_url == INDIVIDUAL_BASE_URL
    assert auth.wire_api == "completions"
    assert client.api_key == REFRESHED_TOKEN
    assert str(client.base_url).rstrip("/") == INDIVIDUAL_BASE_URL
    # the file on disk and the live client agree about the current token
    assert store.read_auth()["github-copilot"]["access"] == REFRESHED_TOKEN


def test_rebind_client_reports_the_wire_api_the_refreshed_catalog_names(auth_file, offline):
    """The catalog is re-read on refresh, so its routing is current too."""
    seed_credential()
    client = ScriptedClient()
    assert rebind_client(client, CopilotProvider(), {"model": "gpt-5"}, {}).wire_api == \
        "responses"


# --- try_recover_auth: the policy at the send sites ---

def test_try_recover_auth_ignores_an_error_it_cannot_fix(auth_file, offline):
    client = ScriptedClient()
    assert try_recover_auth(client, CopilotProvider(), RuntimeError("503 unavailable"),
                            False, {"model": "gemini-2.5-pro"}, {}) is False
    assert offline.calls == []
    assert client.api_key == TOKEN


def test_try_recover_auth_ignores_a_401_from_a_provider_with_no_credential(offline):
    client = ScriptedClient(api_key="cfg-vertex")
    assert try_recover_auth(client, get_provider("vllm"), auth_error(), False) is False
    assert offline.calls == []
    assert client.api_key == "cfg-vertex"


def test_try_recover_auth_refreshes_once_and_says_retry_now(auth_file, offline, logs):
    seed_credential()
    client = ScriptedClient()
    assert try_recover_auth(client, CopilotProvider(), auth_error(), False,
                            {"model": "gemini-2.5-pro"}, {}) is True
    assert client.api_key == REFRESHED_TOKEN
    assert "refreshing and retrying once" in messages_of(logs)


def test_try_recover_auth_aborts_when_a_second_401_follows_a_refresh(auth_file, offline,
                                                                    logs):
    """The stored refresh token produced a credential the server still rejects."""
    seed_credential()
    client = ScriptedClient()
    with pytest.raises(SystemExit):
        try_recover_auth(client, CopilotProvider(), auth_error(), True,
                         {"model": "gemini-2.5-pro"}, {})
    message = messages_of(logs)
    assert "copilot-login" in message
    assert offline.calls == []          # it does not refresh a second time
    assert client.api_key == TOKEN      # and it does not pretend to have fixed it


# --- agent._send_with_retry: the completions path ---

@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def quiet_agent(monkeypatch, tmp_path, sleeps):
    """No real sleeps (they are recorded instead), no telemetry context."""
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(agent_module, "using_session",
                        lambda session_id: contextlib.nullcontext())
    return tmp_path


def call_send_with_retry(client, messages, provider, session_dir, model="gemini-2.5-pro",
                         **kwargs):
    kwargs.setdefault("agent_cfg", {"model": model})
    kwargs.setdefault("config", {})
    return agent_module._send_with_retry(
        client, messages, [], model, 0.0, 100, "session-1",
        session_dir / "session-1.session", no_stream=True, provider=provider, **kwargs)


def test_send_with_retry_refreshes_and_retries_once_on_a_401(auth_file, offline,
                                                            quiet_agent, sleeps, logs):
    seed_credential()
    client = ScriptedClient(completions=[auth_error(), no_stream_response("after refresh")])

    result = call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                                  CopilotProvider(), quiet_agent)

    assert result.choices[0].message.content == "after refresh"
    assert client.counts()["completions"] == 2
    assert client.api_key == REFRESHED_TOKEN
    assert store.read_auth()["github-copilot"]["access"] == REFRESHED_TOKEN
    # only the 0.5s warm-up: the credential retry slept through no backoff
    assert sleeps == [0.5]


def test_send_with_retry_aborts_when_the_refreshed_credential_is_rejected_too(
        auth_file, offline, quiet_agent, sleeps, logs):
    seed_credential()
    client = ScriptedClient(completions=[auth_error(), auth_error()])

    with pytest.raises(SystemExit):
        call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                             CopilotProvider(), quiet_agent)

    assert client.counts()["completions"] == 2
    assert "copilot-login" in messages_of(logs)
    assert sleeps == [0.5]


def test_a_credential_retry_does_not_consume_the_backoff_budget(auth_file, offline,
                                                               quiet_agent, sleeps, logs):
    """After a 401 refresh, the next real outage still backs off from scratch."""
    seed_credential()
    client = ScriptedClient(completions=[
        auth_error(), RuntimeError("Error code: 503 - unavailable"),
        no_stream_response("back on line")])

    result = call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                                  CopilotProvider(), quiet_agent,
                                  max_retries=4, base_delay=5)

    assert result.choices[0].message.content == "back on line"
    assert sleeps == [0.5, 5]           # base_delay * 2**0, not 2**1
    assert "Attempt 1/4" in messages_of(logs)


def test_send_with_retry_recovers_on_the_responses_path(auth_file, offline, quiet_agent,
                                                        sleeps):
    """The 401 handler is shared: gpt-5 goes out through ``responses.create``."""
    seed_credential()
    client = ScriptedClient(responses=[auth_error(), responses_reply("done after refresh")])

    result = call_send_with_retry(
        client, [{"role": "user", "content": "hi"}], CopilotProvider(), quiet_agent,
        model="gpt-5", wire_api="responses", config={})

    assert result.choices[0].message.content == "done after refresh"
    assert client.counts()["responses"] == 2
    assert client.counts()["completions"] == 0
    assert client.api_key == REFRESHED_TOKEN
    assert sleeps == [0.5]


def test_a_401_from_a_provider_without_credentials_keeps_the_old_retry_path(
        offline, quiet_agent, sleeps):
    """Non-Copilot providers: same sleep, same retry, no refresh attempted."""
    client = ScriptedClient(completions=[auth_error(), no_stream_response("retried")])

    result = call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                                  get_provider("vllm"), quiet_agent,
                                  max_retries=2, base_delay=5)

    assert result.choices[0].message.content == "retried"
    assert sleeps == [0.5, 5]
    assert offline.calls == []


def test_the_credential_retry_keeps_the_wire_api_of_the_original_request(auth_file,
                                                                        offline,
                                                                        quiet_agent):
    """Recovery swaps the credential, not the route: the same request goes again.

    The refreshed catalog re-infers ``gpt-4.1`` as a Responses model, so a retry
    that re-read the route would silently change wire APIs mid-conversation.
    """
    seed_credential(available_model_ids=["gpt-4.1"],
                    models={"gpt-4.1": {"api": "completions"}})
    client = ScriptedClient(completions=[auth_error(), no_stream_response("again")])

    result = call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                                  CopilotProvider(), quiet_agent, model="gpt-4.1")

    assert result.choices[0].message.content == "again"
    assert client.counts() == {"completions": 2, "responses": 0}
    # the catalog really did change under the run; only the route stayed put
    assert store.read_auth()["github-copilot"]["models"]["gpt-4.1"]["api"] == "responses"


# --- execute_agent: the recovery is wired to the real call sites ---

def _mock_agent_run(mocker, tmp_path, agent_name, config_models, seeded=False):
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("soul data")
    if seeded:
        seed_credential()
    mocker.patch.object(agent_module, "get_config",
                        return_value={"sessions_root": str(tmp_path),
                                      "models": config_models})
    mock_workflow = mocker.MagicMock()
    mock_workflow.agents_dir = tmp_path
    mocker.patch("sdlc_factory.workflows.get_workflow", return_value=mock_workflow)
    mocker.patch("sdlc_factory.telemetry.setup_telemetry")
    mocker.patch.object(agent_module, "using_session", return_value=mocker.MagicMock())
    mock_client = mocker.patch.object(agent_module, "OpenAI", create=True).return_value
    return mock_client


def _stream_chunk(mocker, content):
    chunk = mocker.MagicMock()
    chunk.choices = [mocker.MagicMock()]
    chunk.choices[0].delta.content = content
    chunk.choices[0].delta.tool_calls = []
    chunk.model_dump.return_value = {"choices": [{"delta": {}}]}
    return chunk


COPILOT_CODER = {"coder": {"model": "gemini-2.5-pro", "provider": "github-copilot"}}


def test_execute_agent_recovers_from_a_401_mid_run(mocker, auth_file, offline, tmp_path,
                                                  logs):
    mock_client = _mock_agent_run(mocker, tmp_path, "coder", COPILOT_CODER, seeded=True)
    mocker.patch.object(agent_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    mock_client.chat.completions.create.side_effect = [
        auth_error(), [_stream_chunk(mocker, "recovered and finished")]]

    assert agent_module.execute_agent("coder", "do the thing") == "recovered and finished"
    assert mock_client.chat.completions.create.call_count == 2
    assert mock_client.api_key == REFRESHED_TOKEN
    assert store.read_auth()["github-copilot"]["access"] == REFRESHED_TOKEN


def test_execute_agent_aborts_when_the_refreshed_credential_is_rejected(mocker, auth_file,
                                                                       offline, tmp_path,
                                                                       logs):
    mock_client = _mock_agent_run(mocker, tmp_path, "coder", COPILOT_CODER, seeded=True)
    mocker.patch.object(agent_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    mock_client.chat.completions.create.side_effect = [auth_error(), auth_error()]

    with pytest.raises(SystemExit):
        agent_module.execute_agent("coder", "do the thing")
    assert "copilot-login" in messages_of(logs)


# --- chat.py: the same recovery on the interactive path ---

def _mock_chat_run(mocker, tmp_path, session_id, config_models, seeded=False):
    (tmp_path / f"{session_id}.session").write_text("[]", encoding="utf-8")
    mocker.patch.object(chat_module, "get_config",
                        return_value={"sessions_root": str(tmp_path),
                                      "models": config_models})
    if seeded:
        seed_credential()
    mock_client = mocker.patch.object(chat_module, "OpenAI", create=True).return_value
    mocker.patch("builtins.input", side_effect=["hello", EOFError])
    return mock_client


def _chat_reply(content="reply"):
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=[]))])
    return response


def test_chat_session_refreshes_and_retries_once_on_a_401(mocker, auth_file, offline,
                                                          tmp_path):
    seed_credential()
    mock_client = _mock_chat_run(mocker, tmp_path, "coder-123", COPILOT_CODER)
    mock_client.chat.completions.create.side_effect = [
        auth_error(), _chat_reply("after refresh")]

    chat_module.run_chat_session("coder-123")

    assert mock_client.chat.completions.create.call_count == 2
    assert mock_client.api_key == REFRESHED_TOKEN
    assert store.read_auth()["github-copilot"]["access"] == REFRESHED_TOKEN


def test_chat_session_aborts_on_a_second_401(mocker, auth_file, offline, tmp_path, logs):
    seed_credential()
    mock_client = _mock_chat_run(mocker, tmp_path, "coder-123", COPILOT_CODER)
    mock_client.chat.completions.create.side_effect = [auth_error(), auth_error()]

    with pytest.raises(SystemExit):
        chat_module.run_chat_session("coder-123")
    assert mock_client.chat.completions.create.call_count == 2
    assert "copilot-login" in messages_of(logs)


def test_chat_session_reports_a_401_it_cannot_fix_as_before(mocker, auth_file, offline,
                                                           tmp_path, logs):
    """vllm: no refresh, and the error still lands in the caller's handler."""
    mock_client = _mock_chat_run(mocker, tmp_path, "coder-123", {})
    mock_client.chat.completions.create.side_effect = auth_error()

    chat_module.run_chat_session("coder-123")

    assert mock_client.chat.completions.create.call_count == 1
    assert offline.calls == []
    assert "Chat Error" in messages_of(logs)


# --- the session-start line ---

def test_execute_agent_logs_the_provider_it_resolved(mocker, auth_file, offline, tmp_path,
                                                     logs):
    mock_client = _mock_agent_run(mocker, tmp_path, "coder", COPILOT_CODER, seeded=True)
    mock_client.chat.completions.create.return_value = [_stream_chunk(mocker, "done")]

    agent_module.execute_agent("coder", "do the thing")

    line = next(record for record in logs if "Provider:" in record.getMessage())
    text = line.getMessage()
    for fact in ("github-copilot", INDIVIDUAL_BASE_URL, "gemini-2.5-pro", "completions"):
        assert fact in text
    assert line.color == typer.colors.CYAN


def test_chat_session_logs_the_provider_it_resolved(mocker, auth_file, offline, tmp_path,
                                                    logs):
    seed_credential()
    mock_client = _mock_chat_run(mocker, tmp_path, "coder-123", COPILOT_CODER)
    mock_client.chat.completions.create.return_value = _chat_reply()

    chat_module.run_chat_session("coder-123")

    line = next(record for record in logs if "Provider:" in record.getMessage())
    text = line.getMessage()
    for fact in ("github-copilot", INDIVIDUAL_BASE_URL, "gemini-2.5-pro", "completions"):
        assert fact in text
    assert line.color == typer.colors.CYAN


def test_the_session_line_names_the_legacy_vllm_default(mocker, tmp_path, logs):
    """The line is the audit trail for the default path too, not just Copilot."""
    mock_client = _mock_agent_run(mocker, tmp_path, "coder", {"coder": {"model": "local-vllm"}})
    mock_client.chat.completions.create.return_value = [_stream_chunk(mocker, "done")]

    agent_module.execute_agent("coder", "do the thing")

    line = next(record for record in logs if "Provider:" in record.getMessage())
    assert "vllm" in line.getMessage()


# --- auth.json permission warning ---

def _warning_records(records) -> list:
    return [record for record in records if "chmod 600" in record.getMessage()]


def test_read_auth_warns_once_when_auth_json_is_group_readable(auth_file, logs):
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"github-copilot": {"access": "a"}}', encoding="utf-8")
    os.chmod(auth_file, 0o644)

    for _ in range(3):
        store.read_auth()

    warnings = _warning_records(logs)
    assert len(warnings) == 1
    assert "0644" in warnings[0].getMessage()
    assert str(auth_file) in warnings[0].getMessage()


def test_read_auth_warns_when_only_the_executable_bit_is_loose(auth_file, logs):
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    os.chmod(auth_file, 0o601)

    store.read_auth()

    assert len(_warning_records(logs)) == 1


def test_read_auth_stays_quiet_when_permissions_are_tight(auth_file, logs):
    store.write_auth({"github-copilot": {"access": "a"}})

    store.read_auth()

    assert _warning_records(logs) == []
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_read_auth_stays_quiet_when_the_file_is_missing(auth_file, logs):
    assert store.read_auth() == {}
    assert _warning_records(logs) == []


def test_the_permission_warning_is_not_resent_after_write_auth_repairs_it(auth_file, logs):
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    os.chmod(auth_file, 0o644)
    store.read_auth()
    assert len(_warning_records(logs)) == 1

    os.chmod(auth_file, 0o600)
    store.read_auth()
    assert len(_warning_records(logs)) == 1