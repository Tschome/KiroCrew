"""Tasks 8b + 13 — gateway setup handler and harness base_url injection.

* ``apply`` writes the vault key, the deepseek_env mapping, deepseek_base_url,
  the optional model and flips acp_backend to deepseek, all in KiroCrew's own
  config layer (no dsh profile file).
* A vault failure is reported and the backend is NOT flipped.
* ``probe-models`` parses an OpenAI /models response and degrades on failure.
* The deepseek harness injects DEEPSEEK_BASE_URL from the config field only
  when it is set.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import gateway_setup as gs


def _owner_request(body: dict):
    """A request double that reads as the dashboard owner and returns *body*."""

    class _App(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    app = _App()
    app["state"] = SimpleNamespace(owner_id="")

    class _Req:
        def __init__(self) -> None:
            self.app = app
            self.path = "/api/gateway-setup/apply"
            self._claims = {"user": "local-app", "app": ""}

        async def json(self):
            return body

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __contains__(self, key):
            return key in self._claims

        def __getitem__(self, key):
            return self._claims[key]

    return _Req()


@pytest.mark.asyncio
async def test_apply_writes_all_targets(monkeypatch, tmp_path):
    # Isolate config.json and the vault under a temp config dir.
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.config_path", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.gateway_setup.config_dir", lambda: tmp_path
    )

    vault_writes: list[tuple[str, str]] = []

    class _Vault:
        def __init__(self, _dir):
            pass

        async def set_with_revision(self, name, value):
            vault_writes.append((name, value))
            return "rev1"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.SecretVault", _Vault)

    req = _owner_request(
        {
            "base_url": "http://10.3.20.46:4000/v1/",
            "api_key": "sk-test",
            "default_model": "deepseek-chat",
        }
    )
    resp = await gs.api_gateway_setup_apply(req)
    assert resp.status == 200
    out = json.loads(resp.body.decode())
    assert out["ok"] is True
    assert out["acp_backend"] == "deepseek"

    # Vault got the key.
    assert vault_writes == [("DEEPSEEK_API_KEY", "sk-test")]

    # Config got every field, base_url trailing slash trimmed.
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    agent = doc["agent"]
    assert agent["deepseek_env"]["DEEPSEEK_API_KEY"] == "secret://DEEPSEEK_API_KEY"
    assert agent["deepseek_base_url"] == "http://10.3.20.46:4000/v1"
    assert agent["model"] == "deepseek-chat"
    assert agent["acp_backend"] == "deepseek"


@pytest.mark.asyncio
async def test_apply_vault_failure_does_not_flip_backend(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.config_path", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.gateway_setup.config_dir", lambda: tmp_path
    )

    class _Vault:
        def __init__(self, _dir):
            pass

        async def set_with_revision(self, name, value):
            raise RuntimeError("vault locked")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.SecretVault", _Vault)

    req = _owner_request({"base_url": "http://x/v1", "api_key": "sk"})
    resp = await gs.api_gateway_setup_apply(req)
    assert resp.status == 500
    out = json.loads(resp.body.decode())
    assert out["code"] == "vault_write_failed"
    assert out["applied"] == []
    # Config untouched: no backend flip.
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert "agent" not in doc or doc["agent"].get("acp_backend", "") != "deepseek"


@pytest.mark.asyncio
async def test_apply_missing_fields_rejected(monkeypatch, tmp_path):
    req = _owner_request({"api_key": "sk"})  # no base_url
    resp = await gs.api_gateway_setup_apply(req)
    assert resp.status == 400
    assert json.loads(resp.body.decode())["code"] == "missing_base_url"


@pytest.mark.asyncio
async def test_apply_non_owner_denied(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.gateway_setup.is_owner_dashboard_request",
        lambda request: False,
    )
    req = _owner_request({"base_url": "http://x/v1", "api_key": "sk"})
    resp = await gs.api_gateway_setup_apply(req)
    assert resp.status == 403
    assert json.loads(resp.body.decode())["code"] == "owner_only"


# ── harness base_url injection (task 8b) ──


@pytest.mark.asyncio
async def test_harness_injects_base_url_when_set(monkeypatch):
    from kiro_crew.acp.harness import deepseek as dsh

    monkeypatch.setattr(dsh, "_deepseek_base_url", lambda: "http://gw:4000/v1")
    # Stub the vault-env resolver so prepare_spawn_env does not touch config/vault.
    monkeypatch.setattr(dsh, "_deepseek_vault_env", lambda: ({}, ()))

    adapter = dsh.DeepseekLaunch()
    env: dict[str, str] = {}

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)

    await adapter.prepare_spawn_env(env, to_thread=_to_thread)
    assert env.get("DEEPSEEK_BASE_URL") == "http://gw:4000/v1"


@pytest.mark.asyncio
async def test_harness_omits_base_url_when_unset(monkeypatch):
    from kiro_crew.acp.harness import deepseek as dsh

    monkeypatch.setattr(dsh, "_deepseek_base_url", lambda: "")
    monkeypatch.setattr(dsh, "_deepseek_vault_env", lambda: ({}, ()))

    adapter = dsh.DeepseekLaunch()
    env: dict[str, str] = {}

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)

    await adapter.prepare_spawn_env(env, to_thread=_to_thread)
    assert "DEEPSEEK_BASE_URL" not in env


# ── backend switch route (task 7) ──


@pytest.mark.asyncio
async def test_switch_to_kiro_keeps_gateway_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agent": {
                    "acp_backend": "deepseek",
                    "deepseek_base_url": "http://gw/v1",
                    "deepseek_env": {"DEEPSEEK_API_KEY": "secret://DEEPSEEK_API_KEY"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.config_path", lambda: cfg)

    req = _owner_request({"backend": ""})
    resp = await gs.api_gateway_setup_switch(req)
    assert resp.status == 200
    assert json.loads(resp.body.decode())["backend"] == ""
    agent = json.loads(cfg.read_text(encoding="utf-8"))["agent"]
    # Backend flipped to Kiro, but gateway config preserved for lossless switch-back.
    assert agent["acp_backend"] == ""
    assert agent["deepseek_base_url"] == "http://gw/v1"
    assert agent["deepseek_env"]["DEEPSEEK_API_KEY"] == "secret://DEEPSEEK_API_KEY"


@pytest.mark.asyncio
async def test_switch_to_gateway_unconfigured_needs_wizard(monkeypatch):
    # No base_url / key configured → needs_wizard, flip nothing.
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        staticmethod(
            lambda: SimpleNamespace(
                agent=SimpleNamespace(deepseek_base_url="", deepseek_env={})
            )
        ),
        raising=True,
    )
    req = _owner_request({"backend": "deepseek"})
    resp = await gs.api_gateway_setup_switch(req)
    assert resp.status == 200
    out = json.loads(resp.body.decode())
    assert out["ok"] is False
    assert out["code"] == "needs_wizard"


@pytest.mark.asyncio
async def test_switch_to_gateway_configured_flips(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agent": {
                    "acp_backend": "",
                    "deepseek_base_url": "http://gw/v1",
                    "deepseek_env": {"DEEPSEEK_API_KEY": "secret://DEEPSEEK_API_KEY"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.gateway_setup.config_path", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        staticmethod(
            lambda: SimpleNamespace(
                agent=SimpleNamespace(
                    deepseek_base_url="http://gw/v1",
                    deepseek_env={"DEEPSEEK_API_KEY": "secret://DEEPSEEK_API_KEY"},
                )
            )
        ),
        raising=True,
    )
    req = _owner_request({"backend": "deepseek"})
    resp = await gs.api_gateway_setup_switch(req)
    assert resp.status == 200
    assert json.loads(resp.body.decode())["backend"] == "deepseek"
    assert json.loads(cfg.read_text(encoding="utf-8"))["agent"]["acp_backend"] == "deepseek"


@pytest.mark.asyncio
async def test_switch_invalid_backend_rejected():
    req = _owner_request({"backend": "claude"})
    resp = await gs.api_gateway_setup_switch(req)
    assert resp.status == 400
    assert json.loads(resp.body.decode())["code"] == "invalid_backend"
