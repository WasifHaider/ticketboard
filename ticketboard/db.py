import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Legal ticket status transitions, shared by API validation and worker logic
# so they can never drift apart.
LEGAL_TRANSITIONS = {
    "todo": {"ready"},
    "ready": {"in_progress", "todo"},
    "in_progress": {"done", "failed", "needs_review"},
    "done": set(),
    "failed": {"ready"},
    "needs_review": {"done", "failed", "ready"},
}


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    _run_migrations(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema patches for columns introduced after a
    table already existed (CREATE TABLE IF NOT EXISTS doesn't alter existing
    tables). Must run BEFORE schema.sql's own INSERT statements, which
    reference these columns by name. No-ops harmlessly if a table doesn't
    exist yet (fresh db — schema.sql's CREATE TABLE will define it directly
    with the columns already present)."""
    _add_column_if_missing(conn, "tickets", "worktree_path", "TEXT")
    _add_column_if_missing(conn, "tickets", "branch_name", "TEXT")
    _add_column_if_missing(conn, "tickets", "base_commit", "TEXT")
    _add_column_if_missing(conn, "tickets", "judge_verdict", "TEXT")
    _add_column_if_missing(conn, "tickets", "judge_reason", "TEXT")
    _add_column_if_missing(conn, "worker_state", "last_heartbeat", "TEXT")
    _widen_ticket_status_check(conn)


def _widen_ticket_status_check(conn: sqlite3.Connection) -> None:
    """SQLite CHECK constraints can't be altered with ALTER TABLE — they're
    baked into the table at creation. A db created before 'needs_review' was
    added to the status CHECK would reject any insert/update to that status
    even after schema.sql is re-run, since CREATE TABLE IF NOT EXISTS leaves
    the existing table (and its old CHECK) untouched. Detect that case and
    rebuild the table with the new constraint, preserving all data."""
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "tickets" not in tables:
        return

    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets'"
    ).fetchone()
    if ddl_row is None or "needs_review" in (ddl_row["sql"] or ""):
        return  # already current, or table doesn't exist (shouldn't happen given check above)

    # SQLite's ALTER TABLE RENAME auto-rewrites FK references in OTHER
    # tables' schema text to point at the new name (a documented but easy
    # to miss behavior on modern SQLite). Without suppressing that, every
    # table with a FK into tickets (ticket_events, ticket_attachments,
    # ticket_messages, worker_state) ends up permanently pointing at
    # "tickets_old_migration" once that table is dropped below — a subtle,
    # data-destroying bug caught by re-running a real migration end-to-end.
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE tickets RENAME TO tickets_old_migration")
    conn.executescript("""
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL,
            files_hint TEXT,
            status TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo','ready','in_progress','done','failed','needs_review')),
            order_index INTEGER NOT NULL,
            worktree_path TEXT,
            branch_name TEXT,
            base_commit TEXT,
            judge_verdict TEXT,
            judge_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    old_cols = {row["name"] for row in conn.execute(
        "PRAGMA table_info(tickets_old_migration)")}
    new_cols = ["id", "project_id", "title", "objective", "acceptance_criteria",
                "files_hint", "status", "order_index", "worktree_path",
                "branch_name", "base_commit", "judge_verdict", "judge_reason",
                "created_at", "updated_at"]
    select_cols = [c if c in old_cols else "NULL" for c in new_cols]
    conn.execute(
        f"INSERT INTO tickets ({', '.join(new_cols)}) "
        f"SELECT {', '.join(select_cols)} FROM tickets_old_migration"
    )
    conn.execute("DROP TABLE tickets_old_migration")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                            col_type: str) -> None:
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(conn: sqlite3.Connection, name: str, repo_path: str,
                    spec_summary: Optional[str] = None,
                    test_command: Optional[str] = None) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO projects (name, repo_path, spec_summary, test_command) "
        "VALUES (?, ?, ?, ?)",
        (name, repo_path, spec_summary, test_command),
    )
    conn.commit()
    return get_project(conn, cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ).fetchall()


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def create_ticket(conn: sqlite3.Connection, project_id: int, title: str,
                   objective: str, acceptance_criteria: str,
                   files_hint: Optional[str] = None,
                   order_index: Optional[int] = None) -> sqlite3.Row:
    if order_index is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS next FROM tickets "
            "WHERE project_id = ?", (project_id,)
        ).fetchone()
        order_index = row["next"]
    cur = conn.execute(
        "INSERT INTO tickets (project_id, title, objective, acceptance_criteria, "
        "files_hint, order_index) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, title, objective, acceptance_criteria, files_hint, order_index),
    )
    conn.commit()
    return get_ticket(conn, cur.lastrowid)


