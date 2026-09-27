"""GitHub Copilot provider — AUTH FLOW ONLY (T4).

Port of pi's verified TypeScript implementation
(``packages/ai/src/auth/oauth/github-copilot.ts`` + ``.../device-code.ts`` +
``.../api/github-copilot-headers.ts``), see PLAN.md §2.3.

Implemented here: the OAuth device-code flow, the Copilot proxy-token exchange
(``copilot_internal/v2/token``), the model catalog fetch with policy auto-enable,
base-URL derivation from the token itself, and ``auth.json`` persistence.

Not implemented here: runtime ``resolve()`` (T6) and the CLI wrappers (T5).

All network traffic goes through one injectable seam (``http=...``); every flow
function takes it as its last argument, so tests drive the whole login with fake
responses and no socket is ever opened.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, NamedTuple, Optional

from . import store
from .base import Provider, ResolvedAuth

PROVIDER_ID = "github-copilot"

# Public OAuth app client id of the VS Code Copilot Chat extension. Not a secret:
# it identifies the editor, the secret is the user's browser-side authorization.
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_FLOW_SCOPE = "read:user"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Copilot gates on VS Code identity headers. One constant block on purpose
# (PLAN.md §5 "header drift") — bump the versions here and nowhere else.
EDITOR_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}
COPILOT_API_VERSION = "2026-06-01"

DEFAULT_DOMAIN = "github.com"
INDIVIDUAL_BASE_URL = "https://api.individual.githubcopilot.com"

# RFC 8628 §3.2: a missing `interval` means 5 seconds. §3.5: `slow_down` means
# add 5 seconds when the server does not report the new minimum itself.
DEFAULT_POLL_INTERVAL_SECONDS = 5
MIN_POLL_INTERVAL_SECONDS = 1
SLOW_DOWN_INTERVAL_INCREMENT_SECONDS = 5
TIMEOUT_MESSAGE = "Device flow timed out"
SLOW_DOWN_TIMEOUT_MESSAGE = (
    "Device flow timed out after one or more slow_down responses. This is often "
    "caused by clock drift in WSL or VM environments. Please sync or restart the "
    "VM clock and try again."
)

# Copilot's own expiry is ~10 minutes; refresh 5 minutes early.
EXPIRY_BUFFER_MS = 5 * 60 * 1000
HTTP_TIMEOUT_SECONDS = 30.0

# Wire-API routing (PLAN.md §2.3). Copilot serves different models over
# different APIs; this codebase speaks completions today, Responses arrives in
# T7, and Anthropic-Messages (Claude) stays out of scope (§5).
RESPONSES_API_PREFIXES = ("gpt-", "grok-", "oswe", "mai-")
UNSUPPORTED_API_PREFIXES = ("claude",)

ENTERPRISE_PROMPT = "GitHub Enterprise URL/domain (blank for github.com)"


class CopilotAuthError(Exception):
    """The Copilot auth flow could not be completed."""


class HttpError(CopilotAuthError):
    """A non-success HTTP response with no usable JSON body, or a transport error."""

    def __init__(self, status: int, body: str = "", url: str = ""):
        super().__init__(f"HTTP {status} from {url}: {str(body)[:200]}")
        self.status = status
        self.body = body
        self.url = url


class RateLimited(HttpError):
    """HTTP 429. Raised rather than swallowed: it ends a policy-enable batch."""

    def __init__(self, status: int = 429, body: str = "", url: str = ""):
        super().__init__(status, body, url)


class DeviceFlowError(CopilotAuthError):
    """The device-code flow was rejected, or gave up before authorization."""


class HttpResponse(dict):
    """Parsed JSON body plus its HTTP status (fakes may return plain dicts)."""

    def __init__(self, data: Optional[dict] = None, status: int = 200):
        super().__init__(data or {})
        self.status = status


# --- HTTP seam (stdlib only; no new dependencies) ---

def _json_object(raw: str) -> Optional[dict]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _request(url: str, method: str = "GET", headers: Optional[dict] = None,
             body: Optional[bytes] = None) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read().decode("utf-8", "replace")
        # Device-flow errors (authorization_pending & friends) arrive as JSON on
        # 4xx statuses, so a parseable body is returned, not raised.
        parsed = _json_object(raw)
        if status == 429:
            raise RateLimited(status, raw, url) from error
        if parsed is not None:
            return HttpResponse(parsed, status=status)
        raise HttpError(status, raw, url) from error
    except urllib.error.URLError as error:
        raise HttpError(0, str(error.reason), url) from error
    if not raw.strip():
        return HttpResponse({}, status=status)
    parsed = _json_object(raw)
    if parsed is None:
        raise HttpError(status, raw, url)
    return HttpResponse(parsed, status=status)


def http_get(url: str, headers: Optional[dict] = None) -> dict:
    """GET ``url`` and return its JSON object body."""
    return _request(url, "GET", headers)


def http_post_form(url: str, data: dict, headers: Optional[dict] = None) -> dict:
    """POST ``data`` as ``application/x-www-form-urlencoded``, return JSON body."""
    merged = {"Content-Type": "application/x-www-form-urlencoded"}
    merged.update(headers or {})
    return _request(url, "POST", merged, urllib.parse.urlencode(data).encode("utf-8"))


def http_post_json(url: str, payload: dict, headers: Optional[dict] = None) -> dict:
    """POST ``payload`` as ``application/json``, return JSON body."""
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return _request(url, "POST", merged, json.dumps(payload).encode("utf-8"))


class Http(NamedTuple):
    """The three-call HTTP seam every flow function is written against."""

    get: Callable[[str, Optional[dict]], dict] = http_get
    post_form: Callable[[str, dict, Optional[dict]], dict] = http_post_form
    post_json: Callable[[str, dict, Optional[dict]], dict] = http_post_json


DEFAULT_HTTP = Http()


def resolve_http(http: Any = None) -> Http:
    """Normalize an injected seam: ``None``, a mapping, or any object with the
    ``get`` / ``post_form`` / ``post_json`` attributes (namedtuple, stub, ...).

    A partial injection falls back to the real implementation for whichever call
    it omits — convenient, and the reason a test fake that forgets an endpoint
    fails loudly (the fakes here raise on unexpected URLs) instead of quietly
    pretending.
    """
    if http is None:
        return DEFAULT_HTTP
    if isinstance(http, Http):
        return http
    if isinstance(http, dict):
        return Http(
            get=http.get("get", http_get),
            post_form=http.get("post_form", http_post_form),
            post_json=http.get("post_json", http_post_json),
        )
    return Http(
        get=getattr(http, "get", http_get),
        post_form=getattr(http, "post_form", http_post_form),
        post_json=getattr(http, "post_json", http_post_json),
    )


# --- domain and base URL ---

_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-._~]*[A-Za-z0-9])?$")


def normalize_domain(input: Optional[str]) -> Optional[str]:
    """``company.ghe.com`` or ``https://company.ghe.com`` -> ``company.ghe.com``.

    Blank/absent input yields ``None`` (meaning "github.com"); anything that is
    not a plain hostname yields ``None`` too, so callers can tell "unset" from
    "invalid". Only the host survives (scheme, port, path and userinfo dropped),
    which keeps user input out of the URL structure we build from it.
    """
    trimmed = (input or "").strip()
    if not trimmed:
        return None
    candidate = trimmed if "://" in trimmed else f"https://{trimmed}"
    try:
        hostname = urllib.parse.urlsplit(candidate).hostname
    except ValueError:
        return None
    if not hostname or not _HOSTNAME_PATTERN.match(hostname):
        return None
    return hostname


_PROXY_EP_PATTERN = re.compile(r"proxy-ep=([^;]+)")
_PROXY_HOST_PREFIX = re.compile(r"^proxy\.")


def get_base_url_from_token(token: Optional[str]) -> Optional[str]:
    """Derive the API base URL from a Copilot token's ``proxy-ep`` claim.

    Token format: ``tid=...;exp=...;proxy-ep=proxy.individual.githubcopilot.com``.
    The proxy host is rewritten ``proxy.`` -> ``api.`` (pi regression #6768: the
    auth-resolved URL is authoritative, not the static catalog URL).
    """
    if not isinstance(token, str):
        return None
    match = _PROXY_EP_PATTERN.search(token)
    if not match:
        return None
    host = match.group(1).strip()
    if not host or "/" in host or "\\" in host or "@" in host or any(c.isspace() for c in host):
        return None
    return f"https://{_PROXY_HOST_PREFIX.sub('api.', host, count=1)}"


def get_base_url(token: Optional[str] = None, enterprise_domain: Optional[str] = None) -> str:
    """Token ``proxy-ep`` first, then the enterprise host, then the individual endpoint."""
    from_token = get_base_url_from_token(token)
    if from_token:
        return from_token
    if enterprise_domain:
        return f"https://copilot-api.{enterprise_domain}"
    return INDIVIDUAL_BASE_URL


def get_wire_api(model_id: str) -> str:
    """Which wire API Copilot serves ``model_id`` on: see the prefix constants."""
    lowered = (model_id or "").lower()
    if lowered.startswith(UNSUPPORTED_API_PREFIXES):
        return "unsupported"
    if lowered.startswith(RESPONSES_API_PREFIXES):
        return "responses"
    return "completions"


# --- step 1: device code ---

def _form_headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": EDITOR_HEADERS["User-Agent"],
    }


def _trusted_verification_uri(uri: str) -> str:
    """The URI gets opened in a browser; only http(s) URLs are acceptable."""
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CopilotAuthError("Untrusted verification_uri in device code response")
    return uri


def start_device_flow(domain: str = DEFAULT_DOMAIN, http: Any = None) -> dict:
    """Request a device code. Returns device_code/user_code/verification_uri/
    interval/expires_in."""
    seam = resolve_http(http)
    response = seam.post_form(
        f"https://{domain}/login/device/code",
        {"client_id": CLIENT_ID, "scope": DEVICE_FLOW_SCOPE},
        _form_headers(),
    )
    device_code = response.get("device_code")
    user_code = response.get("user_code")
    verification_uri = response.get("verification_uri")
    interval = response.get("interval")
    expires_in = response.get("expires_in")
    if (
        not isinstance(device_code, str)
        or not isinstance(user_code, str)
        or not isinstance(verification_uri, str)
        or (interval is not None and not isinstance(interval, (int, float)))
        or not isinstance(expires_in, (int, float))
    ):
        raise CopilotAuthError("Invalid device code response fields")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": _trusted_verification_uri(verification_uri),
        "interval": interval,
        "expires_in": expires_in,
    }


# --- step 2: poll for the GitHub access token ---

def _poll_interval_seconds(value: Any, default: float = DEFAULT_POLL_INTERVAL_SECONDS) -> int:
    """Server value wins when it is a number (even 0, which we clamp to 1s); a
    missing/non-numeric one falls back to ``default`` (RFC 8628: 5s)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(MIN_POLL_INTERVAL_SECONDS, int(value))
    return max(MIN_POLL_INTERVAL_SECONDS, int(default))


