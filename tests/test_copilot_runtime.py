"""T6 — GitHub Copilot runtime resolution + dynamic per-request headers.

Two things are under test here:

``CopilotProvider.resolve`` — turn the stored ``auth.json`` credential into a
``ResolvedAuth``: refresh on expiry, base URL derived from the token itself
(pi regression #6768), the wire API recorded per model in the catalog, and the
legacy ``api_timeout``. All refresh traffic goes through the injected fake seam;
``auth.json`` lives under ``tmp_path``. No test here opens a socket.

``prepare_headers`` + the send path — the per-request headers Copilot gates on,
recomputed for every ``create()`` because ``agent.py`` builds messages
incrementally, and threaded from the ``make_client`` call sites down into
``client.chat.completions.create(extra_headers=...)``.
"""

import contextlib
import logging
import time
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from sdlc_factory import agent as agent_module
from sdlc_factory import chat as chat_module
from sdlc_factory.providers import PROVIDERS, get_provider, request_headers
from sdlc_factory.providers import store
from sdlc_factory.providers.copilot import (
    EDITOR_HEADERS,
    INDIVIDUAL_BASE_URL,
    CopilotProvider,
    HttpError,
    credential_is_expired,
    prepare_headers,
)

COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
MODELS_URL = f"{INDIVIDUAL_BASE_URL}/models"

TOKEN = "tid=abc;exp=1800000000;proxy-ep=proxy.individual.githubcopilot.com"
REFRESHED_TOKEN = "tid=def;exp=1800003600;proxy-ep=proxy.individual.githubcopilot.com"
# A token with no proxy-ep claim: the base URL must fall back, never to config.
BARE_TOKEN = "tid=bare;exp=1800000000"

TEN_MINUTES_MS = 10 * 60 * 1000
EXPIRED = 1_000  # epoch millis, i.e. 1970: definitively in the past


# --- abort() capture: it logs the message, then raises SystemExit(1) ---

@pytest.fixture
def aborts():
    """Collect the messages ``abort()`` logs, so tests can assert on the text."""
    records: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("sdlc_factory")
    handler = Collector()
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def expect_abort(action, records: list) -> str:
    """Run ``action``, expecting ``abort()``; return everything it logged."""
    before = len(records)
    try:
        action()
    except SystemExit:
        return "\n".join(records[before:])
    raise AssertionError("expected abort() (SystemExit), but nothing was raised")


# --- fake HTTP seam (same shape as the T4 auth tests: no sockets) ---

class HttpCall(NamedTuple):
    kind: str
    url: str
    headers: dict


class FakeHttp:
    """Scripted GET/post seam: URL -> response, or a list consumed in order.

    An exception instance raises, a callable is invoked with (url, headers). An
    unscripted URL is an assertion failure, so an unexpected call is loud.
    """

    def __init__(self, get=None, post_form=None, post_json=None):
        self.calls: list[HttpCall] = []
        self._script = {"get": get or {}, "post_form": post_form or {},
                        "post_json": post_json or {}}

    def get(self, url, headers=None):
        return self._call("get", url, headers)

    def post_form(self, url, data, headers=None):
        return self._call("post_form", url, headers)

    def post_json(self, url, payload, headers=None):
        return self._call("post_json", url, headers)

    def urls(self, kind: str = "get") -> list:
        return [call.url for call in self.calls if call.kind == kind]

    def headers_for(self, url: str, kind: str = "get") -> dict:
        for call in self.calls:
            if call.kind == kind and call.url == url:
                return call.headers
        raise AssertionError(f"no {kind.upper()} to {url} was recorded")

    def _call(self, kind, url, headers):
        self.calls.append(HttpCall(kind, url, dict(headers or {})))
        script = self._script[kind]
        if url not in script:
            raise AssertionError(f"unexpected {kind.upper()} {url}")
        entry = script[url]
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(url, headers)
        return entry if entry is not None else {}


# --- fixtures + builders ---

@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / ".sdlc-factory" / "auth.json"
    monkeypatch.setattr(store, "AUTH_FILE", path)
    return path


def catalog_payload(*model_ids):
    return {"data": [{"id": model_id, "model_picker_enabled": True,
                      "policy": {"state": "enabled"},
                      "capabilities": {"supports": {"tool_calls": True}}}
                     for model_id in model_ids]}


