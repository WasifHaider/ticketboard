# TICKETBOARD

A local kanban board where you write tickets (objective + acceptance
criteria) and an autonomous worker builds them for you using the
[Claude Code CLI](https://code.claude.com/docs/en/cli-reference) — in an
isolated git worktree, verified by your test suite and an independent
"judge" pass, never touching your working tree or `main` directly.

FastAPI + SQLite backend, Vue 3 frontend (no build step — Vue is loaded
from a CDN `<script>` tag), fully mobile-responsive so you can review and
promote tickets from your phone while the worker builds on your machine.

```
todo -> ready -> in_progress -> done
                              -> needs_review  (judge flagged it)
                              -> failed        (tests failed / CLI error)
```

Only one ticket builds at a time, across all projects (a single global
lock) — this is a personal, single-user tool, not a multi-tenant queue.

---

## Requirements

- **Python 3.11+**
- **Node.js is NOT required** — the frontend has no build step.
- **[Claude Code CLI](https://code.claude.com/docs/en/cli-reference)**
  installed and authenticated (`npm install -g @anthropic-ai/claude-code`,
  then run `claude` once to log in). The worker shells out to `claude -p`
  to actually build tickets, and to discuss tickets in chat — without it,
  everything else (board, tickets, projects) still works, but nothing
  builds.
- **Git**, and each project you add must be its own git repo (the worker
  builds tickets in an isolated worktree/branch of that repo).

## Quick start

```bash
git clone <this-repo> ticketboard
cd ticketboard

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

python -m uvicorn ticketboard.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** in a browser. That's it — no separate
frontend build/install step.

To use it from your phone on the same network, run with `--host 0.0.0.0`
and open `http://<your-machine-LAN-IP>:8000` from the phone. See
**Security note** below before doing this on an untrusted network.

## Using it

1. Click **New ticket**. Either pick an existing registered project, or
   register a new one inline: a name, the repo's local path (or use
   **Browse…** to pick it with a native folder dialog), and the test
   command the worker must run to verify a build (e.g. `pytest -q`,
   `npm test`).
2. Fill in a title, an objective (the outcome, not the implementation),
   and acceptance criteria (one checkable line per criterion — this
   becomes the pass/fail contract for both the build and the judge pass).
   Optionally attach a spec/log/screenshot and narrow the files hint.
3. The ticket lands in **To Do**. Use the chat panel in the ticket detail
   view to discuss scope with Claude before committing to it (read-only —
   it won't touch your repo during discussion).
4. When ready, click **Promote**. This moves the ticket to **Ready** and
   fires the worker immediately (usually within 1-2 seconds) rather than
   waiting for a poll.
5. Watch it move through **In Progress** → **Done** (or **Needs Review** /
   **Failed** if something didn't check out) in the board and the ticket's
   own event timeline. Every completed ticket lives on its own branch
   (`ticket/<id>`) in your repo — review and merge it yourself; TICKETBOARD
   never pushes or merges to `main`.

## How the worker actually runs

The API fires the worker as a detached background process the instant a
ticket is promoted (`ticketboard/worker.py`, `trigger_worker_async`). That
is enough for interactive/dev use — while the server is running, promoting
a ticket builds it.

If you want tickets to also get picked up when nothing promoted them
recently (e.g. a build fails and needs an automatic retry-safety-net, or
you want to be resilient to the trigger request failing), also schedule
`python -m ticketboard.worker --once` to run every couple of minutes as a
periodic task:

- **Windows:** Task Scheduler, action = run the venv's `python.exe` with
  args `-m ticketboard.worker --once`, working directory = the repo root.
- **macOS/Linux:** a cron entry, e.g.
  `*/2 * * * * cd /path/to/ticketboard && .venv/bin/python -m ticketboard.worker --once`

This poll is a safety net only, not the primary path — the instant-dispatch
on promote is what you'll see in normal use.

An optional daily digest (`python -m ticketboard.digest`) prints a
plain-text summary of the day's ticket activity to stdout — wire it into
your own notification path (cron + mail, a Telegram bot, etc.) however you
like; it makes no assumptions about delivery.

## Configuration

All configuration is environment variables (see `ticketboard/config.py`),
with sane defaults — nothing is required to get started locally.

| Variable | Default | Meaning |
|---|---|---|
| `TICKETBOARD_DB_PATH` | `<repo>/ticketboard.db` | SQLite file path |
| `TICKETBOARD_HOST` | `0.0.0.0` | uvicorn bind host |
| `TICKETBOARD_PORT` | `8000` | uvicorn bind port |
| `TICKETBOARD_CLAUDE_MAX_TURNS` | `40` | max agentic turns per build |
| `TICKETBOARD_CLAUDE_TIMEOUT_S` | `1800` | build timeout (seconds) |
| `TICKETBOARD_STALE_HOURS` | `3` | flags a ticket stuck `in_progress` this long |
| `TICKETBOARD_JUDGE_ENABLED` | `1` | run the acceptance-criteria judge pass after tests pass |
| `TICKETBOARD_JUDGE_MAX_TURNS` | `10` | max turns for the judge pass |
| `TICKETBOARD_JUDGE_TIMEOUT_S` | `300` | judge pass timeout (seconds) |
| `TICKETBOARD_JUDGE_MAX_DIFF_CHARS` | `20000` | diff size fed to the judge before truncation |

Copy `.env.example` to `.env` and adjust if you use something like
`python-dotenv`/your shell's env-loading of choice — TICKETBOARD itself
reads these straight from `os.environ`, no `.env` loader is bundled.

## Security note

There is no authentication. Anyone who can reach the server can create
projects (arbitrary local repo paths), promote tickets, and trigger the
worker to run Claude Code CLI with `--dangerously-skip-permissions`
against those repos' isolated worktrees. This is built for **local,
single-user use** — localhost, or your own LAN/Tailscale-style private
network. Do not expose it to the open internet without adding your own
auth layer in front of it (a reverse proxy with basic auth is the simplest
option).

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

79 tests, no network calls, no real `claude` CLI invocations (the worker's
Claude Code and judge calls are mocked in tests). SQLite has no native
build step, so this runs the same on Windows/macOS/Linux without a
compiler toolchain.

## Project layout

```
ticketboard/
  main.py            FastAPI app factory, mounts static frontend
  config.py           env-var configuration
  db.py                SQLite access layer (no ORM), schema + migrations
  schema.sql          table definitions
  worker.py            polls/builds Ready tickets via Claude Code CLI
  judge.py              independent acceptance-criteria verification pass
  git_worktree.py    isolated worktree/branch management per ticket
  claude_cli.py      subprocess wrapper around the `claude` CLI
  chat.py                per-ticket discussion (read-only Claude Code calls)
  digest.py             daily plain-text activity summary
  routes/
    projects.py       project CRUD, folder-picker endpoint
    tickets.py           ticket CRUD, status transitions, events, chat, attachments
  static/
    index.html          Vue 3 app shell + templates
    app.js                 Vue app: state, computed board/detail views, API calls
    style.css              dark theme, responsive breakpoints (1024px, 640px)
tests/                    pytest suite, one file per module above
```
