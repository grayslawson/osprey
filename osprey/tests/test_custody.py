"""custody.py (used by handoff.sh) against a fake Protect client; no uiprotect needed."""

from __future__ import annotations

from typing import Any

import pytest

import custody

CAMERAS = [
    {"id": "c1", "mac": "AABBCCDDEEFF", "name": "gate", "isAdopted": True, "state": "CONNECTED"},
    {"id": "c2", "mac": "112233445566", "name": "yard", "isAdopted": False, "state": "DISCONNECTED"},
]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def update(self) -> None:
        pass

    async def api_request_obj(self, path: str) -> dict[str, Any]:
        assert path == "bootstrap"
        return {"cameras": CAMERAS}

    async def unadopt_device(self, model: object, identifier: str) -> None:
        self.calls.append(("unadopt", identifier))

    async def adopt_device(self, model: object, identifier: str) -> None:
        self.calls.append(("adopt", identifier))

    async def close_session(self) -> None:
        self.closed = True


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(custody, "_client", lambda *a: fake)
    monkeypatch.setattr(custody, "_camera_model", lambda: "camera")
    monkeypatch.setattr(custody, "SETTLE_SEC", 0.0)
    return fake


def _run(action: str, *extra: str) -> int:
    return custody.main([action, "--host", "h", "--user", "u", "--password", "p", *extra])


def test_release_only_touches_adopted_cameras(client: FakeClient) -> None:
    assert _run("release") == 0
    assert client.calls == [("unadopt", "c1")] and client.closed


def test_restore_only_touches_released_cameras(client: FakeClient) -> None:
    assert _run("restore") == 0
    assert client.calls == [("adopt", "c2")]


def test_mac_filter_and_no_match(client: FakeClient) -> None:
    assert _run("restore", "--mac", "aa:bb:cc:dd:ee:ff") == 0
    assert client.calls == []  # that camera is already adopted
    assert _run("release", "--mac", "00:00:00:00:00:00") == 1
    assert client.closed


def test_status_changes_nothing(client: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("status") == 0
    assert client.calls == []
    out = capsys.readouterr().out
    assert "AABBCCDDEEFF" in out and "adopted=False" in out