def seed_credential(**overrides) -> dict:
    """Write a usable ``github-copilot`` credential into auth.json."""
    credential = {
        "refresh": "gh-refresh-token",
        "access": TOKEN,
        "expires": int(time.time() * 1000) + TEN_MINUTES_MS,
        "enterprise_url": None,
        "available_model_ids": ["gpt-4.1", "gpt-5", "oswe-v1"],
        "models": {"gpt-4.1": {"api": "completions"}, "gpt-5": {"api": "responses"},
                   "oswe-v1": {"api": "responses"}},
    }
    credential.update(overrides)
    data = store.read_auth()
    data["github-copilot"] = credential
    store.write_auth(data)
    return credential


def refresh_http(*model_ids, token=REFRESHED_TOKEN) -> FakeHttp:
    """Seam scripted for one refresh: a new proxy token, then the catalog."""
    return FakeHttp(get={
        COPILOT_TOKEN_URL: {"token": token, "expires_at": int(time.time()) + 600},
        MODELS_URL: catalog_payload(*model_ids),
    })


def resolve(agent_cfg=None, config=None, http=None, provider=None):
    return (provider or CopilotProvider()).resolve(agent_cfg or {}, config or {}, http=http)


# --- resolve: no credential, expiry, refresh ---

def test_resolve_aborts_when_not_logged_in(auth_file, aborts):
    message = expect_abort(lambda: resolve({"model": "gpt-4.1"}), aborts)
    assert "Copilot not configured" in message
    assert "copilot-login" in message


def test_resolve_aborts_when_the_stored_credential_has_no_access_token(auth_file, aborts):
    """A half-written credential must not become a request with an empty bearer."""
    seed_credential(access=None)
    assert "Copilot not configured" in expect_abort(
        lambda: resolve({"model": "gpt-4.1"}), aborts)


def test_resolve_uses_the_stored_credential_without_touching_the_network(auth_file):
    seed_credential()
    fake = FakeHttp()  # any HTTP call would raise AssertionError
    auth = resolve({"model": "gpt-4.1"}, {}, http=fake)
    assert auth.base_url == INDIVIDUAL_BASE_URL
    assert auth.api_key == TOKEN
    assert auth.headers == EDITOR_HEADERS
    assert auth.timeout == 600.0
    assert auth.wire_api == "completions"
    assert fake.calls == []


def test_resolve_timeout_comes_from_config_api_timeout(auth_file):
    seed_credential()
    assert resolve({"model": "gpt-4.1"}, {"api_timeout": 30}).timeout == 30.0


@pytest.mark.parametrize("raw", [None, "soon", True])
def test_resolve_treats_an_unusable_expiry_as_expired(auth_file, raw):
    seed_credential(expires=raw)
    auth = resolve({"model": "gpt-4.1"}, {}, http=refresh_http("gpt-4.1"))
    assert auth.api_key == REFRESHED_TOKEN


def test_resolve_treats_a_missing_expiry_as_expired(auth_file):
    credential = seed_credential()
    credential.pop("expires")
    store.write_auth({"github-copilot": credential})
    auth = resolve({"model": "gpt-4.1"}, {}, http=refresh_http("gpt-4.1"))
    assert auth.api_key == REFRESHED_TOKEN


def test_credential_is_expired_compares_millis_against_the_injected_clock():
    """The expiry is epoch millis; the 5-minute buffer is already subtracted."""
    assert credential_is_expired({"expires": 1_000}, now_ms=1_000) is True
    assert credential_is_expired({"expires": 2_000}, now_ms=1_999) is False
    assert credential_is_expired({"expires": 2_000}, now_ms=2_000) is True
    for unusable in ({}, {"expires": None}, {"expires": "soon"}, {"expires": True}):
        assert credential_is_expired(unusable, now_ms=0) is True


def test_resolve_refreshes_an_expired_credential_and_rewrites_auth_json(auth_file):
    store.write_auth({"anthropic": {"api_key": "untouched"},
                      "github-copilot": dict(seed_credential(), expires=EXPIRED)})
    fake = refresh_http("gpt-4.1", "gpt-5")
    auth = resolve({"model": "gpt-4.1"}, {}, http=fake)

    assert auth.api_key == REFRESHED_TOKEN
    assert fake.urls() == [COPILOT_TOKEN_URL, MODELS_URL]
    assert fake.headers_for(COPILOT_TOKEN_URL)["Authorization"] == "Bearer gh-refresh-token"
    assert fake.headers_for(MODELS_URL)["Authorization"] == f"Bearer {REFRESHED_TOKEN}"

    stored = store.read_auth()
    assert stored["anthropic"] == {"api_key": "untouched"}
    refreshed = stored["github-copilot"]
    assert refreshed["access"] == REFRESHED_TOKEN
    assert refreshed["refresh"] == "gh-refresh-token"
    assert refreshed["expires"] > int(time.time() * 1000)
    assert "gpt-4.1" in refreshed["available_model_ids"]
    assert refreshed["models"]["gpt-5"]["api"] == "responses"


