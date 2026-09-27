import json
import stat

import pytest
from openai import OpenAI

from sdlc_factory.providers import PROVIDERS, get_provider, make_client
from sdlc_factory.providers import store
from sdlc_factory.providers.base import Provider, ResolvedAuth
from sdlc_factory.providers.google import GoogleProvider
from sdlc_factory.providers.vllm import VllmProvider

DEFAULT_VLLM_URL = "http://sagittarius-a.mara-balance.ts.net:8100/v1"
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.fixture(autouse=True)
def clean_key_env(monkeypatch):
    """Resolution chains must not be polluted by the developer's real env."""
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --- ResolvedAuth / Provider base ---

def test_resolved_auth_defaults():
    auth = ResolvedAuth(base_url="http://x/v1", api_key="k", timeout=600.0)
    assert auth.base_url == "http://x/v1"
    assert auth.api_key == "k"
    assert auth.timeout == 600.0
    assert auth.headers == {}
    assert auth.wire_api == "completions"

def test_provider_prepare_headers_defaults_empty():
    class Dummy(Provider):
        id = "dummy"

        def resolve(self, agent_cfg, config):
            return ResolvedAuth(base_url="http://x/v1", api_key="k", timeout=1.0)

    assert Dummy().prepare_headers([{"role": "user", "content": "hi"}]) == {}

def test_provider_is_abstract():
    with pytest.raises(TypeError):
        Provider()


# --- vllm resolution (legacy chain, verbatim from agent.py/chat.py) ---

