import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.function_logic import ACTOR_LAMBDA, TENANT_CONFIRM_PATH, FunctionBackend  # noqa: E402


EVENT_ID = "11111111-2222-4333-8444-555555555555"


def _event(args=None, extra_params=None, prompt=""):
    params = {"tool_calls": [{"args": args or {}}], "user_phone_number": "+56950570324"}
    if extra_params:
        params.update(extra_params)
    return SimpleNamespace(
        event_id=EVENT_ID,
        prompt=prompt,
        extra_params=params,
        organization=SimpleNamespace(
            organization_id="99999999-aaaa-4bbb-8ccc-dddddddddddd"
        ),
        branch="test",
        access_token="access-token",
    )


def test_confirm_reply_posts_availability_to_tenant_api(monkeypatch):
    calls = []
    backend = FunctionBackend(_event(args={"respuesta": "Confirmar"}))

    def fake_post(path, payload):
        calls.append({"path": path, "payload": payload})
        return {"driver": {"id": "driver-1"}, "idempotent_noop": False}

    monkeypatch.setattr(backend, "_post_tenant_api", fake_post)

    result = json.loads(backend.process_request())

    assert result["confirmed"] is True
    assert calls == [
        {
            "path": TENANT_CONFIRM_PATH,
            "payload": {
                "driver_phone": "+56950570324",
                "orchestration_event_uuid": EVENT_ID,
                "actor_lambda": ACTOR_LAMBDA,
            },
        }
    ]


def test_decline_reply_leaves_driver_unconfirmed(monkeypatch):
    backend = FunctionBackend(_event(args={"respuesta": "Rechazar"}))

    def fail_post(*args, **kwargs):
        raise AssertionError("decline must not call Tenant API")

    monkeypatch.setattr(backend, "_post_tenant_api", fail_post)

    result = json.loads(backend.process_request())

    assert result["confirmed"] is False
    assert result["declined"] is True


def test_confirm_requires_driver_phone():
    backend = FunctionBackend(
        _event(args={"respuesta": "Confirmar"}, extra_params={"user_phone_number": ""})
    )

    try:
        backend.process_request()
    except ValueError as exc:
        assert "driver_phone" in str(exc)
    else:
        raise AssertionError("expected missing driver phone error")