def poll_for_github_token(domain: str, device: dict, http: Any = None,
                          sleep: Optional[Callable[[float], Any]] = None,
                          now: Optional[Callable[[], float]] = None) -> str:
    """Poll ``login/oauth/access_token`` until the user authorizes.

    ``sleep`` and ``now`` are injectable so tests can run the whole state machine
    (pending -> slow_down -> success, and the expiry deadline) without waiting.
    """
    seam = resolve_http(http)
    sleep_fn = sleep or time.sleep
    now_fn = now or time.time
    interval = _poll_interval_seconds(device.get("interval"))
    expires_in = device.get("expires_in")
    deadline = now_fn() + (expires_in if isinstance(expires_in, (int, float)) and expires_in > 0
                           else float("inf"))
    url = f"https://{domain}/login/oauth/access_token"
    slow_down_responses = 0

    remaining = deadline - now_fn()
    if remaining > 0:
        sleep_fn(min(interval, remaining))

    while now_fn() < deadline:
        response = seam.post_form(
            url,
            {
                "client_id": CLIENT_ID,
                "device_code": device.get("device_code"),
                "grant_type": DEVICE_CODE_GRANT_TYPE,
            },
            _form_headers(),
        )
        access_token = response.get("access_token")
        if isinstance(access_token, str) and access_token:
            return access_token

        error = response.get("error")
        if not isinstance(error, str):
            raise DeviceFlowError("Invalid device token response")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            slow_down_responses += 1
            interval = _poll_interval_seconds(
                response.get("interval"), interval + SLOW_DOWN_INTERVAL_INCREMENT_SECONDS
            )
        else:
            description = response.get("error_description")
            suffix = f": {description}" if isinstance(description, str) and description else ""
            raise DeviceFlowError(f"Device flow failed: {error}{suffix}")

        remaining = deadline - now_fn()
        if remaining <= 0:
            break
        sleep_fn(min(interval, remaining))

    raise DeviceFlowError(SLOW_DOWN_TIMEOUT_MESSAGE if slow_down_responses else TIMEOUT_MESSAGE)


