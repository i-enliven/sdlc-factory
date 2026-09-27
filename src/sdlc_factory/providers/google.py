"""Google (AI Studio / Vertex) provider — moved verbatim from the
``provider == "google"`` branch of ``agent.py::_setup_client``, with the optional
``providers.google`` overrides from config.json layered on top (see ``resolve``)."""

import os

from .base import (
    Provider,
    ResolvedAuth,
    api_key_from_ref,
    provider_base_url,
    resolve_timeout,
)

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GoogleProvider(Provider):
    id = "google"

    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        """Config precedence for Google.

        Base URL: ``providers.google.base_url`` if set, else the fixed AI Studio
        URL. The override IS honored here (unlike ``vllm_base_url``, which google
        has always ignored) so a Vertex/gateway proxy can be pointed at without a
        code change. Key: ``providers.google.api_key_ref`` when present (it
        replaces the chain below), otherwise the legacy fallback chain, unchanged.
        """
        base_url = provider_base_url(self.id, config, GOOGLE_BASE_URL)
        api_key = api_key_from_ref(self.id, config)
        if api_key is None:
            api_key = (
                config.get("gemini_api_key")
                or os.environ.get("GEMINI_API_KEY")
                or config.get("vertex_api_key")
                or os.environ.get("OPENAI_API_KEY")
                or "EMPTY"
            )
        return ResolvedAuth(base_url=base_url, api_key=api_key, timeout=resolve_timeout(config))
