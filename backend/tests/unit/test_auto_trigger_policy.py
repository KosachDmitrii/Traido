"""Auto-trigger toggle — persisted operator setting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading import auto_trigger_policy as atp


@pytest.fixture(autouse=True)
def _isolate_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto_trigger.json"
    store: dict[str, dict[str, str]] = {}

    class _FakeRedis:
        def hget(self, key, field):
            raw = store.get(key, {}).get(field)
            return raw.encode() if raw is not None else None

        def hset(self, key, mapping):
            store[key] = {str(k): str(v) for k, v in mapping.items()}
            return True

        def ping(self):
            return True

    monkeypatch.setattr(atp, "POLICY_PATH", path)
    monkeypatch.setattr(atp, "_redis_client", lambda: _FakeRedis())
    atp.reset_auto_trigger_cache()
    yield
    atp.reset_auto_trigger_cache()


def test_default_off() -> None:
    assert atp.get_auto_trigger_enabled() is False
    payload = atp.policy_payload()
    assert payload["enabled"] is False
    assert "note" in payload


def test_set_persists_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto_trigger.json"
    monkeypatch.setattr(atp, "POLICY_PATH", path)
    atp.set_auto_trigger_enabled(True, actor="test")
    assert atp.get_auto_trigger_enabled() is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["enabled"] is True
    assert raw["actor"] == "test"


def test_user_file_beats_test_redis_even_when_redis_is_newer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto_trigger.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "actor": "user",
                "updated_at": "2026-09-03T15:56:52+00:00",
            }
        ),
        encoding="utf-8",
    )
    store: dict[str, dict[str, str]] = {
        atp.REDIS_KEY: {
            "enabled": "0",
            "actor": "test",
            "updated_at": "2026-09-03T16:10:37+00:00",
        }
    }

    class _FakeRedis:
        def hget(self, key, field):
            raw = store.get(key, {}).get(field)
            return raw.encode() if raw is not None else None

        def hset(self, key, mapping):
            store[key] = {str(k): str(v) for k, v in mapping.items()}
            return True

        def ping(self):
            return True

    monkeypatch.setattr(atp, "POLICY_PATH", path)
    monkeypatch.setattr(atp, "_redis_client", lambda: _FakeRedis())
    atp.reset_auto_trigger_cache()
    assert atp.get_auto_trigger_enabled() is True
    assert store[atp.REDIS_KEY]["enabled"] == "1"
    assert store[atp.REDIS_KEY]["actor"] == "user"