# --- step 3: exchange the GitHub token for a Copilot proxy token ---

def fetch_copilot_token(gh_token: str, enterprise_domain: Optional[str] = None,
                        http: Any = None) -> dict:
    """``GET https://api.{domain}/copilot_internal/v2/token`` -> credential dict.

    ``expires`` is epoch millis with a 5-minute early-refresh buffer subtracted;
    ``refresh`` keeps the GitHub token, which is what later refreshes replay.
    """
    seam = resolve_http(http)
    domain = enterprise_domain or DEFAULT_DOMAIN
    headers = {"Accept": "application/json", "Authorization": f"Bearer {gh_token}", **EDITOR_HEADERS}
    response = seam.get(f"https://api.{domain}/copilot_internal/v2/token", headers)
    token = response.get("token")
    expires_at = response.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, (int, float)) \
            or isinstance(expires_at, bool):
        raise CopilotAuthError("Invalid Copilot token response fields")
    return {
        "refresh": gh_token,
        "access": token,
        "expires": int(expires_at * 1000) - EXPIRY_BUFFER_MS,
        "enterprise_url": enterprise_domain if enterprise_domain and enterprise_domain != DEFAULT_DOMAIN else None,
    }


# --- step 4: model catalog ---

def _model_fields(item: dict) -> Optional[dict]:
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    capabilities = item.get("capabilities")
    supports = capabilities.get("supports") if isinstance(capabilities, dict) else None
    tool_calls = supports.get("tool_calls") if isinstance(supports, dict) else None
    if tool_calls is False:
        return None
    policy = item.get("policy")
    policy_state = policy.get("state") if isinstance(policy, dict) else None
    return {
        "id": model_id,
        "picker_enabled": item.get("model_picker_enabled") is True,
        "policy_state": policy_state,
    }


