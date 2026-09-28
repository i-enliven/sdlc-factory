"""GitHub Copilot provider — auth flow (T4) + runtime resolution (T6).

Port of pi's verified TypeScript implementation
(``packages/ai/src/auth/oauth/github-copilot.ts`` + ``.../device-code.ts`` +
``.../api/github-copilot-headers.ts``), see PLAN.md §2.3.

Implemented here: the OAuth device-code flow, the Copilot proxy-token exchange
(``copilot_internal/v2/token``), the model catalog fetch with policy auto-enable,
base-URL derivation from the token itself, ``auth.json`` persistence, the
read-only helpers the CLI needs (``copilot_status`` / ``logout``), the runtime
``resolve()`` (expiry refresh, wire API, static editor headers) and the dynamic
per-request headers (``prepare_headers``).

Not implemented here: the Responses wire API itself (T7) and the 401 mid-run
force-refresh (T8 — :meth:`CopilotProvider.refresh` is the reusable entry point).

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

from ..utils import abort, global_logger
from . import store
from .base import Provider, ResolvedAuth, resolve_timeout

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

# Runtime abort messages. One place, so the CLI and the agent fail the same way.
NOT_CONFIGURED_MESSAGE = "Copilot not configured — run 'sdlc-factory copilot-login'"
SESSION_EXPIRED_MESSAGE = "Copilot session expired — run 'sdlc-factory copilot-login'"

# Dynamic per-request headers (PLAN.md §2.3). Copilot bills/attributes requests by
# initiator and rejects vision calls that do not declare an image payload.
INITIATOR_HEADER = "X-Initiator"
INITIATOR_USER = "user"
INITIATOR_AGENT = "agent"
OPENAI_INTENT_HEADER = "Openai-Intent"
OPENAI_INTENT_VALUE = "conversation-edits"
VISION_REQUEST_HEADER = "Copilot-Vision-Request"
VISION_REQUEST_VALUE = "true"
IMAGE_CONTENT_TYPE = "image"
# "toolResult" is the role name pi-style transcripts use for tool output; this
# codebase emits OpenAI-style "tool" messages. Both carry images.
VISION_CONTENT_ROLES = ("user", "toolResult", "tool")

UNSUPPORTED_WIRE_API = "unsupported"

# Status rendering. Local time on purpose: the expiry is a wall-clock fact the
# user compares against their own clock, not a duration to reason about.
EXPIRY_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


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
        return UNSUPPORTED_WIRE_API
    if lowered.startswith(RESPONSES_API_PREFIXES):
        return "responses"
    return "completions"


# --- runtime: credential state, catalog, dynamic headers ---

def credential_is_expired(credential: dict, now_ms: Optional[float] = None) -> bool:
    """True when the stored expiry has passed — or when it cannot be trusted.

    ``expires`` is epoch millis and already carries the 5-minute early-refresh
    buffer (see :func:`fetch_copilot_token`). A missing or non-numeric expiry is
    "expired": we would rather pay for a refresh than send a request that 401s.
    """
    expires = credential.get("expires")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return True
    current = now_ms if now_ms is not None else time.time() * 1000
    return expires <= current


def stored_enterprise_domain(credential: dict) -> Optional[str]:
    """The stored enterprise domain, or ``None`` for github.com."""
    raw = credential.get("enterprise_url")
    return raw if isinstance(raw, str) and raw else None


def catalog_model_ids(credential: dict) -> list:
    """Every model id the stored credential knows about, sorted."""
    ids = set()
    models = credential.get("models")
    if isinstance(models, dict):
        ids.update(model_id for model_id in models if isinstance(model_id, str))
    available = credential.get("available_model_ids")
    if isinstance(available, list):
        ids.update(model_id for model_id in available if isinstance(model_id, str))
    return sorted(ids)


def wire_api_for(credential: dict, model_id: Any) -> str:
    """Wire API for ``model_id`` from the stored catalog; aborts when unusable.

    An unknown model is a config typo or a stale catalog and must fail here, with
    the ids that *are* available, rather than at request time. A model whose wire
    API Copilot only serves as Anthropic-Messages is out of scope (PLAN.md §5):
    naming the limitation is more useful than a stream that never parses.
    """
    if not isinstance(model_id, str) or not model_id:
        abort("Copilot agent has no model configured — set models.<agent>.model")
    models = credential.get("models")
    entry = models.get(model_id) if isinstance(models, dict) else None
    api = entry.get("api") if isinstance(entry, dict) else None
    if not isinstance(api, str) or not api:
        available = catalog_model_ids(credential)
        if model_id not in available:
            listing = ", ".join(available) or "(none — run 'sdlc-factory copilot-login')"
            abort(f"Copilot model '{model_id}' is not in the catalog. "
                  f"Available models: {listing}")
        # Pre-T4 credentials stored no per-model api; infer it from the id prefix,
        # exactly like `api_summary` does for `copilot-status`.
        api = get_wire_api(model_id)
    if api == UNSUPPORTED_WIRE_API:
        abort(f"Copilot serves '{model_id}' over the anthropic-messages wire API, which "
              f"this codebase does not speak (PLAN.md §5). Choose a model served over "
              f"chat/completions or Responses.")
    return api


def _message_field(message: Any, name: str) -> Any:
    """Read ``name`` from a message dict *or* an SDK message object.

    ``chat.py``/``agent.py`` append the SDK's own message objects to the history,
    so the pre-send hook cannot assume plain dicts.
    """
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _has_image_part(message: Any) -> bool:
    content = _message_field(message, "content")
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == IMAGE_CONTENT_TYPE
               for part in content)


def prepare_headers(messages: Any) -> dict:
    """Per-request Copilot headers, derived from the outgoing messages.

    Computed at send time, not at client construction: the history grows between
    iterations, so the initiator and the vision flag are different facts on every
    attempt. Malformed entries (non-dict messages, string content, missing roles)
    are ignored rather than raising — a header must never break a send.
    """
    history = messages if isinstance(messages, list) else []
    last_role = None
    for message in reversed(history):
        role = _message_field(message, "role")
        if isinstance(role, str) and role:
            last_role = role
            break
    headers = {
        INITIATOR_HEADER: INITIATOR_USER if last_role in (None, INITIATOR_USER)
        else INITIATOR_AGENT,
        OPENAI_INTENT_HEADER: OPENAI_INTENT_VALUE,
    }
    if any(_message_field(message, "role") in VISION_CONTENT_ROLES
           and _has_image_part(message) for message in history):
        headers[VISION_REQUEST_HEADER] = VISION_REQUEST_VALUE
    return headers


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

def refresh_credential(http: Any = None) -> dict:
    """Replay the stored GitHub token for a fresh proxy token + catalog.

    Rewrites the ``github-copilot`` entry in auth.json (other providers' entries
    are preserved by :func:`save_credential`). The catalog is re-fetched because
    model availability is account state, not token state — but previously enabled
    ids are *kept*: a refresh rotates a credential, it is not a re-authorization,
    and dropping a model here would break an agent that is mid-session.
    """
    stored = load_credential() or {}
    gh_token = stored.get("refresh")
    enterprise = stored_enterprise_domain(stored)
    if not isinstance(gh_token, str) or not gh_token:
        abort(SESSION_EXPIRED_MESSAGE)
    try:
        fresh = fetch_copilot_token(gh_token, enterprise, http)
        base_url = get_base_url(fresh["access"], enterprise)
        catalog = fetch_model_catalog(fresh["access"], base_url, http)
    except (CopilotAuthError, OSError, ValueError) as error:
        global_logger.warning(f"Copilot token refresh failed: {error}")
        abort(SESSION_EXPIRED_MESSAGE)

    stored_models = stored.get("models")
    models = dict(stored_models) if isinstance(stored_models, dict) else {}
    models.update(catalog["models"])
    credential = {
        **stored,
        **fresh,
        "available_model_ids": sorted(set(catalog_model_ids(stored))
                                      | set(catalog["available_model_ids"])),
        "models": models,
    }
    save_credential(credential)
    return credential


def save_credential(credential: dict) -> None:
    """Merge our entry into auth.json; other providers' entries are preserved."""
    data = store.read_auth()
    data[PROVIDER_ID] = credential
    store.write_auth(data)


