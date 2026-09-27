# PLAN: Configurable Provider Layer + GitHub Copilot Provider

Status: draft · Scope: `src/sdlc_factory/` · Reference impl: `~/Projects/pi` (TypeScript, `packages/ai`)

## 1. Context & problem

LLM client construction is duplicated and hardcoded in two places:

- `src/sdlc_factory/agent.py::_setup_client()` (lines ~56-65) — branches on `provider == "google"` vs. everything else (treated as vllm), with a chained key fallback (`vertex_api_key → gemini_api_key → GEMINI_API_KEY → OPENAI_API_KEY → "EMPTY"`).
- `src/sdlc_factory/chat.py::run_chat_session()` (lines ~35-48) — the same logic copy-pasted inline.

Provider semantics (base URL, auth, extra headers, token lifecycle) cannot vary per provider without more `if/else` in both copies. GitHub Copilot needs all of that: a short-lived (~10 min) bearer token refreshed from a stored GitHub refresh token, a per-credential base URL derived from the token itself, and request headers that impersonate VS Code.

**Goal 1:** one configurable provider layer; `local-vllm` stays the default and existing configs keep working unchanged.
**Goal 2:** a `github-copilot` provider with a login/configure workflow.

## 2. Design

### 2.1 New package `src/sdlc_factory/providers/`

```
providers/
  __init__.py        # registry: get_provider(name) -> Provider
  base.py            # Provider ABC + ResolvedAuth dataclass
  vllm.py            # default provider (current behavior, verbatim semantics)
  google.py          # current "google" branch, moved as-is
  copilot.py         # GitHub Copilot: OAuth device flow, token refresh, headers
  store.py           # ~/.sdlc-factory/auth.json read/write (0600)
```

**Interface** (keep it small — everything in `agent.py`/`chat.py` already speaks the OpenAI SDK):

```python
@dataclass
class ResolvedAuth:
    base_url: str
    api_key: str                 # sent as Bearer
    headers: dict[str, str]      # static + dynamic per-request headers
    timeout: float

class Provider(ABC):
    id: str
    def resolve(self, agent_cfg: dict, config: dict) -> ResolvedAuth: ...
    def prepare_messages(self, messages: list) -> list:  # default: no-op
        ...  # copilot overrides to inject vision header signal, see 2.3
```

`make_client(provider_id, agent_cfg, config) -> tuple[OpenAI, ResolvedAuth]` is the single entry point used by `agent.py` and `chat.py`. The OpenAI client is constructed with `base_url`, `api_key`, `default_headers=auth.headers`, `timeout=auth.timeout`.

Registry lookup: `providers.get_provider(agent_cfg.get("provider", "vllm"))`. Unknown provider id → `abort()` with the list of known ids (fail loud, per repo rules — no silent fallback).

### 2.2 Config schema (backwards compatible)

`~/.sdlc-factory/config.json` gains an optional top-level `providers` section; nothing existing changes meaning:

```json
{
  "vllm_base_url": "https://sagittarius-a.mara-balance.ts.net:8443/v1",
  "providers": {
    "vllm":    { "base_url": "<falls back to vllm_base_url>", "api_key_ref": "vertex_api_key" },
    "google":  { "api_key_ref": "gemini_api_key" },
    "github-copilot": { "model_override": null }
  },
  "models": {
    "coder":   { "model": "local-vllm", "provider": "vllm", ... },
    "dreamer": { "model": "claude-sonnet-4.5", "provider": "github-copilot", ... }
  }
}
```

Resolution rules:
- `models.<agent>.provider` defaults to `"vllm"` (unchanged).
- `providers.<id>` keys override the built-in defaults for that provider; absent section = today's behavior exactly (including the legacy key-fallback chain for `vllm`/`google`, so current configs are byte-for-byte equivalent).
- `api_key_ref` names a config key or `env:VAR_NAME`. Secrets stay in `config.json`/env as today; Copilot's OAuth tokens live in `auth.json` (2.4), never in `config.json`.

### 2.3 Copilot provider mechanics (ported from pi, verified against live API)

Reference files: `~/Projects/pi/packages/ai/src/auth/oauth/github-copilot.ts`, `.../api/github-copilot-headers.ts`, `.../providers/github-copilot.ts`.