def test_resolve_keeps_previously_enabled_models_available_after_refresh(auth_file):
    """A refresh rotates the credential; it is not a re-authorization."""
    store.write_auth({"github-copilot": dict(seed_credential(), expires=EXPIRED)})
    # The fresh catalog only reports one of the three stored models.
    resolve({"model": "gpt-4.1"}, {}, http=refresh_http("gpt-4.1"))
    stored = store.read_auth()["github-copilot"]
    assert set(stored["available_model_ids"]) >= {"gpt-4.1", "gpt-5", "oswe-v1"}
    assert stored["models"]["oswe-v1"]["api"] == "responses"


def test_resolve_replays_the_stored_enterprise_domain_on_refresh(auth_file):
    enterprise = "company.ghe.com"
    store.write_auth({"github-copilot": dict(seed_credential(enterprise_url=enterprise),
                                             expires=EXPIRED)})
    enterprise_models = "https://api.copilot-proxy.githubcopilot.com/models"
    fake = FakeHttp(get={
        f"https://api.{enterprise}/copilot_internal/v2/token":
            {"token": "tid=x;exp=1;proxy-ep=proxy.copilot-proxy.githubcopilot.com",
             "expires_at": int(time.time()) + 600},
        enterprise_models: catalog_payload("gpt-4.1"),
    })
    auth = resolve({"model": "gpt-4.1"}, {}, http=fake)
    assert auth.base_url == "https://api.copilot-proxy.githubcopilot.com"
    assert fake.urls() == [f"https://api.{enterprise}/copilot_internal/v2/token",
                           enterprise_models]


def test_resolve_aborts_when_the_refresh_fails(auth_file, aborts):
    seed_credential(expires=EXPIRED)
    fake = FakeHttp(get={COPILOT_TOKEN_URL: HttpError(401, "bad credentials",
                                                      COPILOT_TOKEN_URL)})
    message = expect_abort(lambda: resolve({"model": "gpt-4.1"}, {}, http=fake), aborts)
    assert "Copilot session expired" in message
    assert "copilot-login" in message
    # a failed refresh leaves the stored credential alone: it is not a logout
    assert store.read_auth()["github-copilot"]["access"] == TOKEN


def test_resolve_aborts_when_no_refresh_token_is_stored(auth_file, aborts):
    seed_credential(refresh=None, expires=EXPIRED)
    message = expect_abort(
        lambda: resolve({"model": "gpt-4.1"}, {}, http=FakeHttp()), aborts)
    assert "Copilot session expired" in message


def test_refresh_classmethod_is_reusable_on_its_own(auth_file):
    """T8's 401 path calls the classmethod directly, not through ``resolve``."""
    seed_credential(expires=EXPIRED)
    credential = CopilotProvider.refresh(http=refresh_http("gpt-4.1"))
    assert credential["access"] == REFRESHED_TOKEN
    assert store.read_auth()["github-copilot"]["access"] == REFRESHED_TOKEN


# --- resolve: base URL is the token's, never the config's (pi #6768 analog) ---

def test_base_url_comes_from_the_token_even_when_config_disagrees(auth_file):
    """Regression: retry/session paths must use the auth-resolved URL."""
    seed_credential()
    config = {
        "vllm_base_url": "https://vllm.test:8443/v1",
        "providers": {"github-copilot": {"base_url": "https://static-catalog.test/v1"}},
    }
    assert resolve({"model": "gpt-4.1"}, config).base_url == INDIVIDUAL_BASE_URL


def test_base_url_follows_a_non_individual_proxy_endpoint(auth_file):
    seed_credential(access="tid=x;exp=1;proxy-ep=proxy.copilot-proxy-primary.apis.githubcopilot.com")
    assert resolve({"model": "gpt-4.1"}, {}).base_url == \
        "https://api.copilot-proxy-primary.apis.githubcopilot.com"


