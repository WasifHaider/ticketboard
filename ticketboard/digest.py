"""End-of-day digest: deterministic summary of ticket activity, for a
no_agent cron job (stdout is delivered verbatim).
"""
from ticketboard import config, db, worker


def build_digest(conn, stale_hours: int = config.STALE_IN_PROGRESS_HOURS) -> str:
    lines = ["TICKETBOARD daily digest"]

    counts = {}
    for status in ("todo", "ready", "in_progress", "done", "failed"):
        counts[status] = len(db.list_tickets(conn, status=status))
    lines.append(
        "Status counts: " + ", ".join(f"{k}={v}" for k, v in counts.items())
    )

    today_rows = conn.execute(
        "SELECT t.*, p.name AS project_name FROM tickets t "
        "JOIN projects p ON p.id = t.project_id "
        "WHERE date(t.updated_at) = date('now') AND t.status IN ('done','failed') "
        "ORDER BY t.updated_at ASC"
    ).fetchall()

    if today_rows:
        lines.append("")
        lines.append("Tickets resolved today:")
        for row in today_rows:
            lines.append(f"  [{row['status'].upper()}] {row['project_name']} / {row['title']}")
    else:
        lines.append("")
        lines.append("No tickets resolved today.")

    stale = worker.get_stale_in_progress(conn, hours=stale_hours)
    if stale:
        lines.append("")
        lines.append(f"WARNING: {len(stale)} ticket(s) stuck in_progress > {stale_hours}h:")
        for t in stale:
            lines.append(f"  ticket #{t['id']}: {t['title']} (updated {t['updated_at']})")

    return "\n".join(lines)


def main():
    conn = db.get_connection(config.DB_PATH)
    db.apply_schema(conn)
    print(build_digest(conn))


if __name__ == "__main__":
    main()
