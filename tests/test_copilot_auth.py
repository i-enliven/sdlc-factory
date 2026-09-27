"""T4 — GitHub Copilot auth flow. Every HTTP call goes through the injected fake
seam; no test here opens a socket, and auth.json lives under ``tmp_path``."""

import io
import json
import stat
from typing import NamedTuple, Optional

import pytest

from sdlc_factory.providers import copilot
from sdlc_factory.providers import store
from sdlc_factory.providers.copilot import (
    CLIENT_ID,
    DEFAULT_DOMAIN,
    EDITOR_HEADERS,
    INDIVIDUAL_BASE_URL,
    SLOW_DOWN_TIMEOUT_MESSAGE,
    TIMEOUT_MESSAGE,
    CopilotAuthError,
    CopilotProvider,
    DeviceFlowError,
    Http,
    HttpError,
    HttpResponse,
    RateLimited,
    enable_model,
    enable_models,
    fetch_copilot_token,
    fetch_model_catalog,
    get_base_url,
    get_base_url_from_token,
    get_wire_api,
    login,
    normalize_domain,
    poll_for_github_token,
    resolve_http,
    start_device_flow,
)

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
MODELS_URL = f"{INDIVIDUAL_BASE_URL}/models"

COPILOT_TOKEN = "tid=abc;exp=1800000000;proxy-ep=proxy.individual.githubcopilot.com"
DEVICE = {"device_code": "dev-code", "user_code": "ABCD-1234",
          "verification_uri": "https://github.com/login/device", "interval": 3, "expires_in": 900}


# --- fake HTTP seam ---

class HttpCall(NamedTuple):
    kind: str
    url: str
    payload: Optional[dict]
    headers: dict


class FakeHttp:
    """Scripted replacement for the copilot HTTP seam.

    Each endpoint maps a URL to a response, or to a list of responses consumed in
    order (the last entry repeats once the list is exhausted). A callable entry is
    invoked with (url, payload, headers). Passing an exception instance raises it.
    """

    def __init__(self, get=None, post_form=None, post_json=None):
        self.calls = []
        self._script = {"get": get or {}, "post_form": post_form or {}, "post_json": post_json or {}}
        self._queue = {}

    def get(self, url, headers=None):
        return self._call("get", url, None, headers)

    def post_form(self, url, data, headers=None):
        return self._call("post_form", url, data, headers)

    def post_json(self, url, payload, headers=None):
        return self._call("post_json", url, payload, headers)

    def urls(self, kind=None):
        return [call.url for call in self.calls if kind is None or call.kind == kind]

    def of(self, kind):
        return [call for call in self.calls if call.kind == kind]

    def _call(self, kind, url, payload, headers):
        self.calls.append(HttpCall(kind, url, payload, dict(headers or {})))
        script = self._script[kind]
        if callable(script):
            response = script(url, payload, headers)
        else:
            if url not in script:
                raise AssertionError(f"unexpected {kind.upper()} {url}")
            key = (kind, url)
            if key not in self._queue:
                entries = script[url]
                self._queue[key] = list(entries) if isinstance(entries, list) else [entries]
            queue = self._queue[key]
            response = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else {})
            if callable(response):
                response = response(url, payload, headers)
        if isinstance(response, BaseException):
            raise response
        return response if response is not None else {}


class FakeClock:
    """Deterministic ``time.time``: pops values, repeats the last one."""

    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / ".sdlc-factory" / "auth.json"
    monkeypatch.setattr(store, "AUTH_FILE", path)
    return path


def catalog_entry(model_id, picker=True, policy="enabled", tool_calls=True):
    entry = {"id": model_id, "model_picker_enabled": picker, "policy": {"state": policy}}
    if tool_calls is not None:
        entry["capabilities"] = {"supports": {"tool_calls": tool_calls}}
    return entry


def device_http(**overrides):
    script = {"post_form": {DEVICE_CODE_URL: [{
        "device_code": DEVICE["device_code"], "user_code": DEVICE["user_code"],
        "verification_uri": DEVICE["verification_uri"], "interval": DEVICE["interval"],
        "expires_in": DEVICE["expires_in"],
    }]}}
    script.update(overrides)
    return FakeHttp(**script)


# --- HTTP seam plumbing ---

