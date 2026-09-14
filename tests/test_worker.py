import subprocess
from unittest.mock import patch

import pytest

from ticketboard import db, worker


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = db.get_connection(str(db_path))
    db.apply_schema(c)
    yield c
    c.close()


def make_ready_ticket(conn, repo_path="/tmp/repo", test_command="pytest -q"):
    p = db.create_project(conn, "demo", repo_path, test_command=test_command)
    t = db.create_ticket(conn, p["id"], "T1", "do the thing", "criteria")
    db.update_ticket_status(conn, t["id"], "ready")
    return p, t


def _mock_worktree(worktree_path="/tmp/worktree/ticket-1", branch_name="ticket/1",
                    base_commit="abc123"):
    return {"worktree_path": worktree_path, "branch_name": branch_name,
            "base_commit": base_commit}


PASSING_JUDGE = {"success": True, "verdict": "pass", "reason": "all criteria met", "raw": "{}"}


def test_no_ready_tickets_is_noop(conn):
    result = worker.run_worker_tick(conn)
    assert result["action"] == "skipped"
    assert result["reason"] == "no ready tickets"


def test_already_locked_skips(conn):
    p, t = make_ready_ticket(conn)
    db.try_acquire_lock(conn, t["id"])
    result = worker.run_worker_tick(conn)
    assert result["action"] == "skipped"
    assert result["reason"] == "worker already locked"
    db.release_lock(conn)


def test_success_path_marks_done_and_releases_lock(conn):
    p, t = make_ready_ticket(conn)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="(no changes)"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": True, "output": ""}), \
         patch("ticketboard.worker.judge.run_judge", return_value=PASSING_JUDGE):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "done"
    assert result["branch"] == "ticket/1"
    assert result["judge_verdict"] == "pass"
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "done"
    assert updated["worktree_path"] == "/tmp/worktree/ticket-1"
    assert updated["branch_name"] == "ticket/1"
    assert updated["base_commit"] == "abc123"
    assert updated["judge_verdict"] == "pass"
    assert db.get_locked_ticket_id(conn) is None

    events = db.list_events(conn, t["id"])
    event_types = [e["event_type"] for e in events]
    assert event_types == ["start", "comment", "finish"]


def test_dirty_repo_marks_failed_and_releases_lock(conn):
    p, t = make_ready_ticket(conn)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               side_effect=worker.git_worktree.DirtyRepoError("repo is dirty")):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "failed"
    assert result["reason"] == "dirty_repo"
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "failed"
    assert db.get_locked_ticket_id(conn) is None


def test_claude_failure_marks_failed_and_releases_lock(conn):
    p, t = make_ready_ticket(conn)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": False, "returncode": 1,
                                     "stdout": "", "stderr": "boom"}):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "failed"
    assert result["reason"] == "claude_code_error"
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "failed"
    assert db.get_locked_ticket_id(conn) is None


def test_test_failure_marks_failed_and_releases_lock(conn):
    p, t = make_ready_ticket(conn)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="M file.py"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": False, "output": "FAILED"}):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "failed"
    assert result["reason"] == "tests_failed"
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "failed"
    assert db.get_locked_ticket_id(conn) is None


