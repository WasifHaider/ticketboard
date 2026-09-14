"""Cron-polled ticket builder worker.

Picks the oldest `ready` ticket globally, builds it in an isolated git
worktree on branch ticket/<id> (never touching the target repo's own working
tree), runs Claude Code CLI there, independently verifies (test command +
git diff against the isolated worktree), commits the result to the ticket's
branch, logs events, and flips ticket status. Refuses to build if the target
repo itself has uncommitted changes.

Dispatch has two paths: the API fires trigger_worker_async() the instant a
ticket is promoted to Ready (see routes/tickets.py), so builds normally start
within a second or two rather than waiting for the next poll. A Windows Task
Scheduler task calling `--once` every 2 minutes is the safety-net poller for
missed nudges (server restart mid-promote, a nudge subprocess failing to
spawn, etc.) — it should never be the primary path in normal operation.
"""
import argparse
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ticketboard import config, db, git_worktree, judge
from ticketboard.claude_cli import run_claude_code


def _build_claude_prompt(ticket: dict) -> str:
    parts = [
        f"Objective: {ticket['objective']}",
        f"Acceptance criteria: {ticket['acceptance_criteria']}",
    ]
    if ticket["files_hint"]:
        parts.append(f"Likely files involved: {ticket['files_hint']}")
    parts.append(
        "Follow this repo's CLAUDE.md/AGENTS.md conventions if present. "
        "Use TDD: write a failing test, implement the minimal fix, confirm "
        "tests pass. Do not run git commit — the worker commits your changes "
        "for you after verification."
    )
    return "\n".join(parts)


def run_tests(repo_path: str, test_command: str | None) -> dict:
    if not test_command:
        return {"ran": False, "success": None, "output": "(no test_command configured)"}
    try:
        result = subprocess.run(
            shlex.split(test_command), cwd=repo_path, capture_output=True,
            text=True, timeout=600,
        )
        return {
            "ran": True,
            "success": result.returncode == 0,
            "output": (result.stdout + result.stderr)[-4000:],
        }
    except Exception as exc:
        return {"ran": True, "success": False, "output": f"error running tests: {exc}"}