def test_resolve_http_defaults_to_the_real_seam():
    real = Http(copilot.http_get, copilot.http_post_form, copilot.http_post_json)
    assert resolve_http() == real
    assert resolve_http(None) == real
    assert resolve_http(real) is real

def test_resolve_http_accepts_dict_and_object_injections():
    fake = FakeHttp()
    assert resolve_http(fake).get.__self__ is fake
    from_dict = resolve_http({"post_json": fake.post_json})
    assert from_dict.post_json.__self__ is fake
    assert from_dict.get is copilot.http_get

def test_http_error_carries_status_and_body():
    error = HttpError(503, "service unavailable", "https://x.test/y")
    assert error.status == 503 and error.url == "https://x.test/y"
    assert "503" in str(error) and "https://x.test/y" in str(error)
    assert isinstance(RateLimited(), HttpError)


# --- the real seam: urllib glue, with urlopen patched (still no socket) ---

class FakeUrlopenResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

@pytest.fixture
def patched_urlopen(monkeypatch):
    """Patch `urlopen` itself: exercises the real request/response glue offline."""
    calls = []

    def install(body=b'{"ok": true}', status=200, error=None):
        def fake_urlopen(request, timeout=None):
            calls.append((request, timeout))
            if error is not None:
                raise error
            return FakeUrlopenResponse(body, status)
        monkeypatch.setattr(copilot.urllib.request, "urlopen", fake_urlopen)
        return calls

    return install

def http_error(code, body=b""):
    return copilot.urllib.error.HTTPError(url="https://copilot.test/z", code=code,
                                          msg="nope", hdrs={}, fp=io.BytesIO(body))

def test_http_get_sends_a_get_request(patched_urlopen):
    calls = patched_urlopen(body=b'{"token": "t", "expires_at": 1}')
    assert copilot.http_get("https://copilot.test/x", {"Accept": "application/json"}) == {
        "token": "t", "expires_at": 1}
    request, timeout = calls[0]
    assert request.full_url == "https://copilot.test/x"
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == "application/json"
    assert request.data is None
    assert timeout == copilot.HTTP_TIMEOUT_SECONDS

def test_http_post_form_urlencodes_and_sets_content_type(patched_urlopen):
    calls = patched_urlopen()
    copilot.http_post_form("https://github.test/login/device/code",
                           {"client_id": "Iv1.x", "scope": "read:user"})
    request, _ = calls[0]
    assert request.get_method() == "POST"
    assert request.data == b"client_id=Iv1.x&scope=read%3Auser"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"

def test_http_post_json_serialises_and_sets_content_type(patched_urlopen):
    calls = patched_urlopen()
    copilot.http_post_json("https://copilot.test/models/x/policy", {"state": "enabled"},
                           {"openai-intent": "chat-policy"})
    request, _ = calls[0]
    assert json.loads(request.data) == {"state": "enabled"}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Openai-intent") == "chat-policy"

def test_seam_returns_json_error_bodies_with_their_status(patched_urlopen):
    """authorization_pending arrives as JSON on a 400 — it must not raise."""
    calls = patched_urlopen(error=http_error(400, b'{"error": "authorization_pending"}'))
    response = copilot.http_post_form("https://github.test/login/oauth/access_token", {})
    assert dict(response) == {"error": "authorization_pending"}
    assert response.status == 400

def test_seam_raises_rate_limited_on_429(patched_urlopen):
    patched_urlopen(error=http_error(429, b'{"error": "slow down"}'))
    with pytest.raises(RateLimited):
        copilot.http_post_json("https://copilot.test/models/x/policy", {})

def test_seam_raises_http_error_on_unparseable_error_body(patched_urlopen):
    patched_urlopen(error=http_error(503, b"<html>unavailable</html>"))
    with pytest.raises(HttpError) as exc:
        copilot.http_get("https://copilot.test/models")
    assert exc.value.status == 503 and "unavailable" in exc.value.body

def test_seam_reports_transport_failures_as_status_zero(patched_urlopen):
    patched_urlopen(error=copilot.urllib.error.URLError("nodename nor servname provided"))
    with pytest.raises(HttpError) as exc:
        copilot.http_get("https://copilot.test/models")
    assert exc.value.status == 0

def test_seam_accepts_empty_bodies(patched_urlopen):
    patched_urlopen(body=b"", status=204)
    assert dict(copilot.http_post_json("https://copilot.test/models/x/policy", {})) == {}