def test_base_url_falls_back_to_enterprise_then_individual_never_config(auth_file):
    seed_credential(access=BARE_TOKEN, enterprise_url="company.ghe.com")
    assert resolve({"model": "gpt-4.1"}, {"vllm_base_url": "https://vllm.test/v1"}).base_url == \
        "https://copilot-api.company.ghe.com"
    seed_credential(access=BARE_TOKEN, enterprise_url=None)
    assert resolve({"model": "gpt-4.1"}, {"vllm_base_url": "https://vllm.test/v1"}).base_url == \
        INDIVIDUAL_BASE_URL


def test_base_url_is_rederived_from_the_refreshed_token(auth_file):
    seed_credential(expires=EXPIRED)
    fake = FakeHttp(get={
        COPILOT_TOKEN_URL: {"token": "tid=x;exp=1;proxy-ep=proxy.enterprise.ghe.com",
                            "expires_at": int(time.time()) + 600},
        "https://api.enterprise.ghe.com/models": catalog_payload("gpt-4.1"),
    })
    assert resolve({"model": "gpt-4.1"}, {}, http=fake).base_url == "https://api.enterprise.ghe.com"


# --- resolve: wire API from the catalog ---

@pytest.mark.parametrize("model_id, expected", [
    ("gpt-4.1", "completions"),
    ("gpt-5", "responses"),
    ("oswe-v1", "responses"),
])
def test_resolve_reports_the_catalog_wire_api(auth_file, model_id, expected):
    seed_credential()
    assert resolve({"model": model_id}, {}).wire_api == expected


def test_resolve_aborts_when_the_model_is_not_in_the_catalog(auth_file, aborts):
    seed_credential()
    message = expect_abort(lambda: resolve({"model": "llama-3.3-70b"}, {}), aborts)
    assert "llama-3.3-70b" in message
    for available in ("gpt-4.1", "gpt-5", "oswe-v1"):
        assert available in message


def test_resolve_aborts_when_the_model_needs_an_unsupported_wire_api(auth_file, aborts):
    seed_credential(available_model_ids=["gpt-4.1", "claude-sonnet-4.5"],
                    models={"gpt-4.1": {"api": "completions"},
                            "claude-sonnet-4.5": {"api": "unsupported"}})
    message = expect_abort(lambda: resolve({"model": "claude-sonnet-4.5"}, {}), aborts)
    assert "claude-sonnet-4.5" in message
    assert "anthropic-messages" in message.lower()


def test_resolve_infers_the_wire_api_when_no_catalog_block_was_stored(auth_file):
    """Pre-T4 credentials stored no per-model api; the id prefix stands in."""
    seed_credential(models=None, available_model_ids=["gemini-2.5-pro", "gpt-4.1"])
    assert resolve({"model": "gemini-2.5-pro"}, {}).wire_api == "completions"
    assert resolve({"model": "gpt-4.1"}, {}).wire_api == "responses"


def test_resolve_aborts_when_no_model_is_configured(auth_file, aborts):
    seed_credential()
    assert "model" in expect_abort(lambda: resolve({}, {}), aborts)


# --- dynamic headers ---

def test_prepare_headers_initiator_is_user_for_a_fresh_prompt():
    headers = prepare_headers([{"role": "system", "content": "s"},
                               {"role": "user", "content": "hi"}])
    assert headers["X-Initiator"] == "user"
    assert headers["Openai-Intent"] == "conversation-edits"
    assert "Copilot-Vision-Request" not in headers


@pytest.mark.parametrize("role", ["assistant", "tool", "toolResult", "system"])
def test_prepare_headers_initiator_is_agent_when_the_last_message_is_not_user(role):
    messages = [{"role": "user", "content": "hi"}, {"role": role, "content": "x"}]
    assert prepare_headers(messages)["X-Initiator"] == "agent"


def test_prepare_headers_initiator_defaults_to_user_with_no_messages():
    assert prepare_headers([])["X-Initiator"] == "user"
    assert prepare_headers(None)["X-Initiator"] == "user"


def test_prepare_headers_reads_sdk_message_objects():
    """``chat.py`` appends the SDK's message objects, not dicts."""
    message = SimpleNamespace(role="assistant", content="x")
    assert prepare_headers([{"role": "user", "content": "hi"}, message])["X-Initiator"] == "agent"


def test_prepare_headers_flags_a_vision_request_from_user_content():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is in this screenshot?"},
        {"type": "image", "image_url": {"url": "https://x.test/y.png"}},
    ]}]
    assert prepare_headers(messages)["Copilot-Vision-Request"] == "true"