def fetch_model_catalog(copilot_token: str, base_url: Optional[str] = None,
                        http: Any = None) -> dict:
    """Fetch and filter ``{base}/models``.

    Keeps only tool-call-capable models. Available = picker-enabled and not
    policy-disabled; if the picker is off for *every* model on the individual
    endpoint (some accounts report that despite explicit ``enabled`` policies),
    fall back to policy-enabled ids — and only on that endpoint, so other
    account types keep strict picker semantics. Pending = policy ``unconfigured``
    models, which Claude/Grok require enabling before use.
    """
    seam = resolve_http(http)
    resolved_base = (base_url or get_base_url(copilot_token)).rstrip("/")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {copilot_token}",
        **EDITOR_HEADERS,
        "X-GitHub-Api-Version": COPILOT_API_VERSION,
    }
    response = seam.get(f"{resolved_base}/models", headers)
    data = response.get("data")
    if not isinstance(data, list):
        raise CopilotAuthError("Invalid Copilot models response")

    account_models = [model for model in (_model_fields(item) for item in data
                                          if isinstance(item, dict)) if model]
    picker_ids = [m["id"] for m in account_models if m["picker_enabled"] and m["policy_state"] != "disabled"]
    use_policy_fallback = not picker_ids and resolved_base == INDIVIDUAL_BASE_URL
    if use_policy_fallback:
        available_ids = [m["id"] for m in account_models if m["policy_state"] == "enabled"]
    else:
        available_ids = picker_ids
    pending_ids = [m["id"] for m in account_models
                   if m["policy_state"] == "unconfigured" and (m["picker_enabled"] or use_policy_fallback)]
    return {
        "base_url": resolved_base,
        "available_model_ids": available_ids,
        "policy_pending_ids": pending_ids,
        "models": {m["id"]: {"api": get_wire_api(m["id"])} for m in account_models},
    }


# --- step 5: policy enable (best effort) ---

