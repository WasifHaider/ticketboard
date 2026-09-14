# TICKETBOARD MVP Implementation Plan

> **For Hermes:** Use subagent-driven-development / phased-project-builder skill
> conventions to implement this plan phase-by-phase (TDD per task, verify
> independently after each phase, never commit/push without being asked).

**Goal:** A small standalone kanban-style ticket board (FastAPI + SQLite backend,
lightweight mobile-responsive frontend) where project specs get broken into
phase-tickets, the user reviews/promotes tickets to "Ready" from mobile or
laptop, and Hermes (via a cron-polled worker) builds Ready tickets one at a
time through Claude Code CLI, logging progress events per ticket and flipping
status to Done/Failed. A second cron sends an end-of-day Telegram digest.

**Architecture:** Single FastAPI app serving both a JSON API and a static
frontend (no separate frontend build pipeline for MVP — plain HTML/CSS/JS,
fetch-based, to minimize moving parts). SQLite via stdlib `sqlite3` (no ORM,
matches this user's no-black-box preference from SLEUTH). Ticket state machine:
`todo -> ready -> in_progress -> done | failed`. A global single-flight lock
(one row in a `worker_state` table) enforces "one ticket building at a time,
across all projects" without needing an external queue system.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, stdlib `sqlite3`, vanilla
HTML/CSS/JS frontend served as static files, `httpx` for any internal calls,
pytest for tests.

---

## Ticket status model (reference for all phases)

```
todo        -- auto-created from a phase breakdown, unreviewed
ready       -- user promoted it; eligible for the worker to pick up
in_progress -- worker is actively building it (Claude Code CLI running)
done        -- verified passing, worker finished successfully
failed      -- worker attempted it and verification failed; needs human look
```

Only one ticket may be `in_progress` at any time, across all projects
(enforced by a single-row lock, see Phase 3).

---

## Phase 1: Schema + DB layer + core tests

**Objective:** Stand up the SQLite schema and a thin, hand-written DB access
layer (no ORM) with tests, nothing else.

**Files:**
- Create: `ticketboard/db.py`
- Create: `ticketboard/schema.sql`
- Create: `tests/test_db.py`
- Create: `requirements.txt`, `.gitignore`

**Schema (`schema.sql`):**
```sql
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    spec_summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    files_hint TEXT,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo','ready','in_progress','done','failed')),
    order_index INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('start','finish','comment','fail')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS worker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    locked_ticket_id INTEGER REFERENCES tickets(id)
);
INSERT OR IGNORE INTO worker_state (id, locked_ticket_id) VALUES (1, NULL);
```

**Step 1: Write failing test**
```python
# tests/test_db.py
import sqlite3
from ticketboard.db import get_connection, apply_schema

def test_apply_schema_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    apply_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "tickets", "ticket_events", "worker_state"} <= tables
```

**Step 2: Run test to verify failure**
Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ticketboard'`

**Step 3: Write minimal implementation**
`ticketboard/db.py`:
```python
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
```

**Step 4: Run test to verify pass**
Run: `python -m pytest tests/test_db.py -v`
Expected: PASS

**Step 5: Add CRUD helpers + tests** (`create_project`, `create_ticket`,
`list_tickets(status=None)`, `get_ticket`, `update_ticket_status`,
`add_event`, `list_events`, `try_acquire_lock`, `release_lock`,
`get_locked_ticket_id`) — each with a corresponding test in `test_db.py`
following the same TDD cycle. `try_acquire_lock`/`release_lock` implement the
single-flight lock via `worker_state` (an `UPDATE ... WHERE locked_ticket_id
IS NULL` is atomic in SQLite under the default transaction — use that instead
of a separate mutex).

**Step 6: Commit** (user does this manually per project convention, or agent
if explicitly told to for this project — confirm at Phase 1 review.)

---

## Phase 2: FastAPI app + REST endpoints

**Objective:** Expose the DB layer over HTTP: projects, tickets (create/list/
patch status), events.

**Files:**
- Create: `ticketboard/main.py`
- Create: `ticketboard/routes/projects.py`
- Create: `ticketboard/routes/tickets.py`
- Create: `tests/test_api.py`

**Endpoints:**
- `POST /projects` `{name, repo_path, spec_summary}` -> project row
- `GET /projects` -> list
- `POST /projects/{id}/tickets` `{tickets: [{title, objective,
  acceptance_criteria, files_hint}, ...]}` -> bulk-create, `order_index`
  assigned sequentially, status defaults `todo`
- `GET /tickets?status=&project_id=` -> list (used by UI and by the worker
  cron to find `status=ready` oldest-first)
- `PATCH /tickets/{id}` `{status}` -> validated state transition (UI uses
  this for `todo->ready`; worker uses it for `ready->in_progress->done|failed`)
- `GET /tickets/{id}` -> ticket + its events, for the detail view
- `POST /tickets/{id}/events` `{event_type, message}` -> append event,
  bumps `updated_at`

**TDD:** each endpoint gets a `TestClient`-based test in `test_api.py`
(round-trip create -> list -> patch -> verify), following the same
write-test-first cycle as Phase 1. Use FastAPI `lifespan` to open/apply
schema on a test DB per test (fixture in `tests/conftest.py`, `tmp_path`-based
— avoids the "lifespan never ran" class of bug SLEUTH hit).

**Validation to include:** `PATCH /tickets/{id}` rejects illegal transitions
(e.g. `done -> ready` directly) with 400 — table of legal transitions lives in
`ticketboard/db.py` as a constant, shared by API validation and worker logic
so they can't drift.

---

## Phase 3: Frontend kanban board

**Objective:** A single static page, mobile-responsive, showing columns
(To Do / Ready / In Progress / Done / Failed), ticket cards, click-through to
a detail view with the event log, and a button to promote `todo -> ready`.

**Files:**
- Create: `ticketboard/static/index.html`
- Create: `ticketboard/static/app.js`
- Create: `ticketboard/static/style.css`
- Modify: `ticketboard/main.py` (mount `StaticFiles`)

**Approach:** No build step — plain `fetch()` calls against the Phase 2 API,
re-render on poll (every ~10s) or on user action. CSS: simple responsive grid
that collapses columns to a scrollable single-column list under ~600px width
(mobile). Detail view: modal or separate route showing objective, acceptance
criteria, and the chronological event list.

**Verification:** manual — run `uvicorn ticketboard.main:app`, hit
`http://localhost:8000` from both a desktop browser and a phone (same LAN),
confirm: board loads, "todo" tickets show a promote-to-ready button, status
changes persist on refresh, detail view shows events.

---

## Phase 4: Worker — cron-polled ticket builder

**Objective:** The piece that actually builds tickets: polls for the oldest
`ready` ticket (across all projects) when no ticket is `in_progress`, runs it
through Claude Code CLI in the ticket's project `repo_path`, verifies
independently, logs events, flips status.

**Files:**
- Create: `ticketboard/worker.py`
- Create: `tests/test_worker.py` (mocks the Claude Code subprocess call and
  test-command verification, asserts the state machine transitions and
  locking behave correctly — does NOT actually invoke `claude` in tests)

**Worker algorithm (`run_worker_tick(conn, config)`):**
1. If `get_locked_ticket_id(conn)` is not None -> a ticket is already running
   (or a previous run crashed mid-build) -> log and return (do not
   double-build; a stuck lock needs human intervention, surfaced via the
   end-of-day digest showing an `in_progress` ticket older than N hours).
2. `ticket = list_tickets(conn, status='ready')` oldest by `order_index`/
   `created_at`, first across all projects, or return if none.
3. `try_acquire_lock(conn, ticket.id)`; `update_ticket_status(ticket.id,
   'in_progress')`; `add_event(ticket.id, 'start', ...)`.
4. Look up the ticket's project `repo_path`. Run:
   ```
   claude -p "<objective + acceptance_criteria + files_hint + \
   'follow this repo's CLAUDE.md/AGENTS.md conventions; TDD; do not git commit'>" \
     --output-format json --max-turns 40
   ```
   via `subprocess.run(..., cwd=repo_path, capture_output=True, timeout=...)`.
5. Independently verify: run the target repo's test command (read from a
   `test_command` field on `projects`, set at project-creation time — e.g.
   `pytest -q` or `npm test`), and `git status --porcelain` / `git diff
   --stat` to confirm files touched are in scope.
