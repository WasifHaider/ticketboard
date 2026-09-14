import pytest

from ticketboard import db, digest


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = db.get_connection(str(db_path))
    db.apply_schema(c)
    yield c
    c.close()


def test_digest_empty_board(conn):
    text = digest.build_digest(conn)
    assert "Status counts" in text
    assert "No tickets resolved today." in text


def test_digest_counts_statuses(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t1 = db.create_ticket(conn, p["id"], "T1", "o", "a")
    t2 = db.create_ticket(conn, p["id"], "T2", "o", "a")
    db.update_ticket_status(conn, t1["id"], "ready")
    text = digest.build_digest(conn)
    assert "todo=1" in text
    assert "ready=1" in text


def test_digest_lists_resolved_today(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t = db.create_ticket(conn, p["id"], "Fix bug", "o", "a")
    db.update_ticket_status(conn, t["id"], "ready")
    db.update_ticket_status(conn, t["id"], "in_progress")
    db.update_ticket_status(conn, t["id"], "done")
    text = digest.build_digest(conn)
    assert "Tickets resolved today:" in text
    assert "Fix bug" in text
    assert "[DONE]" in text


def test_digest_flags_stale_in_progress(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t = db.create_ticket(conn, p["id"], "Stuck", "o", "a")
    db.update_ticket_status(conn, t["id"], "ready")
    db.update_ticket_status(conn, t["id"], "in_progress")
    conn.execute(
        "UPDATE tickets SET updated_at = datetime('now', '-5 hours') WHERE id = ?",
        (t["id"],),
    )
    conn.commit()
    text = digest.build_digest(conn, stale_hours=3)
    assert "WARNING" in text
    assert "Stuck" in text
