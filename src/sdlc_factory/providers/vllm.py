"""Local vLLM provider — the default. Legacy semantics moved verbatim from
``agent.py::_setup_client`` / ``chat.py::run_chat_session``, with the optional
``providers.vllm`` overrides from config.json layered on top (see ``resolve``)."""

import os

from .base import (
    Provider,
    ResolvedAuth,
    api_key_from_ref,
    provider_base_url,
    resolve_timeout,
)

DEFAULT_VLLM_BASE_URL = "http://sagittarius-a.mara-balance.ts.net:8100/v1"


class VllmProvider(Provider):
    id = "vllm"

    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        """Config precedence for vLLM.

        Base URL: ``providers.vllm.base_url`` > ``vllm_base_url`` > the built-in
        default. Key: ``providers.vllm.api_key_ref`` when present (it replaces
        the chain below), otherwise the legacy fallback chain, unchanged.
        """
        base_url = provider_base_url(
            self.id, config, DEFAULT_VLLM_BASE_URL, legacy_key="vllm_base_url"
        )
        api_key = api_key_from_ref(self.id, config)
        if api_key is None:
            api_key = (
                config.get("vertex_api_key")
                or config.get("gemini_api_key")
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "EMPTY"
            )
        return ResolvedAuth(base_url=base_url, api_key=api_key, timeout=resolve_timeout(config))
