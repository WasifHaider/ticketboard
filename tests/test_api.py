import pytest
from fastapi.testclient import TestClient

from ticketboard.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app(db_path=str(db_path))
    with TestClient(app) as c:
        yield c


def test_create_and_list_project(client):
    resp = client.post("/projects", json={
        "name": "demo", "repo_path": "/tmp/demo", "test_command": "pytest -q",
    })
    assert resp.status_code == 200
    project = resp.json()
    assert project["name"] == "demo"

    resp2 = client.get("/projects")
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1


def test_get_project_404(client):
    resp = client.get("/projects/999")
    assert resp.status_code == 404


def test_bulk_create_tickets(client):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    resp = client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [
            {"title": "T1", "objective": "o1", "acceptance_criteria": "a1"},
            {"title": "T2", "objective": "o2", "acceptance_criteria": "a2"},
        ]
    })
    assert resp.status_code == 200
    tickets = resp.json()
    assert len(tickets) == 2
    assert tickets[0]["status"] == "todo"
    assert tickets[0]["order_index"] == 0
    assert tickets[1]["order_index"] == 1


def test_bulk_create_tickets_404_project(client):
    resp = client.post("/projects/999/tickets", json={"tickets": []})
    assert resp.status_code == 404


def test_list_and_get_ticket(client):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    })
    resp = client.get("/tickets")
    assert resp.status_code == 200
    tickets = resp.json()
    assert len(tickets) == 1

    ticket_id = tickets[0]["id"]
    detail = client.get(f"/tickets/{ticket_id}").json()
    assert detail["id"] == ticket_id
    assert detail["events"] == []


def test_list_tickets_filter_by_status_and_project(client):
    p1 = client.post("/projects", json={"name": "p1", "repo_path": "/p1"}).json()
    p2 = client.post("/projects", json={"name": "p2", "repo_path": "/p2"}).json()
    client.post(f"/projects/{p1['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    })
    client.post(f"/projects/{p2['id']}/tickets", json={
        "tickets": [{"title": "T2", "objective": "o", "acceptance_criteria": "a"}]
    })
    resp = client.get("/tickets", params={"project_id": p1["id"]})
    assert len(resp.json()) == 1
    resp2 = client.get("/tickets", params={"status": "todo"})
    assert len(resp2.json()) == 2
    resp3 = client.get("/tickets", params={"status": "ready"})
    assert len(resp3.json()) == 0


def test_patch_ticket_status_legal(client):
    from unittest.mock import patch as mock_patch
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    }).json()[0]

    with mock_patch("ticketboard.routes.tickets.trigger_worker_async") as mock_trigger:
        resp = client.patch(f"/tickets/{ticket['id']}", json={"status": "ready"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    mock_trigger.assert_called_once()


def test_patch_ticket_status_to_ready_triggers_worker(client):
    from unittest.mock import patch as mock_patch
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    }).json()[0]

    with mock_patch("ticketboard.routes.tickets.trigger_worker_async") as mock_trigger:
        client.patch(f"/tickets/{ticket['id']}", json={"status": "ready"})
        mock_trigger.assert_called_once()

        # non-ready transitions must NOT re-trigger the worker
        mock_trigger.reset_mock()
        client.patch(f"/tickets/{ticket['id']}", json={"status": "todo"})
        mock_trigger.assert_not_called()


def test_patch_ticket_status_illegal_returns_400(client):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    }).json()[0]

    resp = client.patch(f"/tickets/{ticket['id']}", json={"status": "done"})
    assert resp.status_code == 400


def test_patch_ticket_404(client):
    resp = client.patch("/tickets/999", json={"status": "ready"})
    assert resp.status_code == 404


def test_create_event(client):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/tickets", json={
        "tickets": [{"title": "T1", "objective": "o", "acceptance_criteria": "a"}]
    }).json()[0]

    resp = client.post(f"/tickets/{ticket['id']}/events", json={
        "event_type": "comment", "message": "hello",
    })
    assert resp.status_code == 200

    detail = client.get(f"/tickets/{ticket['id']}").json()
    assert len(detail["events"]) == 1
    assert detail["events"][0]["message"] == "hello"