def enable_model(copilot_token: str, model_id: str, base_url: Optional[str] = None,
                 http: Any = None) -> bool:
    """``POST {base}/models/{id}/policy {"state": "enabled"}``.

    Best effort: a refusal (any non-success status, or an error response we can
    parse) returns ``False``. Rate limiting raises :class:`RateLimited` and so
    does a transport failure (status 0, no response at all) — both are the
    signal to stop the batch rather than keep hammering the endpoint.
    """
    seam = resolve_http(http)
    resolved_base = (base_url or get_base_url(copilot_token)).rstrip("/")
    url = f"{resolved_base}/models/{urllib.parse.quote(model_id, safe='')}/policy"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {copilot_token}",
        **EDITOR_HEADERS,
        "openai-intent": "chat-policy",
        "x-interaction-type": "chat-policy",
    }
    try:
        response = seam.post_json(url, {"state": "enabled"}, headers)
    except RateLimited:
        raise
    except HttpError as error:
        if error.status == 0:
            raise
        return False
    status = getattr(response, "status", 200)
    if status == 429:
        raise RateLimited(429, "", url)
    return 200 <= status < 300


def enable_models(copilot_token: str, model_ids, base_url: Optional[str] = None,
                  http: Any = None) -> list:
    """Enable a batch of models; rate limiting or a transport error stops it."""
    enabled = []
    for model_id in model_ids:
        try:
            if enable_model(copilot_token, model_id, base_url, http):
                enabled.append(model_id)
        except CopilotAuthError:
            break
    return enabled


# --- persistence + orchestration ---

def save_credential(credential: dict) -> None:
    """Merge our entry into auth.json; other providers' entries are preserved."""
    data = store.read_auth()
    data[PROVIDER_ID] = credential
    store.write_auth(data)


def load_credential() -> Optional[dict]:
    """The stored ``github-copilot`` credential, or ``None`` when not logged in."""
    credential = store.read_auth().get(PROVIDER_ID)
    return credential if isinstance(credential, dict) else None


def login(prompt: Optional[Callable[[str], str]] = None, http: Any = None,
          sleep: Optional[Callable[[float], Any]] = None,
          enterprise: Optional[str] = None,
          notify: Optional[Callable[[dict], Any]] = None,
          now: Optional[Callable[[], float]] = None) -> dict:
    """Run the whole login and persist the credential. Returns the credential.

    ``prompt`` asks for the optional enterprise domain (skipped when
    ``enterprise`` is given, e.g. ``copilot-login --enterprise``); ``notify``
    receives the device code so the CLI can show the user code and URL.
    """
    raw_enterprise = enterprise if enterprise is not None else (prompt(ENTERPRISE_PROMPT) if prompt else "")
    enterprise_domain = None
    trimmed = (raw_enterprise or "").strip()
    if trimmed:
        enterprise_domain = normalize_domain(trimmed)
        if not enterprise_domain:
            raise CopilotAuthError(f"Invalid GitHub Enterprise URL/domain: {trimmed}")

    domain = enterprise_domain or DEFAULT_DOMAIN
    device = start_device_flow(domain, http)
    if notify:
        notify(device)

    gh_token = poll_for_github_token(domain, device, http, sleep, now)
    credential = fetch_copilot_token(gh_token, enterprise_domain, http)
    base_url = get_base_url(credential["access"], enterprise_domain)
    catalog = fetch_model_catalog(credential["access"], base_url, http)
    enabled_ids = enable_models(credential["access"], catalog["policy_pending_ids"], base_url, http)

    available = sorted(set(catalog["available_model_ids"]) | set(enabled_ids))
    credential["available_model_ids"] = available
    credential["models"] = {
        model_id: {"api": catalog["models"].get(model_id, {}).get("api", get_wire_api(model_id))}
        for model_id in available
    }
    save_credential(credential)
    return credential


class CopilotProvider(Provider):
    """GitHub Copilot. Auth flow only — ``resolve`` is T6."""

    id = PROVIDER_ID

    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        raise NotImplementedError(
            "GitHub Copilot runtime resolution is implemented in T6 (PLAN.md §2.3); "
            "run the login flow (T5: 'sdlc-factory copilot-login') for now."
        )

    def login(self, prompt=None, http=None, sleep=None, enterprise=None, notify=None) -> dict:
        return login(prompt=prompt, http=http, sleep=sleep, enterprise=enterprise, notify=notify)