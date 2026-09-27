"""Provider interface: resolves auth for a model provider.

Everything in this codebase speaks the OpenAI SDK, so a provider only has to
say where to connect, with which credentials/headers, and over which wire API.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

DEFAULT_API_TIMEOUT = 600.0


@dataclass
class ResolvedAuth:
    """A fully resolved connection: what ``make_client`` builds a client from."""

    base_url: str
    api_key: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_API_TIMEOUT
    wire_api: str = "completions"


def resolve_timeout(config: dict) -> float:
    """Legacy timeout semantics: ``api_timeout`` from config, else 600.0."""
    return float(config.get("api_timeout", DEFAULT_API_TIMEOUT))


class Provider(ABC):
    """A model provider. Subclasses set ``id`` and implement ``resolve``."""

    id: str = ""

    @abstractmethod
    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        """Resolve base URL, key, headers and wire API for one agent."""

    def prepare_headers(self, messages: list) -> dict[str, str]:
        """Per-request headers derived from the outgoing messages (none by default)."""
        return {}