def test_prepare_headers_flags_a_vision_request_from_tool_result_content():
    messages = [
        {"role": "user", "content": "read the screenshot"},
        {"role": "assistant", "content": "looking"},
        {"role": "toolResult", "content": [{"type": "image", "image_url": {"url": "data:,"}}]},
    ]
    assert prepare_headers(messages)["Copilot-Vision-Request"] == "true"
    assert prepare_headers(messages)["X-Initiator"] == "agent"


@pytest.mark.parametrize("role", ["user", "toolResult", "tool"])
def test_prepare_headers_ignores_string_content(role):
    messages = [{"role": role, "content": "a screenshot URL as text: https://x.test/y.png"}]
    assert "Copilot-Vision-Request" not in prepare_headers(messages)


@pytest.mark.parametrize("role", ["assistant", "system"])
def test_prepare_headers_ignores_images_outside_user_and_tool_results(role):
    messages = [{"role": role, "content": [{"type": "image", "image_url": {"url": "data:,"}}]}]
    assert "Copilot-Vision-Request" not in prepare_headers(messages)


def test_prepare_headers_ignores_malformed_content():
    assert "Copilot-Vision-Request" not in prepare_headers([{"role": "user", "content": None}])
    assert "Copilot-Vision-Request" not in prepare_headers([{"role": "user", "content": ["bare"]}])
    assert "Copilot-Vision-Request" not in prepare_headers(["not-a-message"])


def test_copilot_provider_exposes_prepare_headers():
    headers = CopilotProvider().prepare_headers([{"role": "user", "content": "hi"}])
    assert headers["X-Initiator"] == "user"


# --- request_headers: the caller-side seam ---

def test_request_headers_is_none_for_providers_without_dynamic_headers():
    assert request_headers(None, [{"role": "user", "content": "hi"}]) is None
    assert request_headers(get_provider("vllm"), [{"role": "user", "content": "hi"}]) is None
    assert request_headers(get_provider("google"), []) is None


def test_request_headers_returns_copilot_headers():
    headers = request_headers(get_provider("github-copilot"), [{"role": "user", "content": "hi"}])
    assert headers["X-Initiator"] == "user"
    assert headers["Openai-Intent"] == "conversation-edits"


def test_request_headers_survives_a_provider_without_the_hook():
    class Legacy:
        id = "legacy"

    assert request_headers(Legacy(), []) is None


# --- the send path threads the provider through and sends the headers ---

class CapturingClient:
    """Minimal stand-in for an OpenAI client, recording create() kwargs."""

    def __init__(self, response):
        self.calls = []
        self._response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def no_stream_response(content="ok"):
    message = SimpleNamespace(model_dump=lambda exclude_unset=True: {
        "role": "assistant", "content": content})
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def quiet_agent(monkeypatch, tmp_path):
    """No sleeps, no telemetry context; session files land in tmp_path."""
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(agent_module, "using_session",
                        lambda session_id: contextlib.nullcontext())
    return tmp_path


def call_send_with_retry(client, messages, provider, session_dir):
    return agent_module._send_with_retry(
        client, messages, [], "gpt-4.1", 0.0, 100, "session-1",
        session_dir / "session-1.session", no_stream=True, provider=provider)


def test_send_with_retry_sends_copilot_headers(auth_file, quiet_agent):
    seed_credential()
    client = CapturingClient(no_stream_response())
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    call_send_with_retry(client, messages, CopilotProvider(), quiet_agent)
    headers = client.calls[0]["extra_headers"]
    assert headers["X-Initiator"] == "user"
    assert headers["Openai-Intent"] == "conversation-edits"
    assert "Copilot-Vision-Request" not in headers


def test_send_with_retry_recomputes_headers_per_send(auth_file, quiet_agent):
    """The hook runs per send: agent.py appends messages between calls."""
    seed_credential()
    client = CapturingClient(no_stream_response())
    messages = [{"role": "user", "content": "hi"}]
    call_send_with_retry(client, messages, CopilotProvider(), quiet_agent)
    messages.append({"role": "user", "content": [{"type": "image", "image_url": {"url": "data:,"}}]})
    call_send_with_retry(client, messages, CopilotProvider(), quiet_agent)
    assert len(client.calls) == 2
    assert "Copilot-Vision-Request" not in client.calls[0]["extra_headers"]
    assert client.calls[1]["extra_headers"]["Copilot-Vision-Request"] == "true"


