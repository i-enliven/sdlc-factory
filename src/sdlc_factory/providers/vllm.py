"""Local vLLM provider — the default. Semantics moved verbatim from
``agent.py::_setup_client`` / ``chat.py::run_chat_session``."""

import os

from .base import Provider, ResolvedAuth, resolve_timeout

DEFAULT_VLLM_BASE_URL = "http://sagittarius-a.mara-balance.ts.net:8100/v1"


class VllmProvider(Provider):
    id = "vllm"

    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        base_url = config.get("vllm_base_url", DEFAULT_VLLM_BASE_URL)
        api_key = (
            config.get("vertex_api_key")
            or config.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "EMPTY"
        )
        return ResolvedAuth(base_url=base_url, api_key=api_key, timeout=resolve_timeout(config))