def test_lock_released_even_on_unexpected_exception(conn):
    p, t = make_ready_ticket(conn)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch.object(worker, "run_claude_code", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            worker.run_worker_tick(conn)

    assert db.get_locked_ticket_id(conn) is None
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "in_progress"  # left as-is; needs human look


def test_no_test_command_skips_verification_but_still_marks_done(conn):
    p, t = make_ready_ticket(conn, test_command=None)

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="(no changes)"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch("ticketboard.worker.judge.run_judge", return_value=PASSING_JUDGE):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "done"


def test_get_stale_in_progress(conn):
    p, t = make_ready_ticket(conn)
    db.update_ticket_status(conn, t["id"], "in_progress")
    conn.execute(
        "UPDATE tickets SET updated_at = datetime('now', '-5 hours') WHERE id = ?",
        (t["id"],),
    )
    conn.commit()
    stale = worker.get_stale_in_progress(conn, hours=3)
    assert len(stale) == 1
    assert stale[0]["id"] == t["id"]


def test_run_worker_tick_records_heartbeat(conn):
    assert db.get_last_heartbeat(conn) is None
    worker.run_worker_tick(conn)
    assert db.get_last_heartbeat(conn) is not None


def test_drain_loop_builds_multiple_ready_tickets_back_to_back(conn):
    p = db.create_project(conn, "demo", "/tmp/repo", test_command="pytest -q")
    t1 = db.create_ticket(conn, p["id"], "T1", "o1", "a1")
    t2 = db.create_ticket(conn, p["id"], "T2", "o2", "a2")
    db.update_ticket_status(conn, t1["id"], "ready")
    db.update_ticket_status(conn, t2["id"], "ready")

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="(no changes)"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": True, "output": ""}), \
         patch("ticketboard.worker.judge.run_judge", return_value=PASSING_JUDGE), \
         patch("time.sleep"):
        results = worker.drain_loop(conn, wall_clock_budget_s=60)

    done_results = [r for r in results if r["action"] == "done"]
    assert len(done_results) == 2
    assert results[-1]["reason"] == "no ready tickets"

    assert db.get_ticket(conn, t1["id"])["status"] == "done"
    assert db.get_ticket(conn, t2["id"])["status"] == "done"


def test_trigger_worker_async_spawns_detached_drain_process():
    with patch("ticketboard.worker.subprocess.Popen") as mock_popen:
        worker.trigger_worker_async()

    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[1:] == ["-m", "ticketboard.worker", "--drain"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_trigger_worker_async_swallows_spawn_failure():
    # Must never raise even if Popen itself fails — the ticket is already
    # correctly Ready and the Task Scheduler poll is the fallback path.
    with patch("ticketboard.worker.subprocess.Popen", side_effect=OSError("boom")):
        worker.trigger_worker_async()  # should not raise


# ---------------------------------------------------------------------------
# Judge pass integration (worker-level, judge internals covered separately
# in test_judge.py)
# ---------------------------------------------------------------------------

def test_judge_fail_marks_needs_review_not_failed(conn):
    """A judge failure is a scope/correctness verdict, not a broken build —
    tests genuinely passed, so this must NOT be conflated with 'failed'
    (which means the build itself is broken)."""
    p, t = make_ready_ticket(conn)

    failing_judge = {"success": True, "verdict": "fail",
                      "reason": "criterion X not met", "raw": "{}"}

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="M file.py"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": True, "output": ""}), \
         patch("ticketboard.worker.judge.run_judge", return_value=failing_judge):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "needs_review"
    assert result["reason"] == "judge_failed"
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "needs_review"
    assert updated["judge_verdict"] == "fail"
    assert updated["judge_reason"] == "criterion X not met"
    assert db.get_locked_ticket_id(conn) is None

    events = db.list_events(conn, t["id"])
    assert events[-1]["event_type"] == "fail"
    assert "criterion X not met" in events[-1]["message"]


def test_judge_inconclusive_does_not_block_done(conn):
    """If the judge itself fails to run (CLI error, bad JSON), that's
    infrastructure trouble, not a verdict on the ticket — a good build must
    not be blocked/held hostage by a flaky judge call."""
    p, t = make_ready_ticket(conn)

    inconclusive_judge = {"success": False, "verdict": None,
                           "reason": "judge CLI call failed: timeout", "raw": None}

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="(no changes)"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": True, "output": ""}), \
         patch("ticketboard.worker.judge.run_judge", return_value=inconclusive_judge):
        result = worker.run_worker_tick(conn)

    assert result["action"] == "done"
    assert result["judge_verdict"] is None
    updated = db.get_ticket(conn, t["id"])
    assert updated["status"] == "done"
    assert updated["judge_verdict"] is None  # never set on inconclusive


def test_judge_disabled_skips_judge_entirely(conn):
    p, t = make_ready_ticket(conn)

    class FakeCfg:
        CLAUDE_MAX_TURNS = 40
        CLAUDE_TIMEOUT_S = 1800
        JUDGE_ENABLED = False

    with patch("ticketboard.worker.git_worktree.create_ticket_worktree",
               return_value=_mock_worktree()), \
         patch("ticketboard.worker.git_worktree.commit_worktree_changes",
               return_value={"committed": True, "reason": None}), \
         patch("ticketboard.worker.git_worktree.diff_stat", return_value="(no changes)"), \
         patch.object(worker, "run_claude_code",
                       return_value={"success": True, "returncode": 0,
                                     "stdout": "ok", "stderr": ""}), \
         patch.object(worker, "run_tests",
                       return_value={"ran": True, "success": True, "output": ""}), \
         patch("ticketboard.worker.judge.run_judge") as mock_judge:
        result = worker.run_worker_tick(conn, cfg=FakeCfg)

    assert result["action"] == "done"
    assert result["judge_verdict"] is None
    mock_judge.assert_not_called()
