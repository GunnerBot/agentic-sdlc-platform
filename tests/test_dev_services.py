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


def test_local_dev_multica_compatible_issue_agent_task_flow() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer local-dev-multica-key"}

    runtimes_response = client.get("/api/runtimes", headers=headers)
    assert runtimes_response.status_code == 200
    runtime_id = runtimes_response.json()[0]["id"]

    agent_response = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "agentic-sdlc-codex",
            "runtime_id": runtime_id,
        },
    )
    assert agent_response.status_code == 201
    agent_id = agent_response.json()["id"]

    issue_response = client.post(
        "/api/issues",
        headers=headers,
        json={
            "title": "Implement DAG node",
            "description": "Execute this node.",
            "assignee_type": "agent",
            "assignee_id": agent_id,
        },
    )
    assert issue_response.status_code == 201
    issue_id = issue_response.json()["id"]

    task_runs_response = client.get(
        f"/api/issues/{issue_id}/task-runs",
        headers=headers,
    )
    assert task_runs_response.status_code == 200
    assert task_runs_response.json()[0]["agent_id"] == agent_id

    comment_response = client.post(
        f"/api/issues/{issue_id}/comments",
        headers=headers,
        json={"content": "queued"},
    )
    assert comment_response.status_code == 201

    comments_response = client.get(f"/api/issues/{issue_id}/comments", headers=headers)
    assert comments_response.status_code == 200
    assert comments_response.json()[0]["content"] == "queued"