def test_create_event_404(client):
    resp = client.post("/tickets/999/events", json={
        "event_type": "comment", "message": "hi",
    })
    assert resp.status_code == 404


def test_create_single_ticket(client):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    resp = client.post(f"/projects/{project['id']}/ticket", json={
        "title": "T1", "objective": "o", "acceptance_criteria": "a",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "todo"


def test_create_single_ticket_404_project(client):
    resp = client.post("/projects/999/ticket", json={
        "title": "T1", "objective": "o", "acceptance_criteria": "a",
    })
    assert resp.status_code == 404


def test_upload_attachment(client, tmp_path):
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/ticket", json={
        "title": "T1", "objective": "o", "acceptance_criteria": "a",
    }).json()

    f = tmp_path / "spec.txt"
    f.write_text("here is the spec")

    resp = client.post(
        f"/tickets/{ticket['id']}/attachments",
        files={"file": ("spec.txt", f.open("rb"), "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "spec.txt"

    detail = client.get(f"/tickets/{ticket['id']}").json()
    assert len(detail["attachments"]) == 1


def test_upload_attachment_404_ticket(client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    resp = client.post(
        "/tickets/999/attachments",
        files={"file": ("a.txt", f.open("rb"), "text/plain")},
    )
    assert resp.status_code == 404


def test_chat_message_flow(client):
    from unittest.mock import patch as mock_patch
    project = client.post("/projects", json={"name": "p", "repo_path": "/p"}).json()
    ticket = client.post(f"/projects/{project['id']}/ticket", json={
        "title": "T1", "objective": "o", "acceptance_criteria": "a",
    }).json()

    from ticketboard import chat as chat_module
    with mock_patch.object(chat_module, "run_claude_chat",
                            return_value={"success": True, "reply": "sure, sounds good",
                                          "stderr": ""}):
        resp = client.post(f"/tickets/{ticket['id']}/messages", json={
            "message": "should we also handle X?",
        })
    assert resp.status_code == 200
    assert resp.json()["reply"] == "sure, sounds good"

    messages_resp = client.get(f"/tickets/{ticket['id']}/messages")
    assert len(messages_resp.json()) == 2


def test_chat_message_404_ticket(client):
    resp = client.post("/tickets/999/messages", json={"message": "hi"})
    assert resp.status_code == 404


def test_worker_status_no_heartbeat_yet(client):
    resp = client.get("/worker/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_heartbeat"] is None
    assert body["stale"] is True


def test_worker_status_fresh_heartbeat(tmp_path):
    from ticketboard import db as db_module
    from ticketboard.main import create_app
    from fastapi.testclient import TestClient

    db_path = tmp_path / "heartbeat_test.db"
    app = create_app(db_path=str(db_path))
    with TestClient(app) as c:
        # simulate the worker process's own connection recording a heartbeat
        worker_conn = db_module.get_connection(str(db_path))
        db_module.heartbeat(worker_conn)
        worker_conn.close()

        resp = c.get("/worker/status")
        body = resp.json()
        assert body["last_heartbeat"] is not None
        assert body["stale"] is False


def test_recent_events_empty(client):
    resp = client.get("/events/recent")
    assert resp.status_code == 200
    assert resp.json() == []


def test_recent_events_cross_project(client):
    p1 = client.post("/projects", json={"name": "p1", "repo_path": "/p1"}).json()
    p2 = client.post("/projects", json={"name": "p2", "repo_path": "/p2"}).json()
    t1 = client.post(f"/projects/{p1['id']}/ticket", json={
        "title": "T1", "objective": "o", "acceptance_criteria": "a",
    }).json()
    t2 = client.post(f"/projects/{p2['id']}/ticket", json={
        "title": "T2", "objective": "o", "acceptance_criteria": "a",
    }).json()
    client.post(f"/tickets/{t1['id']}/events", json={
        "event_type": "comment", "message": "event on t1",
    })
    client.post(f"/tickets/{t2['id']}/events", json={
        "event_type": "comment", "message": "event on t2",
    })

    resp = client.get("/events/recent")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 2
    assert {e["ticket_title"] for e in events} == {"T1", "T2"}