**Login flow** (`sdlc-factory copilot-login`, one-time, interactive):
1. `POST https://github.com/login/device/code` (client_id = the VS Code Copilot Chat public client id `Iv1.b507a08c87ecfe98`, scope `read:user`) → print user code + `https://github.com/login/device`, poll `POST /login/oauth/access_token` (handle `authorization_pending` / `slow_down`).
2. `GET https://api.github.com/copilot_internal/v2/token` with `Authorization: Bearer <gh_access_token>` + editor headers → Copilot proxy token (`token`, `expires_at`).
3. `GET {base}/models` → keep ids where `capabilities.supports.tool_calls != false` and picker-enabled / policy not `disabled`; auto-`POST /models/{id}/policy {"state":"enabled"}` for `policy.state == "unconfigured"` models (Claude/Grok require this).
4. Persist to `~/.sdlc-factory/auth.json` (chmod 0600):
   ```json
   { "github-copilot": { "refresh": "<gh token>", "access": "<copilot token>",
       "expires": <epoch_ms - 300000>, "enterprise_url": null,
       "available_model_ids": [...] } }
   ```

**Runtime (`copilot.py::resolve`)**:
- If `expires` passed or missing → refresh: re-call `copilot_internal/v2/token` with stored `refresh`, re-fetch catalog, rewrite `auth.json`. On refresh failure → `abort("Copilot session expired — run 'sdlc-factory copilot-login'")`.
- **Base URL from the token itself**: token string contains `proxy-ep=proxy.individual.githubcopilot.com`; rewrite `proxy.` → `api.` → `https://api.individual.githubcopilot.com`. Enterprise: `https://copilot-api.{domain}`. Do not trust the static catalog URL (pi regression #6768 — compaction/secondary calls must use the auth-resolved URL; same trap applies to our retry/session paths).
- **Static headers** on every request: `User-Agent: GitHubCopilotChat/0.35.0`, `Editor-Version: vscode/1.107.0`, `Editor-Plugin-Version: copilot-chat/0.35.0`, `Copilot-Integration-Id: vscode-chat`.
- **Dynamic headers** per request: `X-Initiator: user|agent` (last message role != user → `agent`), `Openai-Intent: conversation-edits`, `Copilot-Vision-Request: true` when any user/toolResult content carries an image. Since `agent.py` builds messages incrementally, compute these in a `prepare_messages`/pre-send hook, not at client construction.

**API-shape constraint:** Copilot serves different models over different wire APIs — Claude 4.x/5.x via Anthropic-Messages, `gpt-5*`/`grok-*`/`oswe*`/`mai-*` via OpenAI **Responses**, the rest via OpenAI **chat/completions**. This codebase speaks only `client.chat.completions.create`. **GPT models on Copilot are in scope**: T7 adds a Responses-API send path (`client.responses.create`) with streaming + tool-call reassembly, and `_send_with_retry` routes by the model's wire API recorded in the catalog. Anthropic-Messages routing (Claude via Copilot) stays out of scope (§5).

**Wire-API routing:** `copilot-login` stores per-model `api` (`responses` | `completions`) in `auth.json` catalog. `resolve()` returns it; `agent.py` picks the send path accordingly. Non-Copilot providers always use `completions` (unchanged).

### 2.4 CLI workflow

New Typer commands in `cli.py` (thin wrappers, logic in `providers/copilot.py` — repo rule: no business logic in the CLI layer):

- `sdlc-factory copilot-login [--enterprise <domain>]` — device flow, writes `auth.json`, prints the enabled model ids.
- `sdlc-factory copilot-status` — token expiry, model count, base URL.
- `sdlc-factory copilot-logout` — delete the `github-copilot` entry from `auth.json` (does not revoke server-side).
- `sdlc-factory config` — extend to seed the `providers` section; `docs/dot-sdlc-factory` sample config updated with a commented `providers` example.

Switching an agent to Copilot = edit `models.<agent>.provider` + `model`; no code change.

## 3. Task list (delegated to pi, validated per task)

Branch: `feat/provider-layer-copilot`. Status legend: `[ ]` todo · `[~]` in progress · `[x]` implemented+validated · `[!]` blocked/needs rework. Each task = one pi delegation + parent validation (full pytest green + diff review). Existing tests must stay green unmodified throughout.

- [x] **T1 — Providers package skeleton.** `src/sdlc_factory/providers/`: `base.py` (`Provider` ABC, `ResolvedAuth` dataclass), `__init__.py` registry (`get_provider`, `make_client`), `vllm.py` + `google.py` extracted verbatim from `agent.py::_setup_client` (legacy key-fallback chains preserved), `store.py` (`auth.json` read/write, atomic, 0600). New `tests/test_providers.py`. *Validated: 122 passed (82 baseline + 40 new), existing tests untouched, 100% cov on new modules. Commit 8468fc9.*
- [~] **T2 — Wire layer into callers.** Replace `_setup_client` body in `agent.py` and inline block in `chat.py` with `make_client()`. Zero behavior change; `tests/test_agent.py` + `tests/test_chat.py` pass unmodified.
- [ ] **T3 — Config plumbing.** Optional `providers:` section in config; `api_key_ref` (`config-key` or `env:VAR`); unknown provider id aborts listing known ids; `docs/dot-sdlc-factory` sample updated. Tests: precedence, absent-section = today's behavior.
- [ ] **T4 — Copilot auth flow.** `providers/copilot.py`: device-code flow, `copilot_internal/v2/token`, catalog fetch + policy auto-enable, `auth.json` persistence incl. per-model `api` (`responses`/`completions`). All HTTP through one injectable seam. `tests/test_copilot_auth.py` fully mocked.
- [ ] **T5 — Copilot CLI.** `copilot-login` / `copilot-status` / `copilot-logout` Typer commands (thin wrappers). `tests/test_cli.py` extended.
- [ ] **T6 — Copilot runtime resolve.** `resolve()`: expiry refresh, `proxy-ep`→`api.` base URL derivation, static VS Code headers, dynamic headers (`X-Initiator`, `Openai-Intent`, `Copilot-Vision-Request`) via pre-send hook wired into `_send_with_retry` + `chat.py`. Model-not-in-catalog abort. Tests incl. pi #6768-style base-URL regression.
- [ ] **T7 — Responses API path for GPT models.** `providers/responses.py`: send path via `client.responses.create` with streaming + tool-call reassembly, request/response translation to/from the internal chat-style message dicts used by sessions; `_send_with_retry` routes by catalog `api` field. Non-Copilot providers unaffected. `tests/test_responses_path.py` + agent-level routing test.
- [ ] **T8 — Hardening + coverage gate.** 401 mid-run → force-refresh once → retry; `auth.json` perm warn; session-start log of provider/base_url/model; verify heartbeat/MCP inherit via shared `make_client`. Gate: `pytest --cov=src/sdlc_factory/providers` — new modules ≥90% line coverage, full suite green.

## 4. Testing

- `tests/test_providers.py` — registry, default resolution (no `providers` section → identical base_url/key/timeout as today for both vllm and google), `api_key_ref` forms, unknown provider abort.
- `tests/test_copilot_provider.py` — device-flow state machine (pending/slow_down/success), token parse (`proxy-ep` extraction, expiry buffer), catalog filtering + policy-enable batching, refresh-on-expiry, abort when model not completions-capable. HTTP mocked via injected fakes; no network.
- `tests/test_copilot_headers.py` — `X-Initiator` inference, vision detection incl. toolResult images.
- Regression: existing `test_agent.py` / `test_chat.py` green without edits (P1 gate).

## 5. Out of scope / risks

- **Out of scope v1:** OpenAI-Responses and Anthropic-Messages wire routing for Copilot-only models (gpt-5*, grok*, Claude) — needs a second client surface in `_send_with_retry` incl. its streaming tool-call reassembly; do as a follow-up if actually needed. Token *revocation* on logout. Multi-account auth.json.
- **Risk: token lifetime.** Copilot bearer expires ~10 min; long `max_iterations` runs (coder: 150) will cross expiry — hence P5's 401→refresh→retry, and refresh must be safe under concurrent processes (write `auth.json` atomically via existing `write_json`; last-writer-wins is acceptable since refresh is idempotent).
- **Risk: header drift.** Copilot gates on VS Code identity headers; version strings belong in one constant block in `copilot.py`, easy to bump.
- **Risk: `local-vllm` regression.** The whole point of P1's "tests pass unmodified" gate — default path must be byte-equivalent, including the odd-but-relied-on key fallback chain and `"EMPTY"` placeholder.