@pytest.mark.parametrize("config, env, expected", [
    ({"vertex_api_key": "cfg-vertex", "gemini_api_key": "cfg-gemini"},
     {"GEMINI_API_KEY": "env-gemini", "OPENAI_API_KEY": "env-openai"}, "cfg-vertex"),
    ({"gemini_api_key": "cfg-gemini"},
     {"GEMINI_API_KEY": "env-gemini", "OPENAI_API_KEY": "env-openai"}, "cfg-gemini"),
    ({}, {"GEMINI_API_KEY": "env-gemini", "OPENAI_API_KEY": "env-openai"}, "env-gemini"),
    ({}, {"OPENAI_API_KEY": "env-openai"}, "env-openai"),
    ({"vertex_api_key": "", "gemini_api_key": "cfg-gemini"}, {}, "cfg-gemini"),
    ({}, {}, "EMPTY"),
])
def test_vllm_api_key_fallback_chain(config, env, expected, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert VllmProvider().resolve({}, config).api_key == expected

def test_vllm_default_base_url():
    auth = VllmProvider().resolve({}, {})
    assert auth.base_url == DEFAULT_VLLM_URL

def test_vllm_base_url_override():
    config = {"vllm_base_url": "https://example.test:8443/v1"}
    assert VllmProvider().resolve({}, config).base_url == "https://example.test:8443/v1"

def test_vllm_headers_are_empty():
    assert VllmProvider().resolve({}, {}).headers == {}

def test_vllm_wire_api_is_completions():
    assert VllmProvider().resolve({}, {}).wire_api == "completions"


# --- google resolution (legacy chain, verbatim from agent.py/chat.py) ---

@pytest.mark.parametrize("config, env, expected", [
    ({"gemini_api_key": "cfg-gemini", "vertex_api_key": "cfg-vertex"},
     {"GEMINI_API_KEY": "env-gemini", "OPENAI_API_KEY": "env-openai"}, "cfg-gemini"),
    ({}, {"GEMINI_API_KEY": "env-gemini"}, "env-gemini"),
    ({"vertex_api_key": "cfg-vertex"}, {"GEMINI_API_KEY": "env-gemini"}, "env-gemini"),
    ({"vertex_api_key": "cfg-vertex"}, {"OPENAI_API_KEY": "env-openai"}, "cfg-vertex"),
    ({}, {"OPENAI_API_KEY": "env-openai"}, "env-openai"),
    ({}, {}, "EMPTY"),
])
def test_google_api_key_fallback_chain(config, env, expected, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert GoogleProvider().resolve({}, config).api_key == expected

def test_google_base_url_is_fixed():
    config = {"vllm_base_url": "https://example.test:8443/v1"}
    assert GoogleProvider().resolve({}, config).base_url == GOOGLE_URL


# --- timeout ---

@pytest.mark.parametrize("provider", [VllmProvider(), GoogleProvider()])
def test_timeout_defaults_to_600(provider):
    assert provider.resolve({}, {}).timeout == 600.0

@pytest.mark.parametrize("raw, expected", [(90, 90.0), ("45.5", 45.5), (12.5, 12.5)])
def test_timeout_override_is_coerced_to_float(raw, expected):
    for provider in (VllmProvider(), GoogleProvider()):
        assert provider.resolve({}, {"api_timeout": raw}).timeout == expected


# --- registry ---

def test_registry_exposes_builtin_providers():
    assert set(PROVIDERS) >= {"vllm", "google"}
    assert get_provider("vllm").id == "vllm"
    assert get_provider("google").id == "google"
    assert isinstance(get_provider("vllm"), VllmProvider)
    assert isinstance(get_provider("google"), GoogleProvider)
    assert isinstance(get_provider("vllm"), Provider)

def test_get_provider_unknown_lists_known_ids():
    with pytest.raises(ValueError) as exc:
        get_provider("openrouter")
    message = str(exc.value)
    assert "openrouter" in message
    assert "vllm" in message
    assert "google" in message


# --- make_client ---

def test_make_client_returns_client_and_auth():
    client, auth = make_client("vllm", {}, {})
    assert isinstance(client, OpenAI)
    assert isinstance(auth, ResolvedAuth)
    assert str(client.base_url).rstrip("/") == DEFAULT_VLLM_URL
    assert auth.base_url == DEFAULT_VLLM_URL
    assert client.api_key == "EMPTY"
    assert client.timeout == 600.0
    assert auth.wire_api == "completions"

def test_make_client_uses_config_overrides():
    config = {"vllm_base_url": "https://example.test:8443/v1",
              "vertex_api_key": "cfg-vertex", "api_timeout": "45"}
    client, auth = make_client("vllm", {}, config)
    assert str(client.base_url).rstrip("/") == "https://example.test:8443/v1"
    assert client.api_key == "cfg-vertex"
    assert client.timeout == 45.0

def test_make_client_applies_provider_headers():
    class Headerful(VllmProvider):
        id = "headerful"

        def resolve(self, agent_cfg, config):
            auth = super().resolve(agent_cfg, config)
            auth.headers = {"Editor-Version": "vscode/1.107.0"}
            return auth

    PROVIDERS["headerful"] = Headerful()
    try:
        client, auth = make_client("headerful", {}, {})
        assert client.default_headers["Editor-Version"] == "vscode/1.107.0"
        assert auth.headers == {"Editor-Version": "vscode/1.107.0"}
    finally:
        PROVIDERS.pop("headerful", None)

def test_make_client_unknown_provider():
    with pytest.raises(ValueError):
        make_client("nope", {}, {})


# --- auth store ---

@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / ".sdlc-factory" / "auth.json"
    monkeypatch.setattr(store, "AUTH_FILE", path)
    return path

def test_auth_file_default_location():
    from pathlib import Path
    assert store.AUTH_FILE == Path.home() / ".sdlc-factory" / "auth.json"

def test_read_auth_missing_file_returns_empty(auth_file):
    assert store.read_auth() == {}

def test_read_auth_invalid_json_returns_empty(auth_file):
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{not json")
    assert store.read_auth() == {}

def test_read_auth_non_dict_payload_returns_empty(auth_file):
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('["not", "a", "mapping"]')
    assert store.read_auth() == {}

def test_write_then_read_auth_roundtrip(auth_file):
    payload = {"github-copilot": {"refresh": "r", "access": "a", "expires": 123}}
    store.write_auth(payload)
    assert store.read_auth() == payload

def test_write_auth_creates_parent_dirs(auth_file):
    store.write_auth({"k": "v"})
    assert auth_file.exists()

def test_write_auth_file_mode_is_0600(auth_file):
    store.write_auth({"k": "v"})
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600

def test_write_auth_leaves_no_temp_file(auth_file):
    store.write_auth({"k": "v"})
    assert [p.name for p in auth_file.parent.iterdir()] == ["auth.json"]

def test_write_auth_overwrites_existing(auth_file):
    store.write_auth({"a": 1})
    store.write_auth({"b": 2})
    assert store.read_auth() == {"b": 2}