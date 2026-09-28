"""Provider registry, the single client-construction entry point, the
per-request header hook, and the mid-run credential recovery (T8)."""

from typing import Callable, Optional

import typer
from openai import AuthenticationError, OpenAI

from ..utils import abort, global_logger
from .base import Provider, ResolvedAuth
from .copilot import CopilotProvider
from .google import GoogleProvider
from .vllm import VllmProvider

PROVIDERS: dict[str, Provider] = {
    VllmProvider.id: VllmProvider(),
    GoogleProvider.id: GoogleProvider(),
    CopilotProvider.id: CopilotProvider(),
}

# A 401 mid-run is the one API failure this layer can fix by itself: Copilot's
# bearer lives ~10 minutes (PLAN.md §5 "token lifetime"), so a long run crosses
# expiry between iterations. The status line and the body both carry the code,
# and the SDK only raises its own exception type for it, so match both.
AUTH_ERROR_MARKERS = ("401", "unauthorized", "authentication_error", "invalid_bearer_token")

# What the user is told when even a freshly minted credential is rejected — the
# credential on disk is fine, the account behind it is not, so only a human can
# fix it. Same command as every other auth abort.
AUTH_RECOVERY_FAILED_MESSAGE = (
    "Copilot rejected a freshly refreshed credential — run 'sdlc-factory copilot-login' "
    "to re-authenticate"
)
AUTH_RECOVERY_LOG = "🔑 Credential rejected mid-run (HTTP 401) — refreshing and retrying once..."


def get_provider(provider_id: str) -> Provider:
    """Look up a provider by id. Unknown ids fail loud — no silent fallback."""
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown provider '{provider_id}'. Known providers: {known}")
    return provider


def make_client(
    provider_id: str,
    agent_cfg: dict,
    config: dict,
    client_factory: Optional[Callable[..., OpenAI]] = None,
) -> tuple[OpenAI, ResolvedAuth]:
    """Build the OpenAI client for ``provider_id``; returns the client and its auth.

    Optional per-provider overrides live in the top-level ``providers`` section of
    config.json (``providers.<id>.base_url`` / ``providers.<id>.api_key_ref``) and
    are applied by ``Provider.resolve``; see ``providers.base`` for the precedence
    rules. ``agent_cfg`` may carry provider-level overrides later — not yet read.

    ``client_factory`` defaults to ``openai.OpenAI``. Callers pass their own
    module-level ``OpenAI`` symbol so that ``sdlc_factory.agent.OpenAI`` and
    ``sdlc_factory.chat.OpenAI`` stay valid patch points for client creation.
    """
    provider = get_provider(provider_id)
    auth = provider.resolve(agent_cfg, config)
    client = (client_factory or OpenAI)(
        base_url=auth.base_url,
        api_key=auth.api_key,
        default_headers=auth.headers or None,
        timeout=auth.timeout,
    )
    return client, auth


def request_headers(provider: Optional[Provider], messages: list) -> Optional[dict]:
    """Per-request headers for one send, or ``None`` when the provider has none.

    Callers pass the ``Provider`` object next to the client they built with
    :func:`make_client`; the hook is re-evaluated for every request because the
    message history changes between iterations (see ``CopilotProvider``). Most
    providers have no dynamic headers, and the OpenAI SDK treats a ``None``
    ``extra_headers`` as "nothing to add", so that is what we pass through.
    """
    prepare = getattr(provider, "prepare_headers", None)
    if not callable(prepare):
        return None
    return prepare(messages) or None


# --- mid-run credential recovery (T8) ---

def is_auth_error(error: BaseException) -> bool:
    """True when ``error`` says the credential itself was rejected.

    The SDK's own ``AuthenticationError`` is the precise signal; the string
    markers catch a 401 that arrives through a path that does not raise it (a
    streamed error, a proxy, a provider that reports it in the body).
    """
    if isinstance(error, AuthenticationError):
        return True
    text = str(error).lower()
    return any(marker in text for marker in AUTH_ERROR_MARKERS)


def can_refresh_credentials(provider: Optional[Provider]) -> bool:
    """Whether this provider owns a credential it can renew by itself.

    ``github-copilot`` is the only provider that implements ``refresh`` today;
    vllm and google have nothing to renew, so their 401s keep taking the ordinary
    retry path untouched (PLAN.md §3-T8).
    """
    return callable(getattr(provider, "refresh", None))


def apply_auth(client, auth: ResolvedAuth) -> None:
    """Point an already-built client at freshly resolved auth, in place.

    The SDK reads ``api_key`` and ``base_url`` per request, so re-pointing those
    two is what actually changes the next call. The header bags are updated too:
    the constructor's ``default_headers`` land in ``_custom_headers``, and the
    underlying httpx client keeps its own copy — a stale editor header or bearer
    living there would outlive the credential it belongs to.
    """
    client.api_key = auth.api_key
    client.base_url = auth.base_url
    headers = dict(auth.headers)
    defaults = getattr(client, "_custom_headers", None)
    if isinstance(defaults, dict):
        defaults.update(headers)
    live_headers = getattr(getattr(client, "_client", None), "headers", None)
    if live_headers is None:
        return
    try:
        live_headers["Authorization"] = f"Bearer {auth.api_key}"
        for name, value in headers.items():
            live_headers[name] = value
    except (TypeError, AttributeError):
        # A stand-in client whose header bag is not a real httpx.Headers still got
        # the two attributes the SDK actually reads; that is all it needs.
        pass


def rebind_client(client, provider: Provider, agent_cfg: Optional[dict] = None,
                  config: Optional[dict] = None) -> ResolvedAuth:
    """Force a credential refresh and re-point ``client`` at the new credential.

    The refresh writes ``auth.json`` first and the result is then re-resolved, so
    the live client and the file on disk cannot disagree about which token is
    current — the same rule that makes ``resolve`` derive the base URL from the
    token (pi regression #6768) rather than from config.
    """
    provider.refresh()
    auth = provider.resolve(agent_cfg or {}, config or {})
    apply_auth(client, auth)
    return auth


def try_recover_auth(client, provider: Optional[Provider], error: BaseException,
                     already_recovered: bool, agent_cfg: Optional[dict] = None,
                     config: Optional[dict] = None) -> bool:
    """One shot at fixing a mid-run 401; the callers' single entry point for it.

    Returns ``True`` when the caller should retry the request immediately (no
    backoff sleep, no attempt consumed), ``False`` when the error is not ours to
    fix and the caller's normal retry path applies. A second 401 after a refresh
    aborts: the stored refresh token produced a credential the server still
    rejects, so only a re-login can help.

    The retry re-sends the *same* request over the same wire API — what changed is
    the credential, not the route.

    ``already_recovered`` is the caller's per-send flag — one recovery per send,
    not one per process, so a run that lasts hours gets one refresh per expiry.
    """
    if not is_auth_error(error) or not can_refresh_credentials(provider):
        return False
    if already_recovered:
        abort(AUTH_RECOVERY_FAILED_MESSAGE)
    global_logger.info(AUTH_RECOVERY_LOG, extra={"color": typer.colors.YELLOW})
    rebind_client(client, provider, agent_cfg, config)
    return True