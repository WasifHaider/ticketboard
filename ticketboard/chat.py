"""Per-ticket AI chat: lets the user discuss a ticket with Claude Code before
approving it. Each turn re-runs the CLI (print mode) in the ticket's project
repo_path with the full message history + attachment contents folded into
the prompt, since the CLI itself has no persistent session here.
"""
from pathlib import Path

from ticketboard import db
from ticketboard.claude_cli import run_claude_chat

MAX_ATTACHMENT_CHARS = 4000
MAX_HISTORY_TURNS = 20


def _read_attachment_snippet(stored_path: str) -> str:
    path = Path(stored_path)
    try:
        if not path.exists():
            return "(file missing on disk)"
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"(binary file, {len(data)} bytes, not shown)"
        if len(text) > MAX_ATTACHMENT_CHARS:
            return text[:MAX_ATTACHMENT_CHARS] + "\n... (truncated)"
        return text
    except Exception as exc:
        return f"(could not read attachment: {exc})"


def build_chat_prompt(conn, ticket: dict) -> str:
    parts = [
        "You are discussing a proposed work ticket with the user before they "
        "approve it for automated building. Answer questions, suggest scope "
        "changes, flag risks. Do not modify any files or run commands unless "
        "explicitly asked to inspect the repo read-only for context.",
        "",
        f"Ticket title: {ticket['title']}",
        f"Objective: {ticket['objective']}",
        f"Acceptance criteria: {ticket['acceptance_criteria']}",
    ]
    if ticket["files_hint"]:
        parts.append(f"Files hint: {ticket['files_hint']}")

    attachments = db.list_attachments(conn, ticket["id"])
    if attachments:
        parts.append("")
        parts.append("Attached files (for context only, not to be modified):")
        for att in attachments:
            parts.append(f"--- {att['filename']} ---")
            parts.append(_read_attachment_snippet(att["stored_path"]))

    history = db.list_messages(conn, ticket["id"])[-MAX_HISTORY_TURNS:]
    if history:
        parts.append("")
        parts.append("Conversation so far:")
        for msg in history:
            speaker = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{speaker}: {msg['message']}")

    parts.append("")
    parts.append("Respond to the latest user message above as the Assistant.")
    return "\n".join(parts)


def send_chat_message(conn, ticket_id: int, user_message: str,
                       max_turns: int = 15, timeout_s: int = 600) -> dict:
    """Records the user's message, asks Claude Code for a reply, records and
    returns it. Returns {'success', 'reply' or 'error'}."""
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise ValueError(f"ticket {ticket_id} not found")

    db.add_message(conn, ticket_id, "user", user_message)

    project = db.get_project(conn, ticket["project_id"])
    prompt = build_chat_prompt(conn, dict(ticket))
    result = run_claude_chat(project["repo_path"], prompt, max_turns, timeout_s)

    if not result["success"]:
        error_text = f"(chat error: {result['stderr'][:500]})"
        db.add_message(conn, ticket_id, "assistant", error_text)
        return {"success": False, "error": result["stderr"]}

    reply = result["reply"] or "(no response)"
    db.add_message(conn, ticket_id, "assistant", reply)
    return {"success": True, "reply": reply}