6. On success: `update_ticket_status(ticket.id, 'done')`, `add_event(ticket.id,
   'finish', <summary: files changed, test result>)`.
   On failure (claude errored, tests failed, or timeout): `update_ticket_status
   (ticket.id, 'failed')`, `add_event(ticket.id, 'fail', <specifics>)`.
7. `release_lock(conn)` in a `finally` block regardless of outcome — a crash
   mid-build must not leave the lock held forever.

**Entry point:** `python -m ticketboard.worker --once` (single tick, what the
cron job calls) vs a `--loop` dev mode for manual testing.

**Cron wiring (done after Phase 4 code lands, not part of the code itself):**
`mcp__cronjob` action=create, schedule='every 20m' (tune later), script
pointing at `python -m ticketboard.worker --once` with `workdir` set to
`D:\Personal\TICKETBOARD`, `no_agent=false` since it needs Claude Code CLI +
verification reasoning, not a dumb script — actually: worker.py itself
shells out to `claude` directly, so the cron's own agent loop doesn't need to
do the building, just needs to run the script and report. Revisit
`no_agent=true` with a wrapping shell script once worker.py is proven stable,
to skip an LLM call per tick.

---

## Phase 5: End-of-day Telegram digest

**Objective:** A second cron job that summarizes the day's ticket activity
and pings Telegram.