def test_seam_rejects_non_json_and_non_object_success_bodies(patched_urlopen):
    for body in (b"<html>hi</html>", b'[1, 2, 3]', b'"a string"'):
        patched_urlopen(body=body)
        with pytest.raises(HttpError):
            copilot.http_get("https://copilot.test/models")


# --- step 1: device code request ---

def test_start_device_flow_request_and_result():
    fake = device_http()
    device = start_device_flow(http=fake)
    assert device == DEVICE
    call = fake.of("post_form")[0]
    assert call.url == DEVICE_CODE_URL
    assert call.payload == {"client_id": CLIENT_ID, "scope": "read:user"}
    assert call.headers["Accept"] == "application/json"
    assert call.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert call.headers["User-Agent"] == EDITOR_HEADERS["User-Agent"]

def test_start_device_flow_uses_enterprise_domain():
    fake = FakeHttp(post_form={"https://company.ghe.com/login/device/code": DEVICE})
    start_device_flow("company.ghe.com", http=fake)
    assert fake.urls("post_form") == ["https://company.ghe.com/login/device/code"]

def test_start_device_flow_allows_missing_interval():
    fake = device_http(post_form={DEVICE_CODE_URL: {
        "device_code": "d", "user_code": "U-1",
        "verification_uri": "https://github.com/login/device", "expires_in": 900}})
    assert start_device_flow(http=fake)["interval"] is None

@pytest.mark.parametrize("uri", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://github.com/login/device",
    "github.com/login/device",
    "/login/device",
    "http://[::1",
    "",
])
def test_start_device_flow_rejects_non_http_verification_uri(uri):
    fake = device_http(post_form={DEVICE_CODE_URL: {
        "device_code": "d", "user_code": "U-1", "verification_uri": uri, "expires_in": 900}})
    with pytest.raises(CopilotAuthError, match="Untrusted verification_uri"):
        start_device_flow(http=fake)

def test_start_device_flow_accepts_plain_http_uri():
    fake = device_http(post_form={DEVICE_CODE_URL: {
        "device_code": "d", "user_code": "U-1",
        "verification_uri": "http://localhost:8080/login/device", "expires_in": 900}})
    assert start_device_flow(http=fake)["verification_uri"] == "http://localhost:8080/login/device"

@pytest.mark.parametrize("broken", [
    {},
    {"user_code": "U-1", "verification_uri": "https://github.com/login/device", "expires_in": 900},
    {"device_code": "d", "verification_uri": "https://github.com/login/device", "expires_in": 900},
    {"device_code": "d", "user_code": "U-1", "expires_in": 900},
    {"device_code": "d", "user_code": "U-1", "verification_uri": "https://gh.test/d", "expires_in": "900"},
    {"device_code": "d", "user_code": "U-1", "verification_uri": "https://gh.test/d",
     "expires_in": 900, "interval": "5"},
])
def test_start_device_flow_rejects_invalid_fields(broken):
    fake = device_http(post_form={DEVICE_CODE_URL: broken})
    with pytest.raises(CopilotAuthError, match="Invalid device code response"):
        start_device_flow(http=fake)


# --- step 2: polling state machine ---

def test_poll_pending_then_slow_down_then_success():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [
        {"error": "authorization_pending"},
        {"error": "slow_down", "interval": 7},
        {"access_token": "gh-token"},
    ]})
    sleeps = []
    assert poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake, sleep=sleeps.append) == "gh-token"
    assert fake.urls("post_form") == [ACCESS_TOKEN_URL] * 3
    # one interval before the first poll, the pending interval, then the server's new interval
    assert sleeps == [3, 3, 7]
    for call in fake.of("post_form"):
        assert call.payload == {
            "client_id": CLIENT_ID,
            "device_code": "dev-code",
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        assert call.headers["Accept"] == "application/json"

def test_poll_defaults_to_rfc_8628_five_second_interval():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [{"access_token": "gh-token"}]})
    sleeps = []
    device = dict(DEVICE, interval=None)
    poll_for_github_token(DEFAULT_DOMAIN, device, http=fake, sleep=sleeps.append)
    assert sleeps == [5]

def test_poll_slow_down_without_server_interval_adds_five_seconds():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [
        {"error": "slow_down"},
        {"error": "slow_down"},
        {"access_token": "gh-token"},
    ]})
    sleeps = []
    poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake, sleep=sleeps.append)
    assert sleeps == [3, 8, 13]

