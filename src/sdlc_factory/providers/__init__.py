"""Provider registry and the single client-construction entry point."""

from typing import Callable, Optional

from openai import OpenAI

from .base import Provider, ResolvedAuth
from .google import GoogleProvider
from .vllm import VllmProvider

PROVIDERS: dict[str, Provider] = {
    VllmProvider.id: VllmProvider(),
    GoogleProvider.id: GoogleProvider(),
}


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