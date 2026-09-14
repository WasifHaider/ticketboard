import platform
import shutil
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ticketboard import db

router = APIRouter()


def _pick_folder_dialog() -> Optional[str]:
    """Pops a native "Select Folder" dialog and returns the chosen absolute
    path, or None if the user cancelled/it's unsupported on this OS. This
    blocks the calling thread until the dialog closes — FastAPI runs sync
    `def` routes in a threadpool, so it does not block the event loop or
    other requests.

    This is a nice-to-have convenience, not a required feature: the REPO
    PATH field can always be typed in by hand, so any platform/toolchain gap
    here degrades to "no picker" rather than breaking project creation.
    """
    system = platform.system()

    if system == "Windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$f.Description = 'Select project repo folder';"
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {"
            "  Write-Output $f.SelectedPath"
            "}"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    elif system == "Darwin":
        script = (
            'POSIX path of (choose folder with prompt "Select project repo folder")'
        )
        cmd = ["osascript", "-e", script]
    elif system == "Linux":
        if shutil.which("zenity"):
            cmd = ["zenity", "--file-selection", "--directory",
                   "--title=Select project repo folder"]
        elif shutil.which("kdialog"):
            cmd = ["kdialog", "--getexistingdirectory", ".",
                   "--title", "Select project repo folder"]
        else:
            return None  # no GUI folder picker available — type the path manually
    else:
        return None

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip()
    return path or None


class ProjectCreate(BaseModel):
    name: str
    repo_path: str
    spec_summary: Optional[str] = None
    test_command: Optional[str] = None


class TicketCreate(BaseModel):
    title: str
    objective: str
    acceptance_criteria: str
    files_hint: Optional[str] = None


class TicketsBulkCreate(BaseModel):
    tickets: list[TicketCreate]


def _row(r) -> dict:
    return dict(r) if r is not None else None


@router.post("/browse-folder")
def browse_folder():
    """Pops the native Windows folder picker and returns the selected
    absolute path, for the New Ticket modal's REPO PATH field."""
    path = _pick_folder_dialog()
    if path is None:
        return {"path": None}
    return {"path": path}


@router.post("/projects")
def create_project(payload: ProjectCreate, request: Request):
    conn = request.app.state.conn
    project = db.create_project(
        conn, payload.name, payload.repo_path,
        payload.spec_summary, payload.test_command,
    )
    return _row(project)


@router.get("/projects")
def list_projects(request: Request):
    conn = request.app.state.conn
    return [_row(p) for p in db.list_projects(conn)]


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request):
    conn = request.app.state.conn
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _row(project)


@router.post("/projects/{project_id}/tickets")
def bulk_create_tickets(project_id: int, payload: TicketsBulkCreate, request: Request):
    conn = request.app.state.conn
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    created = [
        _row(db.create_ticket(
            conn, project_id, t.title, t.objective, t.acceptance_criteria, t.files_hint,
        ))
        for t in payload.tickets
    ]
    return created


@router.post("/projects/{project_id}/ticket")
def create_single_ticket(project_id: int, payload: TicketCreate, request: Request):
    conn = request.app.state.conn
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    ticket = db.create_ticket(
        conn, project_id, payload.title, payload.objective,
        payload.acceptance_criteria, payload.files_hint,
    )
    return _row(ticket)