**Files:**
- Create: `ticketboard/digest.py`

**Logic:** query counts per status, list tickets that moved to `done`/`failed`
today (by `updated_at` date), list any ticket stuck `in_progress` longer than
a threshold (stale-lock signal), format a short plain-text summary, return it
as the script's stdout (cron `no_agent=true`, since this is deterministic
formatting, no reasoning needed) or have a cron agent call it and relay.

**Cron wiring:** `mcp__cronjob` action=create, schedule natural language
e.g. 'every day at 6pm', `script='ticketboard/digest.py'`, `no_agent=true`,
`deliver='telegram'` (or whatever the connected platform is called in this
Hermes install — confirm at wiring time).

---

## Phase 6: Spec-to-tickets integration

**Objective:** Wire the existing "paste a spec in chat -> phase breakdown"
step (from `phased-project-builder` skill) to create the project + tickets in
TICKETBOARD via its API instead of (or in addition to) the old chat-summary
flow.

**Files:**
- Modify: `phased-project-builder` skill (via `skill_manage` patch) — Step 3
  onward changes from "write plan .md + chat summary + wait for chat
  approval" to "write plan .md (still, for durable detail) + POST /projects +
  POST /{id}/tickets (status=todo) + tell user in chat the board link + they
  promote tickets to Ready in the UI instead of replying 'approved'."
- No new ticketboard code needed if Phase 2's API already covers it — this
  phase is primarily a skill-doc update + one live end-to-end test (paste a
  real toy spec, confirm tickets land in the board correctly).

---

## Cross-phase notes