def get_stale_in_progress(conn, hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale = []
    for t in db.list_tickets(conn, status="in_progress"):
        try:
            updated = datetime.fromisoformat(t["updated_at"]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if updated < cutoff:
            stale.append(dict(t))
    return stale


def run_worker_tick(conn, cfg=config) -> dict:
    """Runs one tick of the worker. Returns a summary dict for logging/tests."""
    db.heartbeat(conn)

    if db.get_locked_ticket_id(conn) is not None:
        return {"action": "skipped", "reason": "worker already locked"}

    ready = db.list_tickets(conn, status="ready")
    if not ready:
        return {"action": "skipped", "reason": "no ready tickets"}

    ticket = ready[0]
    project = db.get_project(conn, ticket["project_id"])

    if not db.try_acquire_lock(conn, ticket["id"]):
        return {"action": "skipped", "reason": "lost lock race"}

    try:
        db.update_ticket_status(conn, ticket["id"], "in_progress")

        try:
            worktree = git_worktree.create_ticket_worktree(project["repo_path"], ticket["id"])
        except git_worktree.DirtyRepoError as exc:
            db.update_ticket_status(conn, ticket["id"], "failed")
            db.add_event(conn, ticket["id"], "fail", str(exc))
            return {"action": "failed", "ticket_id": ticket["id"],
                    "reason": "dirty_repo", "detail": str(exc)}

        db.set_ticket_worktree(conn, ticket["id"], worktree["worktree_path"],
                                worktree["branch_name"], worktree.get("base_commit"))
        db.add_event(conn, ticket["id"], "start",
                     f"Worker picked up ticket in project {project['name']!r}, "
                     f"building on branch {worktree['branch_name']!r}")

        build_path = worktree["worktree_path"]

        prompt = _build_claude_prompt(dict(ticket))
        claude_result = run_claude_code(
            build_path, prompt, cfg.CLAUDE_MAX_TURNS, cfg.CLAUDE_TIMEOUT_S,
        )

        if not claude_result["success"]:
            db.update_ticket_status(conn, ticket["id"], "failed")
            db.add_event(conn, ticket["id"], "fail",
                         f"Claude Code CLI failed: {claude_result['stderr'][:1000]}")
            return {"action": "failed", "ticket_id": ticket["id"],
                    "reason": "claude_code_error", "detail": claude_result}

        test_result = run_tests(build_path, project["test_command"])
        commit_result = git_worktree.commit_worktree_changes(
            build_path, f"ticketboard: {ticket['title']}"
        )
        diff_base = worktree.get("base_commit") or "HEAD"
        diff = git_worktree.diff_stat(build_path, base_ref=diff_base)

        if test_result["ran"] and not test_result["success"]:
            db.update_ticket_status(conn, ticket["id"], "failed")
            db.add_event(conn, ticket["id"], "fail",
                         f"Tests failed:\n{test_result['output'][:1500]}\n\n"
                         f"Diff so far (branch {worktree['branch_name']}):\n{diff[:500]}")
            return {"action": "failed", "ticket_id": ticket["id"],
                    "reason": "tests_failed", "detail": test_result}

        judge_verdict = None
        if cfg.JUDGE_ENABLED:
            db.add_event(conn, ticket["id"], "comment", "Running acceptance-criteria judge pass…")
            judge_result = judge.run_judge(
                dict(ticket), build_path, worktree.get("base_commit"),
            )
            if judge_result["success"]:
                judge_verdict = judge_result["verdict"]
                db.set_ticket_judge_result(conn, ticket["id"], judge_result["verdict"],
                                           judge_result["reason"])
                if judge_result["verdict"] == "fail":
                    db.update_ticket_status(conn, ticket["id"], "needs_review")
                    db.add_event(conn, ticket["id"], "fail",
                                 f"Judge flagged this build: {judge_result['reason']}\n\n"
                                 f"Diff (branch {worktree['branch_name']}):\n{diff[:500]}")
                    return {"action": "needs_review", "ticket_id": ticket["id"],
                            "reason": "judge_failed", "detail": judge_result}
            else:
                # Judge itself couldn't run (CLI error, parse failure) — this
                # is inconclusive, not a failure of the build. Don't block a
                # genuinely good build on a judge outage; log it so it's
                # visible, and proceed to done. This mirrors "fail open" for
                # infra problems vs "fail closed" for actual quality issues.
                db.add_event(conn, ticket["id"], "comment",
                             f"Judge pass could not run (treated as inconclusive, "
                             f"not blocking): {judge_result['reason']}")

        db.update_ticket_status(conn, ticket["id"], "done")
        db.add_event(conn, ticket["id"], "finish",
                     f"Completed on branch {worktree['branch_name']}. "
                     f"Tests: {'passed' if test_result['success'] else 'not run'}. "
                     f"Judge: {judge_verdict or 'skipped'}. "
                     f"Committed: {commit_result['committed']}. "
                     f"Diff:\n{diff[:1000]}")
        return {"action": "done", "ticket_id": ticket["id"],
                "branch": worktree["branch_name"], "judge_verdict": judge_verdict}
    finally:
        db.release_lock(conn)


def drain_loop(conn, wall_clock_budget_s: int = 900, idle_sleep_s: int = 5) -> list[dict]:
    """Keeps building ready tickets back-to-back within a time budget instead
    of exiting after one, so a queue of N tickets doesn't wait N * poll
    interval to drain. Stops early once no ready tickets remain."""
    start = time.monotonic()
    results = []
    while time.monotonic() - start < wall_clock_budget_s:
        result = run_worker_tick(conn)
        results.append(result)
        if result["action"] == "skipped" and result["reason"] == "no ready tickets":
            break
        if result["action"] == "skipped" and result["reason"] == "worker already locked":
            break
        time.sleep(1)  # brief pause between builds, not a full poll interval
    return results


def trigger_worker_async() -> None:
    """Fire-and-forget: spawns `python -m ticketboard.worker --drain` as a
    fully detached background process and returns immediately, without
    waiting for it or capturing its output. Called by the API the instant a
    ticket is promoted to Ready, so the worker starts within ~1-2s instead of
    waiting up to the Task Scheduler poll interval.

    If the spawn itself fails (e.g. python not found), this must not raise —
    the promote API call already succeeded and the ticket is correctly
    Ready; the Task Scheduler poll will pick it up on its next tick as the
    fallback path. Log-and-swallow, don't break the promote response.
    """
    try:
        creationflags = 0
        kwargs = {}
        if sys.platform == "win32":
            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: the child must
            # outlive this request/response cycle and must not be killed if
            # the parent (uvicorn worker handling this request) is reloaded
            # or the terminal session closes.
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [sys.executable, "-m", "ticketboard.worker", "--drain"],
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        # Deliberately swallow — see docstring. The Task Scheduler poll is
        # the safety net for exactly this failure mode.
        pass


def main():
    parser = argparse.ArgumentParser(description="TICKETBOARD worker")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--drain", action="store_true",
                         help="build all ready tickets back-to-back within a time budget")
    parser.add_argument("--loop", action="store_true", help="poll continuously (dev mode)")
    parser.add_argument("--interval", type=int, default=20, help="loop interval seconds")
    args = parser.parse_args()

    conn = db.get_connection(config.DB_PATH)
    db.apply_schema(conn)

    if args.loop:
        while True:
            result = run_worker_tick(conn)
            print(result)
            time.sleep(args.interval)
    elif args.drain:
        results = drain_loop(conn)
        for r in results:
            print(r)
    else:
        result = run_worker_tick(conn)
        print(result)


if __name__ == "__main__":
    main()
