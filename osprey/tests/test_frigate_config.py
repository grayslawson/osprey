"""The Frigate URL store: validation and atomic persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frigate_config import Store, validate_url


def test_validate_url_normalises() -> None:
    assert validate_url("  http://frigate.lan:5000/ ") == "http://frigate.lan:5000"


@pytest.mark.parametrize("bad", [
    "ftp://frigate", "frigate:5000", "http://", "http://u:p@frigate", "http://frigate?x=1",
    "http://frigate#f", "http://frigate:99999", "http://frigate:0",
])
def test_validate_url_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_url(bad)


def test_store_round_trip_and_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "sub" / "frigate.json")
    assert store.load() is None
    assert store.save("http://frigate:5000/") == "http://frigate:5000"
    assert store.load() == "http://frigate:5000"
    assert store.save(None) is None
    assert store.load() is None
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["frigate.json"]  # no temp files left


def test_store_rejects_invalid_and_keeps_old_value(tmp_path: Path) -> None:
    store = Store(tmp_path / "frigate.json")
    store.save("http://a:1")
    with pytest.raises(ValueError):
        store.save("javascript:alert(1)")
    assert store.load() == "http://a:1"


@pytest.mark.parametrize("content", ["not json", '{"base_url": 5}', '{"base_url": "ftp://x"}', "[]"])
def test_load_ignores_a_damaged_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "frigate.json"
    path.write_text(content)
    assert Store(path).load() is None


def test_store_without_a_path_is_memory_only_and_env_selects_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Store().save("http://a:1") == "http://a:1"
    path = tmp_path / "f.json"
    monkeypatch.setenv("OSPREY_FRIGATE_CONFIG_FILE", str(path))
    Store.from_environment().save("http://b:2")
    assert json.loads(path.read_text()) == {"base_url": "http://b:2"}
