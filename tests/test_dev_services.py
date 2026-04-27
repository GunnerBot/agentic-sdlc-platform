from fastapi.testclient import TestClient

from agentic_sdlc_platform.dev_services import app


def test_local_dev_hermes_contract_requires_token() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/sessions/ask",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "what is this repo?",
            "repo": "atlas-tech-inc/keychain-os-erp",
        },
    )

    assert response.status_code == 401


def test_local_dev_hermes_contract_answers_questions() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/sessions/ask",
        headers={"Authorization": "Bearer local-dev-hermes-key"},
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "what is this repo?",
            "repo": "atlas-tech-inc/keychain-os-erp",
        },
    )

    assert response.status_code == 200
    assert response.json()["session_id"].startswith("dev-hermes-session-")
    assert response.json()["message_id"].startswith("dev-hermes-message-")
    assert "atlas-tech-inc/keychain-os-erp" in response.json()["answer"]


def test_local_dev_multica_contract_creates_and_updates_tasks() -> None:
    client = TestClient(app)

    create_response = client.post(
        "/api/tasks",
        headers={"Authorization": "Bearer local-dev-multica-key"},
        json={
            "source": "linear",
            "external_id": "OS-123",
            "title": "Fix allocation bug",
            "repo": "atlas-tech-inc/keychain-os-erp",
            "metadata": {"priority": "high"},
        },
    )

    assert create_response.status_code == 201
    task_id = create_response.json()["id"]
    assert create_response.json()["status"] == "queued"

    update_response = client.patch(
        f"/api/tasks/{task_id}",
        headers={"Authorization": "Bearer local-dev-multica-key"},
        json={"status": "completed", "metadata": {"pr": 12}},
    )

    assert update_response.status_code == 200
    assert update_response.json()["id"] == task_id
    assert update_response.json()["status"] == "completed"
    assert update_response.json()["metadata"] == {"pr": 12}