def load_credential() -> Optional[dict]:
    """The stored ``github-copilot`` credential, or ``None`` when not logged in."""
    credential = store.read_auth().get(PROVIDER_ID)
    return credential if isinstance(credential, dict) else None


def logout() -> bool:
    """Drop the ``github-copilot`` entry from auth.json, keeping every other key.

    Returns ``True`` when an entry was actually removed. Nothing is revoked
    server-side (PLAN.md §5) — the GitHub-side revocation is a human action.
    """
    data = store.read_auth()
    if PROVIDER_ID not in data:
        return False
    data.pop(PROVIDER_ID)
    store.write_auth(data)
    return True


def format_expiry(expires_ms: Any) -> Optional[str]:
    """Render an epoch-millis expiry as local time; ``None`` when unusable."""
    if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
        return None
    try:
        return time.strftime(EXPIRY_TIME_FORMAT, time.localtime(expires_ms / 1000))
    except (OverflowError, OSError, ValueError):
        return None


def api_summary(model_ids, models: Optional[dict] = None) -> dict:
    """``{wire api: model count}`` for ``model_ids``, from the stored catalog.

    Counted over the *same* ids the status reports as available, so the summary
    can never disagree with the model count; a model missing from the stored
    ``models`` block falls back to prefix inference (:func:`get_wire_api`).
    """
    catalog = models if isinstance(models, dict) else {}
    summary: dict = {}
    for model_id in model_ids:
        entry = catalog.get(model_id)
        api = entry.get("api") if isinstance(entry, dict) else None
        if not isinstance(api, str) or not api:
            api = get_wire_api(model_id)
        summary[api] = summary.get(api, 0) + 1
    return summary