def test_poll_never_sleeps_below_one_second():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [{"access_token": "gh-token"}]})
    sleeps = []
    poll_for_github_token(DEFAULT_DOMAIN, dict(DEVICE, interval=0), http=fake, sleep=sleeps.append)
    assert sleeps == [1]

def test_poll_raises_on_terminal_error():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [
        {"error": "expired_token", "error_description": "The device code expired"},
    ]})
    with pytest.raises(DeviceFlowError, match="Device flow failed: expired_token: The device code expired"):
        poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake, sleep=lambda s: None)

def test_poll_raises_on_unrecognised_response():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [{"whatever": True}]})
    with pytest.raises(DeviceFlowError, match="Invalid device token response"):
        poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake, sleep=lambda s: None)

def test_poll_times_out():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [{"error": "authorization_pending"}]})
    clock = FakeClock([0, 0, 0, 1000])
    with pytest.raises(DeviceFlowError, match=TIMEOUT_MESSAGE):
        poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake,
                              sleep=lambda s: None, now=clock)

def test_poll_timeout_after_slow_down_explains_clock_drift():
    fake = FakeHttp(post_form={ACCESS_TOKEN_URL: [
        {"error": "slow_down", "interval": 2},
        {"error": "authorization_pending"},
    ]})
    clock = FakeClock([0, 0, 0, 1000])
    with pytest.raises(DeviceFlowError, match=SLOW_DOWN_TIMEOUT_MESSAGE):
        poll_for_github_token(DEFAULT_DOMAIN, DEVICE, http=fake,
                              sleep=lambda s: None, now=clock)

def test_poll_uses_enterprise_domain():
    fake = FakeHttp(post_form={"https://company.ghe.com/login/oauth/access_token":
                               [{"access_token": "gh-token"}]})
    token = poll_for_github_token("company.ghe.com", DEVICE, http=fake, sleep=lambda s: None)
    assert token == "gh-token"


# --- step 3: Copilot proxy token ---

def copilot_token_http(token=COPILOT_TOKEN, expires_at=1800000000, url=COPILOT_TOKEN_URL):
    return FakeHttp(get={url: {"token": token, "expires_at": expires_at}})

def test_fetch_copilot_token_shape_and_expiry_buffer():
    fake = copilot_token_http()
    credential = fetch_copilot_token("gh-token", None, http=fake)
    assert credential == {
        "refresh": "gh-token",
        "access": COPILOT_TOKEN,
        "expires": 1800000000 * 1000 - 5 * 60 * 1000,
        "enterprise_url": None,
    }
    call = fake.of("get")[0]
    assert call.url == COPILOT_TOKEN_URL
    assert call.headers["Authorization"] == "Bearer gh-token"
    for header, value in EDITOR_HEADERS.items():
        assert call.headers[header] == value

def test_fetch_copilot_token_truncates_fractional_expiry():
    credential = fetch_copilot_token("gh", None, http=copilot_token_http(expires_at=1800000000.75))
    assert credential["expires"] == 1800000000750 - 300000

def test_fetch_copilot_token_enterprise():
    fake = copilot_token_http(url="https://api.company.ghe.com/copilot_internal/v2/token")
    credential = fetch_copilot_token("gh", "company.ghe.com", http=fake)
    assert fake.of("get")[0].url == "https://api.company.ghe.com/copilot_internal/v2/token"
    assert credential["enterprise_url"] == "company.ghe.com"

def test_fetch_copilot_token_treats_github_com_as_no_enterprise():
    credential = fetch_copilot_token("gh", DEFAULT_DOMAIN, http=copilot_token_http())
    assert credential["enterprise_url"] is None

@pytest.mark.parametrize("broken", [{}, {"token": "t"}, {"expires_at": 1},
                                    {"token": 5, "expires_at": "1800000000"}])
def test_fetch_copilot_token_rejects_invalid_response(broken):
    with pytest.raises(CopilotAuthError, match="Invalid Copilot token response"):
        fetch_copilot_token("gh", None, http=FakeHttp(get={COPILOT_TOKEN_URL: broken}))


# --- base URL derivation ---

