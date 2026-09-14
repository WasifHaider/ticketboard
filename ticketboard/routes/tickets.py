from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ticketboard import chat, config, db
from ticketboard.worker import trigger_worker_async

router = APIRouter()


class TicketStatusUpdate(BaseModel):
    status: str


class EventCreate(BaseModel):
    event_type: str
    message: str


class ChatMessageCreate(BaseModel):
    message: str


def _row(r) -> dict:
    return dict(r) if r is not None else None


@router.get("/tickets")
def list_tickets(request: Request, status: Optional[str] = None,
                  project_id: Optional[int] = None):
    conn = request.app.state.conn
    return [_row(t) for t in db.list_tickets(conn, status=status, project_id=project_id)]


@router.get("/worker/status")
def worker_status(request: Request):
    conn = request.app.state.conn
    last_heartbeat = db.get_last_heartbeat(conn)
    locked_ticket_id = db.get_locked_ticket_id(conn)

    stale = True
    seconds_since = None
    if last_heartbeat:
        try:
            hb = datetime.strptime(last_heartbeat, "%Y-%m-%d %H:%M:%S")
            seconds_since = (datetime.utcnow() - hb).total_seconds()
            # heartbeat is considered fresh if within 2x the expected poll
            # interval (dispatcher runs every ~1-2 min via Task Scheduler)
            stale = seconds_since > 300
        except ValueError:
            pass

    return {
        "last_heartbeat": last_heartbeat,
        "seconds_since_heartbeat": seconds_since,
        "stale": stale,
        "locked_ticket_id": locked_ticket_id,
    }


@router.get("/events/recent")
def recent_events(request: Request, limit: int = 20):
    conn = request.app.state.conn
    return [_row(e) for e in db.list_recent_events(conn, limit=limit)]


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, request: Request):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    events = [_row(e) for e in db.list_events(conn, ticket_id)]
    attachments = [_row(a) for a in db.list_attachments(conn, ticket_id)]
    messages = [_row(m) for m in db.list_messages(conn, ticket_id)]
    result = _row(ticket)
    result["events"] = events
    result["attachments"] = attachments
    result["messages"] = messages
    return result


@router.patch("/tickets/{ticket_id}")
def update_ticket_status(ticket_id: int, payload: TicketStatusUpdate, request: Request):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    try:
        updated = db.update_ticket_status(conn, ticket_id, payload.status)
    except db.IllegalTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload.status == "ready":
        # Fire the worker immediately instead of waiting for the next
        # Task Scheduler poll (up to 2 minutes). Fire-and-forget: this never
        # raises and never blocks the response — the Task Scheduler poll
        # remains the safety net if this spawn fails for any reason.
        trigger_worker_async()
    return _row(updated)


@router.post("/tickets/{ticket_id}/events")
def create_event(ticket_id: int, payload: EventCreate, request: Request):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    event = db.add_event(conn, ticket_id, payload.event_type, payload.message)
    return _row(event)


@router.post("/tickets/{ticket_id}/attachments")
async def upload_attachment(ticket_id: int, request: Request, file: UploadFile):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")

    upload_dir = config.BASE_DIR / "uploads" / str(ticket_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload").name
    stored_path = upload_dir / safe_name

    contents = await file.read()
    stored_path.write_bytes(contents)

    attachment = db.add_attachment(
        conn, ticket_id, safe_name, str(stored_path), file.content_type,
    )
    return _row(attachment)


@router.get("/tickets/{ticket_id}/messages")
def list_chat_messages(ticket_id: int, request: Request):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return [_row(m) for m in db.list_messages(conn, ticket_id)]


@router.post("/tickets/{ticket_id}/messages")
def create_chat_message(ticket_id: int, payload: ChatMessageCreate, request: Request):
    conn = request.app.state.conn
    ticket = db.get_ticket(conn, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    try:
        result = chat.send_chat_message(conn, ticket_id, payload.message)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not result["success"]:
        raise HTTPException(status_code=502, detail=f"chat failed: {result['error']}")
    return {"reply": result["reply"]}