def copilot_status() -> dict:
    """Summary of the stored credential for ``copilot-status`` to print.

    ``{"logged_in": False}`` when there is no credential. ``expires_at`` is the
    stored value, which already carries the 5-minute early-refresh buffer, so it
    is the deadline the runtime refreshes *by*, not the raw token expiry. A
    missing or non-numeric expiry counts as expired, since T6's ``resolve()``
    treats an unusable ``expires`` as "refresh now".
    """
    credential = load_credential()
    if not credential:
        return {"logged_in": False}
    expires_ms = credential.get("expires")
    usable_expiry = isinstance(expires_ms, (int, float)) and not isinstance(expires_ms, bool)
    raw_ids = credential.get("available_model_ids")
    model_ids: list = []
    if isinstance(raw_ids, list):
        model_ids = sorted(model_id for model_id in raw_ids if isinstance(model_id, str))
    enterprise_url = stored_enterprise_domain(credential)
    return {
        "logged_in": True,
        "base_url": get_base_url(credential.get("access"), enterprise_url),
        "enterprise_url": enterprise_url,
        "expires_at": int(expires_ms) if usable_expiry else None,
        "expires_human": format_expiry(expires_ms),
        "expired": credential_is_expired(credential),
        "model_count": len(model_ids),
        "model_ids": model_ids,
        "api_summary": api_summary(model_ids, credential.get("models")),
    }


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
    """GitHub Copilot: short-lived bearer, token-derived base URL, editor headers."""

    id = PROVIDER_ID

    def resolve(self, agent_cfg: dict, config: dict, http: Any = None) -> ResolvedAuth:
        """Resolve the stored credential into a connection for one agent.

        The base URL comes from the token's ``proxy-ep`` claim and from nowhere
        else (pi regression #6768): a config/catalog URL that happens to work for
        the first call sends retry and session traffic to the wrong host. The
        static editor headers go on the client; the initiator/vision headers are
        per request, see :meth:`prepare_headers`.

        ``http`` is the same injectable seam the auth flow uses, so tests can
        drive a refresh without a socket.
        """
        credential = load_credential()
        if not credential:
            abort(NOT_CONFIGURED_MESSAGE)
        if credential_is_expired(credential):
            credential = self.refresh(http=http)
        access = credential.get("access")
        if not isinstance(access, str) or not access:
            abort(NOT_CONFIGURED_MESSAGE)
        return ResolvedAuth(
            base_url=get_base_url(access, stored_enterprise_domain(credential)),
            api_key=access,
            headers=dict(EDITOR_HEADERS),
            timeout=resolve_timeout(config),
            wire_api=wire_api_for(credential, agent_cfg.get("model")),
        )

    @classmethod
    def refresh(cls, http: Any = None) -> dict:
        """Force a token + catalog refresh and persist it. Also used on 401 (T8)."""
        return refresh_credential(http=http)

    def prepare_headers(self, messages: list) -> dict:
        return prepare_headers(messages)

    def login(self, prompt=None, http=None, sleep=None, enterprise=None, notify=None) -> dict:
        return login(prompt=prompt, http=http, sleep=sleep, enterprise=enterprise, notify=notify)