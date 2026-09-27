"""Google (AI Studio / Vertex) provider — moved verbatim from the
``provider == "google"`` branch of ``agent.py::_setup_client``."""

import os

from .base import Provider, ResolvedAuth, resolve_timeout

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GoogleProvider(Provider):
    id = "google"

    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        api_key = (
            config.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or config.get("vertex_api_key")
            or os.environ.get("OPENAI_API_KEY")
            or "EMPTY"
        )
        return ResolvedAuth(base_url=GOOGLE_BASE_URL, api_key=api_key, timeout=resolve_timeout(config))