def test_send_with_retry_sends_no_extra_headers_without_a_provider(quiet_agent):
    client = CapturingClient(no_stream_response())
    call_send_with_retry(client, [{"role": "user", "content": "hi"}], None, quiet_agent)
    assert client.calls[0]["extra_headers"] is None


def test_send_with_retry_sends_no_extra_headers_for_vllm(quiet_agent):
    client = CapturingClient(no_stream_response())
    call_send_with_retry(client, [{"role": "user", "content": "hi"}],
                         get_provider("vllm"), quiet_agent)
    assert client.calls[0]["extra_headers"] is None


def _mock_agent_run(mocker, tmp_path, agent_name, config_models, seeded=False):
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("soul data")
    if seeded:
        seed_credential()
    mocker.patch.object(agent_module, "get_config",
                        return_value={"sessions_root": str(tmp_path), "models": config_models})
    mock_workflow = mocker.MagicMock()
    mock_workflow.agents_dir = tmp_path
    mocker.patch("sdlc_factory.workflows.get_workflow", return_value=mock_workflow)
    mocker.patch("sdlc_factory.telemetry.setup_telemetry")
    mocker.patch.object(agent_module, "using_session", return_value=mocker.MagicMock())
    mock_client = mocker.patch.object(agent_module, "OpenAI", create=True).return_value
    chunk = mocker.MagicMock()
    chunk.choices = [mocker.MagicMock()]
    chunk.choices[0].delta.content = "thought for the day"
    chunk.choices[0].delta.tool_calls = []
    chunk.model_dump.return_value = {"choices": [{"delta": {}}]}
    mock_client.chat.completions.create.return_value = [chunk]
    return mock_client


def test_execute_agent_sends_copilot_headers(mocker, auth_file, tmp_path):
    """The provider object reaches ``_send_with_retry`` from ``execute_agent``."""
    mock_client = _mock_agent_run(
        mocker, tmp_path, "dreamer",
        {"dreamer": {"model": "gpt-4.1", "provider": "github-copilot"}}, seeded=True)
    assert agent_module.execute_agent("dreamer", "reflect") == "thought for the day"
    headers = mock_client.chat.completions.create.call_args.kwargs["extra_headers"]
    assert headers["X-Initiator"] == "user"
    assert headers["Openai-Intent"] == "conversation-edits"


def test_execute_agent_sends_no_extra_headers_for_vllm(mocker, tmp_path):
    mock_client = _mock_agent_run(mocker, tmp_path, "coder",
                                  {"coder": {"model": "local-vllm"}})
    agent_module.execute_agent("coder", "do the thing")
    assert mock_client.chat.completions.create.call_args.kwargs["extra_headers"] is None


def _mock_chat_run(mocker, tmp_path, session_id, config_models):
    (tmp_path / f"{session_id}.session").write_text("[]", encoding="utf-8")
    mocker.patch.object(chat_module, "get_config",
                        return_value={"sessions_root": str(tmp_path), "models": config_models})
    mock_client = mocker.patch.object(chat_module, "OpenAI", create=True).return_value
    response = mocker.MagicMock()
    response.choices = [mocker.MagicMock()]
    response.choices[0].message.tool_calls = []
    response.choices[0].message.content = "reply"
    mock_client.chat.completions.create.return_value = response
    mocker.patch("builtins.input", side_effect=["hello", EOFError])
    return mock_client


def test_chat_session_sends_copilot_headers(mocker, auth_file, tmp_path):
    """``chat.py`` resolves the same provider and sends the same headers."""
    seed_credential()
    mock_client = _mock_chat_run(mocker, tmp_path, "dreamer-123",
                                 {"dreamer": {"model": "gpt-4.1", "provider": "github-copilot"}})
    chat_module.run_chat_session("dreamer-123")
    headers = mock_client.chat.completions.create.call_args.kwargs["extra_headers"]
    assert headers["X-Initiator"] == "user"
    assert headers["Openai-Intent"] == "conversation-edits"


def test_chat_session_sends_no_extra_headers_for_vllm(mocker, tmp_path):
    mock_client = _mock_chat_run(mocker, tmp_path, "coder-123", {})
    chat_module.run_chat_session("coder-123")
    assert mock_client.chat.completions.create.call_args.kwargs["extra_headers"] is None


# --- registry wiring ---

def test_copilot_is_registered_and_resolves():
    assert isinstance(PROVIDERS["github-copilot"], CopilotProvider)
    assert get_provider("github-copilot") is PROVIDERS["github-copilot"]