- **Testing:** `python -m pytest tests/ -q` from `D:\Personal\TICKETBOARD`.
  Standard venv, no WSL/Docker dependency needed (SQLite has no native-build
  surface like SLEUTH's tree-sitter grammars) — should work fine from
  Git-Bash-on-Windows directly, verify at Phase 1 and note here if not.
- **Config:** a `ticketboard/config.py` (env-var based, mirrors SLEUTH's
  `Config` pattern) holding `DB_PATH`, `HOST`/`PORT`, `CLAUDE_MAX_TURNS`
  default, digest schedule threshold for "stale in_progress" alerting.
- **No commits without being asked** — same standing convention as SLEUTH.
- **Risks / open questions:**
  - Claude Code CLI availability/auth must already be set up in this
    environment (per the `claude-code` skill prerequisites) — confirm before
    Phase 4.
  - `test_command` per project needs to be set correctly at project-creation
    time (Phase 6 integration) or verification in Phase 4 has nothing to run
    — for the very first dogfood project (TICKETBOARD building itself, if
    ever) this is `python -m pytest tests/ -q`.
  - Single global lock means a long-running ticket blocks all other projects
    entirely — accepted tradeoff per explicit user choice (strict FIFO,
    single-flight), revisit only if it becomes a real bottleneck.
  - No auth/multi-user concern for MVP — single user, presumably LAN-only or
    localhost access from phone via same network / tailscale-style access;
    flag to user before exposing this to the open internet.

---

## Phase Status

- [x] Phase 1: Schema + DB layer + core tests — 9/9 tests passing
- [x] Phase 2: FastAPI app + REST endpoints — 20/20 tests passing (fixed check_same_thread threading bug)
- [x] Phase 3: Frontend kanban board — served via StaticFiles, verified live via curl (HTML/JS/CSS all 200, full API round-trip)
- [x] Phase 4: Worker — cron-polled ticket builder — 28/28 tests passing, CLI entrypoint verified live (`python -m ticketboard.worker --once`)
- [x] Phase 5: End-of-day Telegram digest — 32/32 tests passing, CLI entrypoint verified live (`python -m ticketboard.digest`)
- [x] Phase 6: Spec-to-tickets integration — phased-project-builder skill patched to push phases into TICKETBOARD instead of chat-only approval
- [x] Phase 7: UI-based ticket/project creation + file attachments — "New Ticket" modal (inline new-project form, title/objective/acceptance/files-hint, file upload), `/projects/{id}/ticket` single-create endpoint, `/tickets/{id}/attachments` upload endpoint — 44/44 tests passing, verified live (curl round-trip: create project, create ticket, upload file)
- [x] Phase 8: Per-ticket AI chat before approval — `ticketboard/chat.py` builds a prompt from ticket fields + attachments + message history, calls Claude Code CLI via stdin (argv-based multi-line prompts were getting mangled by claude.cmd on Windows — fixed by piping via subprocess stdin instead), `/tickets/{id}/messages` GET/POST endpoints, chat panel in ticket detail modal — verified live with multi-turn context threading working correctly
- [x] Phase 9 (senior-dev review, item 1 + trust fix): git worktree isolation, refuse-if-dirty, worker heartbeat, gateway-independent dispatch — `ticketboard/git_worktree.py` builds each ticket in an isolated worktree (outside the target repo, keyed by repo-path hash under TICKETBOARD's own `worktrees/` dir — NOT inside repo_path, which would pollute the target repo's own git status) on branch `ticket/<id>`, refuses to build if the target repo has uncommitted changes, commits the ticket's changes to that branch (never main). Migrated worker.py to build/test/commit inside the worktree instead of the live repo. Added `worker_state.last_heartbeat` + `/worker/status` endpoint + a live badge in the UI header (fresh/stale/unknown) so a silently-dead dispatcher is visible instead of looking identical to a working board. Replaced the Hermes-cron dispatch (which silently no-ops if the gateway isn't running — the exact trust gap flagged) with Windows Task Scheduler tasks (`TicketboardWorker` every 2 min, `TicketboardDigest` daily 6pm) calling the existing .bat wrapper scripts directly — no gateway dependency. Added `_run_migrations`/`_add_column_if_missing` in db.py for additive ALTER TABLE on pre-existing db files (CREATE TABLE IF NOT EXISTS doesn't add new columns to an already-existing table — caught by a live test against the real dev db). Fixed a real Claude Code CLI permission-denial bug found during live testing: builds need `--dangerously-skip-permissions` specifically (not `--allow-...`, a decoy-named flag that only enables the *option*) to write files non-interactively; scoped this exclusively to `run_claude_code` (build path, always inside an isolated worktree) — `run_claude_chat` (the discussion feature) deliberately keeps default permissions, staying read-only. 59/59 tests passing (9 new for git_worktree.py using real temp git repos, not mocks). Verified live end-to-end via the real Task Scheduler task: ticket promoted → picked up with zero Hermes/gateway involvement → built in isolated worktree → TDD cycle ran for real → tests passed → committed to `ticket/6` branch → status flipped to done — confirmed via `git log` showing a real commit outside the worker's own process.
- [x] Phase 10: redesigned UI (ported from a Claude Design mockup) — replaced the plain-HTML static frontend with a real designed board (spine-style status-colored ticket cards, live pulse dot on In Progress, sidebar with worker heartbeat badge + "now building" card + queued/failed stat boxes + a real dispatcher log), a two-column ticket detail modal (objective/AC/files/attachments/timeline on the left, chat + approve bar on the right), and a two-step new-ticket modal (pick/register project on the left, ticket fields on the right). Added `db.list_recent_events` + `/events/recent` endpoint (one joined SQL query, not N+1) to back the dispatcher log. Plain HTML/CSS/JS, no build step, wired to the real live API — not the mockup's fake placeholder data. 61/61 tests passing.
- [x] Phase 12 (senior-dev review, item 3 — acceptance-criteria judge pass): `ticketboard/judge.py` — after tests pass, a FRESH Claude Code invocation (default permissions, read-only, no context from the build session) is given only the acceptance criteria + the actual git diff and rules pass/fail per criterion with citations, returned as structured JSON. New `needs_review` ticket status (distinct from `failed` — tests genuinely passed here, this is a scope/correctness verdict) with its own re-promote flow in the UI. `judge_verdict`/`judge_reason` columns + `base_commit` column (fixed a real latent bug: `diff_stat`/`finish` events were diffing against a bare "HEAD" which goes stale once `commit_worktree_changes` commits — now pinned to the worktree's actual pre-build base commit, captured at worktree-creation time). Judge is fail-open on infra trouble (CLI error, unparseable JSON) — an inconclusive judge never blocks a good build, only an actual "fail" verdict does. Non-destructive SQLite migration for the widened status CHECK constraint (SQLite can't ALTER a CHECK in place — table-rebuild migration preserves all data) with a real bug caught and fixed during live testing: SQLite's ALTER TABLE RENAME silently rewrites OTHER tables' FK schema text to point at the renamed table, requiring `PRAGMA legacy_alter_table=ON` during the rebuild or every FK'd table (events/attachments/messages/worker_state) permanently breaks. UI: new "Needs Review" board column, judge verdict/reason panel in ticket detail, judge-flagged card styling. 79/79 tests passing (10 new: judge.py prompt-building/JSON-extraction/pass-fail/inconclusive-handling in isolation with mocked CLI calls; 3 new worker-integration tests for judge-fail→needs_review, judge-inconclusive→still-done, judge-disabled→skipped; 1 new migration regression test that inserts through the FK path post-rebuild to catch the exact FK-rewrite bug). Verified live end-to-end twice with real throwaway repos and real Claude Code + judge calls (no mocks): both times the builder correctly implemented all stated acceptance criteria and the judge independently verified this from the diff alone, citing the specific functions/behaviors that satisfied each criterion — confirmed via `git log`/file inspection that the judge's ruling matched the actual code on disk.

