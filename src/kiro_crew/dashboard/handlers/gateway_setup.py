"""First-run gateway setup for the DeepSeek Harness ("own gateway" desktop path).

Two owner-gated routes back the onboarding wizard a user reaches when they pick
the ``deepseek`` backend in the setup gate's ``OtherCodingAgents`` selector:

* ``POST /api/gateway-setup/probe-models`` — ask the user's OpenAI-compatible
  gateway for its model list, so the wizard can show a default-model picker. It
  writes NOTHING: the authoritative model list is the one the running harness
  advertises in its session response (``_capture_available_models``); this probe
  only populates the picker and degrades to free text on failure.

* ``POST /api/gateway-setup/apply`` — persist the user's choices to the places
  that actually take effect, all inside KiroCrew's OWN config layer (never dsh's
  self-managed profile files):
    - ``DEEPSEEK_API_KEY`` → the vault, mapped by ``agent.deepseek_env``;
    - base URL → the ``agent.deepseek_base_url`` config field (the harness reads
      it at spawn — see ``acp/harness/deepseek.py``);
    - default model → ``agent.model`` (KiroCrew's existing model selection);
    - ``agent.acp_backend = "deepseek"`` — the moment the backend choice lands.

The config writes go through ``run_config_write`` under the shared config lock,
NOT ``PATCH /api/config/kirocrew``: ``agent.deepseek_env`` is not on that route's
allowlist, so a generic PATCH could not set it. The vault write and the config
write are reported independently so a partial failure names which half did not
land and never leaves ``acp_backend`` flipped without its credentials.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiohttp import web

from kiro_crew.config.loader import (
    ConfigReadError,
    coerce_dict_section,
    config_dir,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)

# The vault entry name and its deepseek_env mapping — the one credential name the
# harness resolves for the deepseek backend. Kept in one place so the handler and
# the config mapping cannot disagree.
_VAULT_KEY_NAME = "DEEPSEEK_API_KEY"
_SECRET_URI = f"secret://{_VAULT_KEY_NAME}"
_FOREIGN_BACKEND = "deepseek"

# A probe must not hang the dashboard on a wrong/slow URL.
_PROBE_TIMEOUT_S = 15.0
# A gateway's /models list is small; cap the body we read so a hostile or
# misconfigured endpoint cannot stream unbounded data into the handler.
_PROBE_MAX_BYTES = 1 * 1024 * 1024


async def _owner_only(request: web.Request) -> web.Response | None:
    """Return a 403 unless the caller is the configured dashboard owner.

    This route writes the vault and the agent config — owner-only material, the
    same gate the sibling ``secrets`` / ``agents`` handlers use. Authentication by
    the token middleware is not enough: an app token or a non-owner dashboard
    subject is authenticated but must not reconfigure the backend.
    """
    if is_owner_dashboard_request(request):
        return None
    return web.json_response(
        {"error": "owner authorization required", "code": "owner_only"}, status=403
    )


def _normalize_base_url(base_url: str) -> str:
    """Trim trailing slashes so ``<base>/models`` joins cleanly."""
    return base_url.strip().rstrip("/")


async def api_gateway_setup_probe_models(request: web.Request) -> web.Response:
    """POST /api/gateway-setup/probe-models — list the upstream gateway's models.

    Body: ``{"base_url": "...", "api_key": "..."}``. Returns ``{"models": [...]}``
    on success; a structured ``{"error", "code"}`` otherwise so the wizard can fall
    back to manual model entry. Writes nothing.
    """
    denied = await _owner_only(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"}, status=400
        )
    base_url = body.get("base_url")
    api_key = body.get("api_key")
    if not isinstance(base_url, str) or not base_url.strip():
        return web.json_response(
            {"error": "base_url is required", "code": "missing_base_url"}, status=400
        )
    if not isinstance(api_key, str) or not api_key.strip():
        return web.json_response(
            {"error": "api_key is required", "code": "missing_api_key"}, status=400
        )

    url = f"{_normalize_base_url(base_url)}/models"
    timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"Authorization": f"Bearer {api_key}"}
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    return web.json_response(
                        {
                            "error": "the gateway rejected the API key",
                            "code": "unauthorized",
                        },
                        status=200,
                    )
                if resp.status >= 400:
                    return web.json_response(
                        {
                            "error": f"the gateway returned HTTP {resp.status}",
                            "code": "upstream_error",
                        },
                        status=200,
                    )
                raw = await resp.content.read(_PROBE_MAX_BYTES + 1)
                if len(raw) > _PROBE_MAX_BYTES:
                    return web.json_response(
                        {"error": "the model list was too large", "code": "too_large"},
                        status=200,
                    )
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"could not reach the gateway: {exc}", "code": "unreachable"},
            status=200,
        )
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "the gateway did not respond in time", "code": "timeout"}, status=200
        )

    import json

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return web.json_response(
            {"error": "the gateway did not return JSON", "code": "not_json"}, status=200
        )
    # OpenAI /models shape: {"data": [{"id": "..."}, ...]}. Tolerate a bare list.
    rows = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        return web.json_response(
            {"error": "unexpected /models response shape", "code": "bad_shape"}, status=200
        )
    models = [
        str(r["id"])
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"].strip()
    ]
    return web.json_response({"models": models})


async def api_gateway_setup_apply(request: web.Request) -> web.Response:
    """POST /api/gateway-setup/apply — persist the gateway config and switch backend.

    Body: ``{"base_url", "api_key", "default_model"?}``. Writes the vault key, the
    ``deepseek_env`` mapping, ``deepseek_base_url``, an optional ``agent.model`` and
    ``agent.acp_backend = "deepseek"``. The vault write happens first; only if it
    lands does the config write (which includes the backend switch) run, so a failed
    key write never leaves the backend flipped without its credential.
    """
    denied = await _owner_only(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"}, status=400
        )
    base_url = body.get("base_url")
    api_key = body.get("api_key")
    default_model = body.get("default_model")
    if not isinstance(base_url, str) or not base_url.strip():
        return web.json_response(
            {"error": "base_url is required", "code": "missing_base_url"}, status=400
        )
    if not isinstance(api_key, str) or not api_key.strip():
        return web.json_response(
            {"error": "api_key is required", "code": "missing_api_key"}, status=400
        )
    if default_model is not None and not isinstance(default_model, str):
        return web.json_response(
            {"error": "default_model must be a string", "code": "invalid_default_model"},
            status=400,
        )

    normalized_base = _normalize_base_url(base_url)
    model = (default_model or "").strip()

    # 1) The provider key into the vault. First, because a failed key write must
    #    not leave the backend switched to a gateway it cannot authenticate to.
    try:
        vault = SecretVault(config_dir())
        await vault.set_with_revision(_VAULT_KEY_NAME, api_key)
    except Exception as exc:
        logger.warning("gateway-setup apply: vault write failed", exc_info=True)
        return web.json_response(
            {
                "error": "could not store the API key",
                "code": "vault_write_failed",
                "detail": str(exc)[:200],
                "applied": [],
            },
            status=500,
        )

    # 2) The config half, atomically under the shared lock. Everything here is a
    #    KiroCrew-owned field; dsh's own profile files are never touched.
    def _apply_config(doc: dict) -> dict | None:
        agent = coerce_dict_section(doc, "agent")
        # deepseek_env mapping: ensure DEEPSEEK_API_KEY -> secret://DEEPSEEK_API_KEY.
        env = agent.get("deepseek_env")
        if not isinstance(env, dict):
            env = {}
        env[_VAULT_KEY_NAME] = _SECRET_URI
        agent["deepseek_env"] = env
        agent["deepseek_base_url"] = normalized_base
        if model:
            agent["model"] = model
        # The backend choice lands here — last, so a config-write failure above
        # leaves acp_backend untouched (still "", setup screen still shown).
        agent["acp_backend"] = _FOREIGN_BACKEND
        return doc

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply_config)
    except ConfigReadError as exc:
        logger.warning("gateway-setup apply: config.json unreadable", exc_info=True)
        return web.json_response(
            {
                "error": "the config file could not be read to update it",
                "code": "config_unreadable",
                "detail": str(exc)[:200],
                # The key DID land in the vault; the backend did NOT switch.
                "applied": ["vault"],
            },
            status=500,
        )
    except Exception as exc:
        logger.warning("gateway-setup apply: config write failed", exc_info=True)
        return web.json_response(
            {
                "error": "could not write the gateway configuration",
                "code": "config_write_failed",
                "detail": str(exc)[:200],
                "applied": ["vault"],
            },
            status=500,
        )

    return web.json_response(
        {
            "ok": True,
            "applied": ["vault", "deepseek_env", "deepseek_base_url", "acp_backend"]
            + (["model"] if model else []),
            "acp_backend": _FOREIGN_BACKEND,
        }
    )


async def api_gateway_setup_switch(request: web.Request) -> web.Response:
    """POST /api/gateway-setup/switch — change the model backend after onboarding.

    Body: ``{"backend": "" | "deepseek"}``. This is the Settings-side switch the
    requirements call for (needs 2.6-2.8), distinct from first-run onboarding:

    * ``"" `` (→ Kiro): set ``agent.acp_backend = ""`` and KEEP the gateway config
      (vault key, ``deepseek_env``, ``deepseek_base_url``) so the user can switch
      back losslessly.
    * ``"deepseek"`` (→ own gateway): if the gateway is already configured (a
      ``deepseek_base_url`` AND the ``deepseek_env`` key mapping are present),
      flip ``acp_backend`` straight to it; otherwise flip NOTHING and answer
      ``needs_wizard`` so the client opens the setup wizard to collect the config
      first — never leave a "chosen but unconfigured" half-state.

    The switch changes persisted config only; it takes effect on the next gateway
    start / new session, so the client should re-check backend status and tell the
    user that rather than pretend a running session hot-switched.
    """
    denied = await _owner_only(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"}, status=400
        )
    backend = body.get("backend")
    if backend not in ("", _FOREIGN_BACKEND):
        return web.json_response(
            {
                "error": f"backend must be '' (Kiro) or '{_FOREIGN_BACKEND}'",
                "code": "invalid_backend",
            },
            status=400,
        )

    # Switching TO the gateway with no config yet: tell the client to run the
    # wizard, flip nothing. Read the current config off the loop.
    if backend == _FOREIGN_BACKEND:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        has_base = bool((cfg.agent.deepseek_base_url or "").strip())
        has_key = _VAULT_KEY_NAME in (cfg.agent.deepseek_env or {})
        if not (has_base and has_key):
            return web.json_response(
                {"ok": False, "code": "needs_wizard", "backend": _FOREIGN_BACKEND}
            )

    def _apply_switch(doc: dict) -> dict | None:
        agent = coerce_dict_section(doc, "agent")
        if agent.get("acp_backend", "") == backend:
            return None  # no change
        agent["acp_backend"] = backend
        # Switching to Kiro deliberately PRESERVES deepseek_env / deepseek_base_url
        # so a later switch back needs no re-entry.
        return doc

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply_switch)
    except ConfigReadError as exc:
        return web.json_response(
            {"error": "config unreadable", "code": "config_unreadable", "detail": str(exc)[:200]},
            status=500,
        )
    except Exception as exc:
        logger.warning("gateway-setup switch: config write failed", exc_info=True)
        return web.json_response(
            {"error": "could not write config", "code": "config_write_failed", "detail": str(exc)[:200]},
            status=500,
        )

    return web.json_response(
        {"ok": True, "backend": backend, "effective": "next_session"}
    )


def setup_gateway_setup_routes(app: web.Application) -> None:
    """Register /api/gateway-setup routes on the dashboard app."""
    app.router.add_post("/api/gateway-setup/probe-models", api_gateway_setup_probe_models)
    app.router.add_post("/api/gateway-setup/apply", api_gateway_setup_apply)
    app.router.add_post("/api/gateway-setup/switch", api_gateway_setup_switch)