def set_ticket_worktree(conn: sqlite3.Connection, ticket_id: int,
                         worktree_path: str, branch_name: str,
                         base_commit: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE tickets SET worktree_path = ?, branch_name = ?, base_commit = ? WHERE id = ?",
        (worktree_path, branch_name, base_commit, ticket_id),
    )
    conn.commit()


def set_ticket_judge_result(conn: sqlite3.Connection, ticket_id: int,
                             verdict: str, reason: str) -> None:
    conn.execute(
        "UPDATE tickets SET judge_verdict = ?, judge_reason = ? WHERE id = ?",
        (verdict, reason, ticket_id),
    )
    conn.commit()


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()


def list_tickets(conn: sqlite3.Connection, status: Optional[str] = None,
                  project_id: Optional[int] = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM tickets WHERE 1=1"
    params: list = []
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if project_id is not None:
        query += " AND project_id = ?"
        params.append(project_id)
    query += " ORDER BY order_index ASC, created_at ASC"
    return conn.execute(query, params).fetchall()


class IllegalTransitionError(ValueError):
    pass


def update_ticket_status(conn: sqlite3.Connection, ticket_id: int,
                          new_status: str, force: bool = False) -> sqlite3.Row:
    ticket = get_ticket(conn, ticket_id)
    if ticket is None:
        raise ValueError(f"ticket {ticket_id} not found")
    if not force and new_status not in LEGAL_TRANSITIONS.get(ticket["status"], set()):
        raise IllegalTransitionError(
            f"cannot transition ticket {ticket_id} from "
            f"{ticket['status']!r} to {new_status!r}"
        )
    conn.execute(
        "UPDATE tickets SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, ticket_id),
    )
    conn.commit()
    return get_ticket(conn, ticket_id)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def add_event(conn: sqlite3.Connection, ticket_id: int, event_type: str,
              message: str) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO ticket_events (ticket_id, event_type, message) "
        "VALUES (?, ?, ?)",
        (ticket_id, event_type, message),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM ticket_events WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_events(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    ).fetchall()


def list_recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Cross-ticket, cross-project event feed for the dispatcher log — a
    single joined query instead of N+1 per-ticket calls from the client."""
    return conn.execute(
        "SELECT te.id, te.ticket_id, te.event_type, te.message, te.created_at, "
        "t.title AS ticket_title, t.project_id "
        "FROM ticket_events te "
        "JOIN tickets t ON t.id = te.ticket_id "
        "ORDER BY te.created_at DESC, te.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Worker single-flight lock
# ---------------------------------------------------------------------------

def try_acquire_lock(conn: sqlite3.Connection, ticket_id: int) -> bool:
    cur = conn.execute(
        "UPDATE worker_state SET locked_ticket_id = ? "
        "WHERE id = 1 AND locked_ticket_id IS NULL",
        (ticket_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def release_lock(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE worker_state SET locked_ticket_id = NULL WHERE id = 1")
    conn.commit()


def get_locked_ticket_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT locked_ticket_id FROM worker_state WHERE id = 1"
    ).fetchone()
    return row["locked_ticket_id"] if row else None


def heartbeat(conn: sqlite3.Connection) -> None:
    """Records that the worker process is alive and polling, independent of
    whether it currently holds the build lock. Used for the UI status badge."""
    conn.execute(
        "UPDATE worker_state SET last_heartbeat = datetime('now') WHERE id = 1"
    )
    conn.commit()


def get_last_heartbeat(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT last_heartbeat FROM worker_state WHERE id = 1"
    ).fetchone()
    return row["last_heartbeat"] if row else None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def add_attachment(conn: sqlite3.Connection, ticket_id: int, filename: str,
                    stored_path: str, content_type: Optional[str] = None) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO ticket_attachments (ticket_id, filename, stored_path, content_type) "
        "VALUES (?, ?, ?, ?)",
        (ticket_id, filename, stored_path, content_type),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM ticket_attachments WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_attachments(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ticket_attachments WHERE ticket_id = ? ORDER BY created_at ASC",
        (ticket_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

def add_message(conn: sqlite3.Connection, ticket_id: int, role: str,
                 message: str) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO ticket_messages (ticket_id, role, message) VALUES (?, ?, ?)",
        (ticket_id, role, message),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM ticket_messages WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_messages(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    ).fetchall()
