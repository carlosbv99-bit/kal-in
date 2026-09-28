"""
Tests de agent_core/routers/android_build.py — auditoría de resultado
real de android_build_and_screenshot (ver
tool_integration/adapters/vscode_android.py). No pasa por ningún
Access Manager: solo deja constancia en el audit log de lo que la
extensión de VS Code reportó.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.orchestrator import app
from audit.audit_log import audit_log

client = TestClient(app, base_url="http://localhost")


def test_installed_outcome_is_recorded_as_success():
    calls = []
    original_record = audit_log.record
    audit_log.record = lambda event: calls.append(event) or original_record(event)
    try:
        response = client.post(
            "/android-build/11111111-1111-4111-8111-111111111111/report-outcome",
            json={"outcome": "installed", "detail": "app-debug.apk instalado en Pixel_7"},
        )
    finally:
        audit_log.record = original_record

    assert response.status_code == 200
    assert calls[0].event_type == "android_build_completed"
    assert calls[0].outcome == "success"
    assert calls[0].context["request_id"] == "11111111-1111-4111-8111-111111111111"


def test_build_failed_outcome_is_recorded_as_failure():
    calls = []
    original_record = audit_log.record
    audit_log.record = lambda event: calls.append(event) or original_record(event)
    try:
        response = client.post(
            "/android-build/22222222-2222-4222-8222-222222222222/report-outcome",
            json={"outcome": "build_failed", "detail": "error: unresolved reference 'foo'"},
        )
    finally:
        audit_log.record = original_record

    assert response.status_code == 200
    assert calls[0].event_type == "android_build_failed"
    assert calls[0].outcome == "failure"


def test_no_device_outcome_is_recorded_as_failure():
    response = client.post("/android-build/33333333-3333-4333-8333-333333333333/report-outcome", json={"outcome": "no_device", "detail": ""})
    assert response.status_code == 200
    assert response.json() == {"id": "33333333-3333-4333-8333-333333333333", "outcome": "no_device"}


def test_response_echoes_request_id_and_outcome():
    response = client.post("/android-build/44444444-4444-4444-8444-444444444444/report-outcome", json={"outcome": "discarded"})
    assert response.status_code == 200
    assert response.json() == {"id": "44444444-4444-4444-8444-444444444444", "outcome": "discarded"}