@pytest.mark.parametrize("token, expected", [
    (COPILOT_TOKEN, INDIVIDUAL_BASE_URL),
    ("tid=x;proxy-ep=proxy.ghe.company.com;y=1", "https://api.ghe.company.com"),
    ("tid=x;proxy-ep=api.enterprise.company.com", "https://api.enterprise.company.com"),
    ("tid=x;exp=1800000000", None),
    ("", None),
    (None, None),
    ("proxy-ep=proxy.a.test/../evil", None),
    ("proxy-ep=proxy.a.test:443/x", None),
    ("proxy-ep=;", None),
])
def test_get_base_url_from_token(token, expected):
    assert get_base_url_from_token(token) == expected

@pytest.mark.parametrize("token, enterprise, expected", [
    (COPILOT_TOKEN, None, INDIVIDUAL_BASE_URL),
    (COPILOT_TOKEN, "company.ghe.com", INDIVIDUAL_BASE_URL),
    ("no-proxy-ep", "company.ghe.com", "https://copilot-api.company.ghe.com"),
    (None, None, INDIVIDUAL_BASE_URL),
    (None, "company.ghe.com", "https://copilot-api.company.ghe.com"),
])
def test_get_base_url_precedence(token, enterprise, expected):
    assert get_base_url(token, enterprise) == expected

@pytest.mark.parametrize("raw, expected", [
    ("company.ghe.com", "company.ghe.com"),
    ("  https://company.ghe.com  ", "company.ghe.com"),
    ("COMPANY.GHE.COM", "company.ghe.com"),
    ("company.ghe.com:8443", "company.ghe.com"),
    ("https://company.ghe.com/login/device", "company.ghe.com"),
    ("https://user:pass@company.ghe.com", "company.ghe.com"),
    ("ftp://company.ghe.com", "company.ghe.com"),
    ("", None),
    ("   ", None),
    (None, None),
    ("http://", None),
    ("//", None),
    ("..", None),
    ("not a domain!!", None),
    ("https://[::1]", None),
    ("https://[::1", None),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


# --- step 4: catalog ---

def test_catalog_filters_tool_calls_and_disabled_models():
    fake = FakeHttp(get={MODELS_URL: {"data": [
        catalog_entry("gpt-5"),
        catalog_entry("claude-sonnet-4.5"),
        catalog_entry("no-tools-model", tool_calls=False),
        catalog_entry("retired-model", policy="disabled"),
        catalog_entry("hidden-model", picker=False),
        catalog_entry("grok-4", policy="unconfigured"),
        {"id": "no-capabilities-block", "model_picker_enabled": True, "policy": {"state": "enabled"}},
        {"model_picker_enabled": True},
        "not-a-dict",
    ]}})
    catalog = fetch_model_catalog("cp-token", INDIVIDUAL_BASE_URL, http=fake)
    assert catalog["available_model_ids"] == ["gpt-5", "claude-sonnet-4.5", "grok-4",
                                              "no-capabilities-block"]
    assert catalog["policy_pending_ids"] == ["grok-4"]
    assert catalog["base_url"] == INDIVIDUAL_BASE_URL
    call = fake.of("get")[0]
    assert call.url == MODELS_URL
    assert call.headers["Authorization"] == "Bearer cp-token"
    assert call.headers["X-GitHub-Api-Version"] == "2026-06-01"
    for header, value in EDITOR_HEADERS.items():
        assert call.headers[header] == value

def test_catalog_derives_base_url_from_token_when_omitted():
    fake = FakeHttp(get={f"{INDIVIDUAL_BASE_URL}/models": {"data": [catalog_entry("gpt-5")]}})
    catalog = fetch_model_catalog(COPILOT_TOKEN, http=fake)
    assert fake.of("get")[0].url == f"{INDIVIDUAL_BASE_URL}/models"
    assert catalog["available_model_ids"] == ["gpt-5"]

def test_catalog_picker_fallback_only_on_individual_endpoint():
    pickerless = {"data": [
        catalog_entry("gpt-5", picker=False, policy="enabled"),
        catalog_entry("grok-4", picker=False, policy="unconfigured"),
        catalog_entry("gemini-2.5-pro", picker=False, policy="disabled"),
    ]}
    individual = fetch_model_catalog("cp", INDIVIDUAL_BASE_URL, http=FakeHttp(get={MODELS_URL: pickerless}))
    assert individual["available_model_ids"] == ["gpt-5"]
    assert individual["policy_pending_ids"] == ["grok-4"]

    enterprise_url = "https://api.company.ghe.com"
    enterprise = fetch_model_catalog("cp", enterprise_url,
                                     http=FakeHttp(get={f"{enterprise_url}/models": pickerless}))
    assert enterprise["available_model_ids"] == []
    assert enterprise["policy_pending_ids"] == []

def test_catalog_keeps_picker_ids_when_any_picker_flag_is_set():
    """The fallback must not kick in when only *some* models are picker-hidden."""
    fake = FakeHttp(get={MODELS_URL: {"data": [
        catalog_entry("gpt-5"),
        catalog_entry("hidden-but-enabled", picker=False, policy="enabled"),
    ]}})
    assert fetch_model_catalog("cp", INDIVIDUAL_BASE_URL, http=fake)["available_model_ids"] == ["gpt-5"]

def test_catalog_rejects_malformed_response():
    with pytest.raises(CopilotAuthError, match="Invalid Copilot models response"):
        fetch_model_catalog("cp", INDIVIDUAL_BASE_URL, http=FakeHttp(get={MODELS_URL: {"data": {}}}))
    with pytest.raises(CopilotAuthError, match="Invalid Copilot models response"):
        fetch_model_catalog("cp", INDIVIDUAL_BASE_URL, http=FakeHttp(get={MODELS_URL: {}}))


# --- step 5: policy enable ---

def test_enable_model_request():
    fake = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy": {"state": "enabled"}})
    assert enable_model("cp-token", "grok-4", INDIVIDUAL_BASE_URL, http=fake) is True
    call = fake.of("post_json")[0]
    assert call.url == f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy"
    assert call.payload == {"state": "enabled"}
    assert call.headers["openai-intent"] == "chat-policy"
    assert call.headers["Authorization"] == "Bearer cp-token"
    assert call.headers["Editor-Version"] == EDITOR_HEADERS["Editor-Version"]

def test_enable_model_derives_base_url_from_token():
    fake = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/gpt-5/policy": {}})
    assert enable_model(COPILOT_TOKEN, "gpt-5", http=fake) is True

def test_enable_model_is_best_effort_on_server_errors():
    fake = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy":
                               HttpError(500, "boom", "https://x.test")})
    assert enable_model("cp", "grok-4", INDIVIDUAL_BASE_URL, http=fake) is False

