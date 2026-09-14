from unittest.mock import patch

import pytest

from ticketboard import chat, db


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = db.get_connection(str(db_path))
    db.apply_schema(c)
    yield c
    c.close()


def make_ticket(conn, repo_path="/tmp/repo"):
    p = db.create_project(conn, "demo", repo_path)
    t = db.create_ticket(conn, p["id"], "T1", "do the thing", "criteria")
    return p, t


def test_build_chat_prompt_includes_ticket_fields(conn):
    p, t = make_ticket(conn)
    prompt = chat.build_chat_prompt(conn, dict(t))
    assert "do the thing" in prompt
    assert "criteria" in prompt


def test_build_chat_prompt_includes_history(conn):
    p, t = make_ticket(conn)
    db.add_message(conn, t["id"], "user", "why do we need this?")
    db.add_message(conn, t["id"], "assistant", "because X")
    prompt = chat.build_chat_prompt(conn, dict(t))
    assert "why do we need this?" in prompt
    assert "because X" in prompt


def test_build_chat_prompt_includes_attachment_text(conn, tmp_path):
    p, t = make_ticket(conn)
    f = tmp_path / "notes.txt"
    f.write_text("important context here")
    db.add_attachment(conn, t["id"], "notes.txt", str(f), "text/plain")
    prompt = chat.build_chat_prompt(conn, dict(t))
    assert "important context here" in prompt


def test_send_chat_message_records_both_turns_and_returns_reply(conn):
    p, t = make_ticket(conn)
    with patch.object(chat, "run_claude_chat",
                       return_value={"success": True, "reply": "here's my answer",
                                     "stderr": ""}):
        result = chat.send_chat_message(conn, t["id"], "what about X?")

    assert result["success"] is True
    assert result["reply"] == "here's my answer"

    messages = db.list_messages(conn, t["id"])
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["message"] == "what about X?"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["message"] == "here's my answer"


def test_send_chat_message_records_error_on_failure(conn):
    p, t = make_ticket(conn)
    with patch.object(chat, "run_claude_chat",
                       return_value={"success": False, "reply": "", "stderr": "boom"}):
        result = chat.send_chat_message(conn, t["id"], "hello")

    assert result["success"] is False
    messages = db.list_messages(conn, t["id"])
    assert len(messages) == 2
    assert "boom" in messages[1]["message"]


def test_send_chat_message_unknown_ticket_raises(conn):
    with pytest.raises(ValueError):
        chat.send_chat_message(conn, 999, "hello")
