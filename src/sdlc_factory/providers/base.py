"""Provider interface: resolves auth for a model provider.

Everything in this codebase speaks the OpenAI SDK, so a provider only has to
say where to connect, with which credentials/headers, and over which wire API.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..utils import abort

DEFAULT_API_TIMEOUT = 600.0
ENV_REF_PREFIX = "env:"

# The wire APIs ``ResolvedAuth.wire_api`` names. ``completions`` is what this
# codebase has always spoken; ``responses`` is Copilot's second surface (T7);
# ``unsupported`` (anthropic-messages) is rejected by the provider, not routed.
COMPLETIONS_WIRE_API = "completions"
RESPONSES_WIRE_API = "responses"


@dataclass
class ResolvedAuth:
    """A fully resolved connection: what ``make_client`` builds a client from."""

    base_url: str
    api_key: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_API_TIMEOUT
    wire_api: str = COMPLETIONS_WIRE_API


def resolve_timeout(config: dict) -> float:
    """Legacy timeout semantics: ``api_timeout`` from config, else 600.0."""
    return float(config.get("api_timeout", DEFAULT_API_TIMEOUT))


def provider_options(provider_id: str, config: dict) -> dict:
    """The optional top-level ``providers.<id>`` block from config.json.

    Anything that is not a mapping (section absent, block absent, or a typo that
    made it a string/list) counts as "no overrides", so configs without a
    ``providers`` section keep today's behavior exactly.
    """
    section = config.get("providers")
    if not isinstance(section, dict):
        return {}
    options = section.get(provider_id)
    return options if isinstance(options, dict) else {}


def provider_base_url(
    provider_id: str,
    config: dict,
    default: str,
    legacy_key: Optional[str] = None,
) -> str:
    """Precedence: ``providers.<id>.base_url`` > ``config[legacy_key]`` > ``default``.

    A blank/absent ``base_url`` override counts as unset; a non-string one aborts.
    The legacy step keeps ``config.get(legacy_key, default)`` semantics — including
    a present-but-empty legacy key winning over the default — so pre-``providers``
    configs are byte-for-byte equivalent.
    """
    override = provider_options(provider_id, config).get("base_url")
    if override:
        if not isinstance(override, str):
            abort(f"providers.{provider_id}.base_url must be a string, got {override!r}")
        return override
    if legacy_key and legacy_key in config:
        return config[legacy_key]
    return default


def api_key_from_ref(provider_id: str, config: dict) -> Optional[str]:
    """Resolve ``providers.<id>.api_key_ref``; ``None`` when no ref is configured.

    ``"env:VAR_NAME"`` reads ``os.environ``, any other string names a top-level
    key in config.json. When a ref *is* configured it REPLACES the provider's
    legacy key-fallback chain entirely, so an unresolvable ref aborts (naming the
    ref) instead of silently falling back to a different credential. Absent, null
    or blank refs mean "no ref" and the legacy chain stays in charge.
    """
    ref = provider_options(provider_id, config).get("api_key_ref")
    if not ref:
        return None
    if not isinstance(ref, str):
        abort(f"providers.{provider_id}.api_key_ref must be a string, got {ref!r}")
    if ref.startswith(ENV_REF_PREFIX):
        var = ref[len(ENV_REF_PREFIX):]
        value = os.environ.get(var)
        if not value:
            abort(
                f"providers.{provider_id}.api_key_ref '{ref}': "
                f"environment variable '{var}' is not set"
            )
        return value
    value = config.get(ref)
    if not value:
        abort(
            f"providers.{provider_id}.api_key_ref '{ref}': "
            f"no such key (or empty value) in config.json"
        )
    return str(value)


class Provider(ABC):
    """A model provider. Subclasses set ``id`` and implement ``resolve``."""

    id: str = ""

    @abstractmethod
    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth:
        """Resolve base URL, key, headers and wire API for one agent."""

    def prepare_headers(self, messages: list) -> dict[str, str]:
        """Per-request headers derived from the outgoing messages (none by default)."""
        return {}