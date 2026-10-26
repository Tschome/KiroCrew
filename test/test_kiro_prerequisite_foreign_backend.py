"""Task 1.2 — foreign-backend early return in ``api_kiro_prerequisite_status``.

The desktop "own gateway" path sets ``agent.acp_backend`` to a foreign id
(e.g. ``deepseek``). In that case the status handler must short-circuit to a
ready snapshot carrying ``foreign_backend`` WITHOUT ever running the kiro-cli
probe (``service.snapshot``), so a user who chose not to log in to Kiro never
sees the "could not check Kiro CLI" / login gate.

The default kiro backend (``acp_backend == ""``) must fall through to the
existing probe path unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import kiro_prerequisite as kp


class _Snapshot:
    """A service stub whose .snapshot() must NOT be called on the foreign path."""

    def __init__(self) -> None:
        self.initial_setup_complete = True
        self.snapshot_calls = 0

    async def snapshot(self, *, force: bool = False, coalesce: bool = False):
        self.snapshot_calls += 1
        # Shaped like the real not-ready/ready snapshot; the test asserts this
        # is returned verbatim for the kiro path and never reached for foreign.
        return {
            "platform": "gateway",
            "installed": True,
            "authenticated": True,
            "ready": True,
            "repair_required": False,
            "initial_setup_complete": True,
        }


def _request(app_service, *, backend: str, monkeypatch):
    """Build an owner request double wired to *app_service* and *backend* config."""

    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        staticmethod(lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))),
        raising=True,
    )

    class _App(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    app = _App()
    app["kiro_prerequisite_service"] = app_service
    app["state"] = SimpleNamespace(owner_id="")

    class _Req:
        def __init__(self) -> None:
            self.app = app
            self.query = {}
            self.path = "/api/kiro-prerequisite"
            self._claims = {"user": "local-app", "app": ""}

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __contains__(self, key):
            return key in self._claims

        def __getitem__(self, key):
            return self._claims[key]

    return _Req()


@pytest.mark.asyncio
async def test_foreign_backend_short_circuits_without_probe(monkeypatch):
    svc = _Snapshot()
    # _service() requires a real KiroPrerequisiteService instance; bypass by
    # patching the resolver to accept our stub.
    monkeypatch.setattr(kp, "_service", lambda request: request.app["kiro_prerequisite_service"])
    req = _request(svc, backend="deepseek", monkeypatch=monkeypatch)

    resp = await kp.api_kiro_prerequisite_status(req)

    import json

    body = json.loads(resp.body.decode())
    assert body["ready"] is True
    assert body["foreign_backend"] == "deepseek"
    assert body["repair_required"] is False
    # The whole point: the kiro-cli probe is never run on the foreign path.
    assert svc.snapshot_calls == 0


@pytest.mark.asyncio
async def test_kiro_backend_falls_through_to_probe(monkeypatch):
    svc = _Snapshot()
    monkeypatch.setattr(kp, "_service", lambda request: request.app["kiro_prerequisite_service"])
    req = _request(svc, backend="", monkeypatch=monkeypatch)

    resp = await kp.api_kiro_prerequisite_status(req)

    import json

    body = json.loads(resp.body.decode())
    # Default kiro path: the probe ran exactly once and there is no
    # foreign_backend key in the owner payload.
    assert svc.snapshot_calls == 1
    assert "foreign_backend" not in body
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_config_read_failure_falls_through_to_kiro_path(monkeypatch):
    svc = _Snapshot()
    monkeypatch.setattr(kp, "_service", lambda request: request.app["kiro_prerequisite_service"])

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(_boom), raising=True
    )

    class _App(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    app = _App()
    app["kiro_prerequisite_service"] = svc
    app["state"] = SimpleNamespace(owner_id="")

    class _Req:
        def __init__(self) -> None:
            self.app = app
            self.query = {}
            self.path = "/api/kiro-prerequisite"
            self._claims = {"user": "local-app", "app": ""}

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __contains__(self, key):
            return key in self._claims

        def __getitem__(self, key):
            return self._claims[key]

    resp = await kp.api_kiro_prerequisite_status(_Req())

    import json

    body = json.loads(resp.body.decode())
    # A config read failure must not be fatal: fall through to the probe path.
    assert svc.snapshot_calls == 1
    assert "foreign_backend" not in body
