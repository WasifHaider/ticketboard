import sqlite3

import pytest

from ticketboard import db


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = db.get_connection(str(db_path))
    db.apply_schema(c)
    yield c
    c.close()


def test_apply_schema_creates_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "tickets", "ticket_events", "worker_state"} <= tables


def test_apply_schema_migrates_pre_existing_db_missing_new_columns(tmp_path):
    """Simulates a db created before worktree_path/branch_name/last_heartbeat
    existed — apply_schema must ALTER TABLE to add them without wiping data."""
    db_path = tmp_path / "old.db"
    old_conn = db.get_connection(str(db_path))
    old_conn.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            repo_path TEXT NOT NULL, spec_summary TEXT, test_command TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            title TEXT NOT NULL, objective TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, files_hint TEXT,
            status TEXT NOT NULL DEFAULT 'todo', order_index INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE ticket_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE worker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1), locked_ticket_id INTEGER
        );
        INSERT OR IGNORE INTO worker_state (id, locked_ticket_id) VALUES (1, NULL);
    """)
    old_conn.execute(
        "INSERT INTO projects (name, repo_path) VALUES ('preexisting', '/x')"
    )
    old_conn.commit()
    old_conn.close()

    migrated_conn = db.get_connection(str(db_path))
    db.apply_schema(migrated_conn)  # must not raise, must not drop data

    project = db.list_projects(migrated_conn)[0]
    assert project["name"] == "preexisting"

    # new columns must exist and be usable now
    assert db.get_last_heartbeat(migrated_conn) is None
    db.heartbeat(migrated_conn)
    assert db.get_last_heartbeat(migrated_conn) is not None


def test_apply_schema_migrates_pre_needs_review_status_check(tmp_path):
    """A db created before 'needs_review' was added to the status CHECK
    would silently reject any transition into that status even after
    schema.sql is updated, because CREATE TABLE IF NOT EXISTS leaves the old
    CHECK baked into the existing table. apply_schema must detect and rebuild
    the table with the new CHECK, preserving all rows including their
    original status values."""
    db_path = tmp_path / "old_check.db"
    old_conn = db.get_connection(str(db_path))
    old_conn.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            repo_path TEXT NOT NULL, spec_summary TEXT, test_command TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            title TEXT NOT NULL, objective TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, files_hint TEXT,
            status TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo','ready','in_progress','done','failed')),
            order_index INTEGER NOT NULL,
            worktree_path TEXT, branch_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE ticket_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE worker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1), locked_ticket_id INTEGER
        );
        INSERT OR IGNORE INTO worker_state (id, locked_ticket_id) VALUES (1, NULL);
    """)
    old_conn.execute(
        "INSERT INTO projects (id, name, repo_path) VALUES (1, 'p', '/x')"
    )
    old_conn.execute(
        "INSERT INTO tickets (project_id, title, objective, acceptance_criteria, "
        "order_index, status) VALUES (1, 'old ticket', 'obj', 'ac', 0, 'done')"
    )
    old_conn.commit()
    old_conn.close()

    migrated_conn = db.get_connection(str(db_path))
    db.apply_schema(migrated_conn)  # must rebuild the table, not raise

    tickets = db.list_tickets(migrated_conn)
    assert len(tickets) == 1
    assert tickets[0]["title"] == "old ticket"
    assert tickets[0]["status"] == "done"  # data preserved through rebuild

    # the new status value must now be accepted where it previously wasn't
    updated = db.update_ticket_status(migrated_conn, tickets[0]["id"], "ready", force=True)
    updated = db.update_ticket_status(migrated_conn, tickets[0]["id"], "in_progress", force=True)
    updated = db.update_ticket_status(migrated_conn, tickets[0]["id"], "needs_review", force=True)
    assert updated["status"] == "needs_review"

    # regression: SQLite's ALTER TABLE RENAME silently rewrites OTHER
    # tables' FK schema text to point at the renamed table unless
    # legacy_alter_table is set — verify ticket_events (and friends) still
    # reference the live "tickets" table, not the dropped
    # "tickets_old_migration", by actually inserting through the FK path.
    event = db.add_event(migrated_conn, tickets[0]["id"], "comment", "post-migration event")
    assert event is not None
    events = db.list_events(migrated_conn, tickets[0]["id"])
    assert len(events) == 1
    assert events[0]["message"] == "post-migration event"


def test_create_and_get_project(conn):
    p = db.create_project(conn, "demo", "/tmp/demo", "a spec", "pytest -q")
    assert p["name"] == "demo"
    assert p["repo_path"] == "/tmp/demo"
    assert p["test_command"] == "pytest -q"
    fetched = db.get_project(conn, p["id"])
    assert fetched["id"] == p["id"]


def test_list_projects(conn):
    db.create_project(conn, "a", "/a")
    db.create_project(conn, "b", "/b")
    projects = db.list_projects(conn)
    assert len(projects) == 2


def test_create_ticket_assigns_sequential_order_index(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t1 = db.create_ticket(conn, p["id"], "T1", "obj1", "ac1")
    t2 = db.create_ticket(conn, p["id"], "T2", "obj2", "ac2")
    assert t1["order_index"] == 0
    assert t2["order_index"] == 1
    assert t1["status"] == "todo"


def test_list_tickets_filters_by_status_and_project(conn):
    p1 = db.create_project(conn, "p1", "/p1")
    p2 = db.create_project(conn, "p2", "/p2")
    t1 = db.create_ticket(conn, p1["id"], "T1", "o", "a")
    db.create_ticket(conn, p2["id"], "T2", "o", "a")
    db.update_ticket_status(conn, t1["id"], "ready")

    assert len(db.list_tickets(conn)) == 2
    assert len(db.list_tickets(conn, project_id=p1["id"])) == 1
    assert len(db.list_tickets(conn, status="ready")) == 1
    assert len(db.list_tickets(conn, status="todo")) == 1


def test_update_ticket_status_legal_transition(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t = db.create_ticket(conn, p["id"], "T1", "o", "a")
    updated = db.update_ticket_status(conn, t["id"], "ready")
    assert updated["status"] == "ready"


def test_update_ticket_status_illegal_transition_raises(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t = db.create_ticket(conn, p["id"], "T1", "o", "a")
    with pytest.raises(db.IllegalTransitionError):
        db.update_ticket_status(conn, t["id"], "done")


def test_add_and_list_events(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t = db.create_ticket(conn, p["id"], "T1", "o", "a")
    db.add_event(conn, t["id"], "start", "began work")
    db.add_event(conn, t["id"], "finish", "done")
    events = db.list_events(conn, t["id"])
    assert len(events) == 2
    assert events[0]["event_type"] == "start"


def test_lock_single_flight(conn):
    p = db.create_project(conn, "demo", "/tmp/demo")
    t1 = db.create_ticket(conn, p["id"], "T1", "o", "a")
    t2 = db.create_ticket(conn, p["id"], "T2", "o", "a")

    assert db.get_locked_ticket_id(conn) is None
    assert db.try_acquire_lock(conn, t1["id"]) is True
    assert db.get_locked_ticket_id(conn) == t1["id"]
    # second acquire fails while locked
    assert db.try_acquire_lock(conn, t2["id"]) is False

    db.release_lock(conn)
    assert db.get_locked_ticket_id(conn) is None
    assert db.try_acquire_lock(conn, t2["id"]) is True
