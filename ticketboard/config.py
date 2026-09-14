import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = os.environ.get("TICKETBOARD_DB_PATH", str(BASE_DIR / "ticketboard.db"))
HOST = os.environ.get("TICKETBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("TICKETBOARD_PORT", "8000"))

# Cross-origin allowlist for when the frontend is deployed separately from
# the backend (e.g. frontend on Vercel, backend on this machine behind a
# Tailscale Funnel / Cloudflare Tunnel / etc). Comma-separated exact
# origins, e.g. "https://ticketboard.vercel.app,https://ticketboard-git-main-you.vercel.app".
# Empty by default — deliberately deny-by-default since the API has no
# auth of its own; same-origin use (frontend served by this same FastAPI
# app, the default local setup) needs no CORS config at all.
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("TICKETBOARD_CORS_ORIGINS", "").split(",") if o.strip()
]
CLAUDE_MAX_TURNS = int(os.environ.get("TICKETBOARD_CLAUDE_MAX_TURNS", "40"))
CLAUDE_TIMEOUT_S = int(os.environ.get("TICKETBOARD_CLAUDE_TIMEOUT_S", "1800"))
STALE_IN_PROGRESS_HOURS = int(os.environ.get("TICKETBOARD_STALE_HOURS", "3"))

JUDGE_ENABLED = os.environ.get("TICKETBOARD_JUDGE_ENABLED", "1") not in ("0", "false", "False")
JUDGE_MAX_TURNS = int(os.environ.get("TICKETBOARD_JUDGE_MAX_TURNS", "10"))
JUDGE_TIMEOUT_S = int(os.environ.get("TICKETBOARD_JUDGE_TIMEOUT_S", "300"))
JUDGE_MAX_DIFF_CHARS = int(os.environ.get("TICKETBOARD_JUDGE_MAX_DIFF_CHARS", "20000"))