def test_enable_model_propagates_transport_failures():
    """Status 0 means "no response at all" — not something to skip past."""
    fake = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy":
                               HttpError(0, "connection reset", "https://x.test")})
    with pytest.raises(HttpError):
        enable_model("cp", "grok-4", INDIVIDUAL_BASE_URL, http=fake)

def test_enable_model_reports_non_success_status():
    success = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy":
                                  HttpResponse({"ok": True}, status=204)})
    assert enable_model("cp", "grok-4", INDIVIDUAL_BASE_URL, http=success) is True
    refusal = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy":
                                  HttpResponse({"error": "nope"}, status=403)})
    assert enable_model("cp", "grok-4", INDIVIDUAL_BASE_URL, http=refusal) is False

def test_enable_model_raises_rate_limit():
    fake = FakeHttp(post_json={f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy":
                               HttpResponse({"error": "slow down"}, status=429)})
    with pytest.raises(RateLimited):
        enable_model("cp", "grok-4", INDIVIDUAL_BASE_URL, http=fake)

def test_enable_models_stops_batch_on_raised_429():
    def responder(url, payload, headers):
        if "grok-4" in url:
            raise RateLimited(429, "rate limited", url)
        return {"state": "enabled"}

    fake = FakeHttp(post_json=responder)
    enabled = enable_models("cp", ["gpt-5", "grok-4", "gemini-2.5-pro"], INDIVIDUAL_BASE_URL, http=fake)
    assert enabled == ["gpt-5"]
    assert fake.urls("post_json") == [f"{INDIVIDUAL_BASE_URL}/models/gpt-5/policy",
                                      f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy"]

def test_enable_models_stops_batch_on_429_status_body():
    def responder(url, payload, headers):
        if "grok-4" in url:
            return HttpResponse({"error": "slow down"}, status=429)
        return {"state": "enabled"}

    fake = FakeHttp(post_json=responder)
    assert enable_models("cp", ["gpt-5", "grok-4", "gemini-2.5-pro"], INDIVIDUAL_BASE_URL, http=fake) == ["gpt-5"]
    assert len(fake.urls("post_json")) == 2

def test_enable_models_continues_past_a_failed_model():
    """A policy call that comes back non-success is skipped; the batch goes on."""
    def responder(url, payload, headers):
        if "grok-4" in url:
            return HttpResponse({"error": "forbidden"}, status=403)
        return {"state": "enabled"}

    fake = FakeHttp(post_json=responder)
    enabled = enable_models("cp", ["gpt-5", "grok-4", "gemini-2.5-pro"], INDIVIDUAL_BASE_URL, http=fake)
    assert enabled == ["gpt-5", "gemini-2.5-pro"]
    assert len(fake.urls("post_json")) == 3

def test_enable_models_stops_batch_on_transport_error():
    """No response at all (transport failure) ends the batch, like a 429 does."""
    def responder(url, payload, headers):
        if "grok-4" in url:
            raise HttpError(0, "connection reset", url)
        return {"state": "enabled"}

    fake = FakeHttp(post_json=responder)
    assert enable_models("cp", ["gpt-5", "grok-4", "gemini-2.5-pro"], INDIVIDUAL_BASE_URL, http=fake) == ["gpt-5"]
    assert len(fake.urls("post_json")) == 2

def test_enable_models_noop_on_empty_batch():
    fake = FakeHttp()
    assert enable_models("cp", [], INDIVIDUAL_BASE_URL, http=fake) == []
    assert fake.calls == []


# --- wire API mapping ---

@pytest.mark.parametrize("model_id, api", [
    ("gpt-5", "responses"),
    ("gpt-5-codex", "responses"),
    ("grok-4", "responses"),
    ("oswe-vscode-cli", "responses"),
    ("mai-1-chat", "responses"),
    ("claude-sonnet-4.5", "unsupported"),
    ("claude-3.7-sonnet", "unsupported"),
    ("Claude-Opus-4", "unsupported"),
    ("gemini-2.5-pro", "completions"),
    ("llama-3.1-70b", "completions"),
    ("phikt-8b", "completions"),
])
def test_get_wire_api(model_id, api):
    assert get_wire_api(model_id) == api


# --- login orchestration + persistence ---

LOGIN_CATALOG = {"data": [
    catalog_entry("gpt-5"),
    catalog_entry("gpt-5-codex"),
    catalog_entry("claude-sonnet-4.5"),
    catalog_entry("gemini-2.5-pro"),
    catalog_entry("oswe-vscode-cli"),
    catalog_entry("mai-1-chat"),
    catalog_entry("grok-4", policy="unconfigured"),
    catalog_entry("no-tools-model", tool_calls=False),
    catalog_entry("retired-model", policy="disabled"),
]}

def login_http(catalog=LOGIN_CATALOG, copilot_token=COPILOT_TOKEN, enterprise=None):
    host = enterprise or DEFAULT_DOMAIN
    return FakeHttp(
        post_form={
            f"https://{host}/login/device/code": [{
                "device_code": "dev-code", "user_code": "ABCD-1234",
                "verification_uri": f"https://{host}/login/device", "interval": 1, "expires_in": 900,
            }],
            f"https://{host}/login/oauth/access_token": [
                {"error": "authorization_pending"},
                {"access_token": "gh-token"},
            ],
        },
        get={
            f"https://api.{host}/copilot_internal/v2/token": {"token": copilot_token,
                                                              "expires_at": 1800000000},
            f"{get_base_url(copilot_token, enterprise)}/models": catalog,
        },
        post_json=lambda url, payload, headers: {"state": "enabled"},
    )

def test_login_persists_merged_auth_json(auth_file):
    store.write_auth({"vllm": {"api_key": "keep-me"}, "google": {"api_key": "also-keep"}})
    fake = login_http()
    notified = []
    credential = login(prompt=lambda question: "", http=fake, sleep=lambda s: None, notify=notified.append)

    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    assert on_disk["vllm"] == {"api_key": "keep-me"}
    assert on_disk["google"] == {"api_key": "also-keep"}
    entry = on_disk["github-copilot"]
    assert entry == credential
    assert entry["refresh"] == "gh-token"
    assert entry["access"] == COPILOT_TOKEN
    assert entry["expires"] == 1800000000 * 1000 - 300000
    assert entry["enterprise_url"] is None
    assert entry["available_model_ids"] == [
        "claude-sonnet-4.5", "gemini-2.5-pro", "gpt-5", "gpt-5-codex",
        "grok-4", "mai-1-chat", "oswe-vscode-cli",
    ]
    assert entry["models"] == {
        "claude-sonnet-4.5": {"api": "unsupported"},
        "gemini-2.5-pro": {"api": "completions"},
        "gpt-5": {"api": "responses"},
        "gpt-5-codex": {"api": "responses"},
        "grok-4": {"api": "responses"},
        "mai-1-chat": {"api": "responses"},
        "oswe-vscode-cli": {"api": "responses"},
    }
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
    assert notified[0]["user_code"] == "ABCD-1234"
    # the unconfigured model got a policy call; the disabled/toolless ones did not
    policy_calls = fake.urls("post_json")
    assert policy_calls == [f"{INDIVIDUAL_BASE_URL}/models/grok-4/policy"]

def test_login_without_notify_and_prompt(auth_file):
    login(http=login_http(), sleep=lambda s: None)
    assert copilot.load_credential()["refresh"] == "gh-token"

def test_login_enterprise_domain_from_prompt(auth_file):
    fake = login_http(enterprise="company.ghe.com")
    credential = login(prompt=lambda question: "https://company.ghe.com", http=fake, sleep=lambda s: None)
    assert credential["enterprise_url"] == "company.ghe.com"
    assert fake.urls("post_form") == [
        "https://company.ghe.com/login/device/code",
        "https://company.ghe.com/login/oauth/access_token",
        "https://company.ghe.com/login/oauth/access_token",
    ]
    assert fake.urls("get")[0] == "https://api.company.ghe.com/copilot_internal/v2/token"
    assert store.read_auth()["github-copilot"] == credential

def test_login_enterprise_without_proxy_ep_uses_copilot_api_host(auth_file):
    fake = login_http(copilot_token="tid=x;exp=1800000000", enterprise="company.ghe.com")
    credential = login(enterprise="company.ghe.com", http=fake, sleep=lambda s: None)
    assert fake.urls("get")[1] == "https://copilot-api.company.ghe.com/models"
    assert credential["access"] == "tid=x;exp=1800000000"

def test_login_explicit_enterprise_skips_the_prompt(auth_file):
    def explode(question):
        raise AssertionError("prompt must not be asked when --enterprise is given")

    login(enterprise="company.ghe.com", prompt=explode, http=login_http(enterprise="company.ghe.com"),
          sleep=lambda s: None)
    assert copilot.load_credential()["enterprise_url"] == "company.ghe.com"

@pytest.mark.parametrize("answer", ["not a domain!!", "http://", "..", "https://[::1]"])
def test_login_rejects_invalid_enterprise(auth_file, answer):
    fake = FakeHttp()  # any request would raise AssertionError from the fake
    with pytest.raises(CopilotAuthError, match="Invalid GitHub Enterprise URL/domain"):
        login(prompt=lambda question: answer, http=fake, sleep=lambda s: None)
    assert fake.calls == []

def test_login_propagates_device_flow_failure(auth_file):
    fake = login_http()
    fake._script["post_form"][f"https://github.com/login/oauth/access_token"] = [
        {"error": "access_denied", "error_description": "The user denied the request"}
    ]
    with pytest.raises(DeviceFlowError, match="access_denied"):
        login(prompt=lambda question: "", http=fake, sleep=lambda s: None)
    assert store.read_auth() == {}

def test_login_replaces_previous_copilot_entry(auth_file):
    store.write_auth({"github-copilot": {"refresh": "stale", "access": "stale"}})
    login(http=login_http(), sleep=lambda s: None, prompt=lambda q: "")
    assert store.read_auth()["github-copilot"]["refresh"] == "gh-token"

def test_load_credential_returns_none_when_absent(auth_file):
    assert store.read_auth() == {}
    assert copilot.load_credential() is None
def test_load_credential_ignores_non_dict_entry(auth_file):
    store.write_auth({"github-copilot": "nope"})
    assert copilot.load_credential() is None


# --- provider class ---

def test_copilot_provider_id_and_deferred_resolve():
    provider = CopilotProvider()
    assert provider.id == "github-copilot"
    with pytest.raises(NotImplementedError, match="T6"):
        provider.resolve({}, {})

def test_copilot_provider_login_delegates(auth_file):
    credential = CopilotProvider().login(http=login_http(), sleep=lambda s: None)
    assert credential["available_model_ids"]
    assert copilot.load_credential() == credential