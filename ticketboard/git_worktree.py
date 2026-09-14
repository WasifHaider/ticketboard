"""Git worktree management: build each ticket in an isolated worktree on its
own branch off a clean base, so verification's git diff is exact and the
target repo's own working tree is never touched by the worker.
"""
import subprocess
from pathlib import Path

from ticketboard import config


class DirtyRepoError(RuntimeError):
    """Raised when the target repo has uncommitted changes and the worker
    refuses to build against it (see repo_is_clean)."""


def _run_git(args: list, cwd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def repo_is_clean(repo_path: str) -> bool:
    """True if the repo has no uncommitted changes (staged, unstaged, or
    untracked). The worker must refuse to build against a dirty repo —
    otherwise diff-based verification for this ticket is meaningless and a
    build could stomp the user's own WIP."""
    result = _run_git(["status", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed in {repo_path}: {result.stderr}")
    return result.stdout.strip() == ""


def worktrees_root(repo_path: str) -> Path:
    """Worktrees live OUTSIDE the target repo's own directory tree, in
    TICKETBOARD's own scratch space, keyed by a hash of the repo path. Putting
    them inside repo_path would make the target repo's own `git status`
    report the worktree's contents as untracked/dirty, defeating
    refuse-if-dirty on the worker's own second run."""
    import hashlib
    repo_key = hashlib.sha1(str(Path(repo_path).resolve()).encode()).hexdigest()[:12]
    return config.BASE_DIR / "worktrees" / repo_key


def create_ticket_worktree(repo_path: str, ticket_id: int) -> dict:
    """Fetches/updates the current branch, then creates a new worktree at
    <ticketboard>/worktrees/<repo-hash>/ticket-<id> on branch ticket/<id> off
    the current HEAD of repo_path. Returns {'worktree_path', 'branch_name',
    'base_commit'}.

    base_commit is the repo_path HEAD sha at the moment of worktree creation
    — the correct diff base for the whole ticket's lifetime. Diffing against
    a bare "HEAD" ref breaks once commit_worktree_changes commits: HEAD then
    points at the new commit itself, making any post-commit diff show
    nothing. base_commit stays fixed and always reflects the true pre-build
    state, for both the worker's own diff_stat/full_diff calls and the judge
    pass, which needs the exact set of changes this ticket introduced.

    Raises DirtyRepoError if repo_path itself is dirty (refuse-if-dirty)."""
    if not repo_is_clean(repo_path):
        raise DirtyRepoError(
            f"refusing to build: {repo_path} has uncommitted changes. "
            f"Commit, stash, or discard them before promoting tickets in this project."
        )

    # Best-effort pull so the base is not stale relative to remote. Not fatal
    # if it fails (e.g. no remote configured, offline) — the worktree still
    # gets created off local HEAD.
    _run_git(["pull", "--ff-only"], cwd=repo_path, timeout=30)

    base_commit_result = _run_git(["rev-parse", "HEAD"], cwd=repo_path, timeout=15)
    base_commit = base_commit_result.stdout.strip() or None

    branch_name = f"ticket/{ticket_id}"
    worktree_dir = worktrees_root(repo_path) / f"ticket-{ticket_id}"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if worktree_dir.exists():
        remove_ticket_worktree(repo_path, str(worktree_dir), branch_name)

    result = _run_git(
        ["worktree", "add", "-b", branch_name, str(worktree_dir)],
        cwd=repo_path, timeout=60,
    )
    if result.returncode != 0:
        # branch may already exist from a prior failed/retried attempt
        result2 = _run_git(
            ["worktree", "add", str(worktree_dir), branch_name],
            cwd=repo_path, timeout=60,
        )
        if result2.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {result.stderr}\n{result2.stderr}"
            )

    return {"worktree_path": str(worktree_dir), "branch_name": branch_name,
            "base_commit": base_commit}


def remove_ticket_worktree(repo_path: str, worktree_path: str, branch_name: str) -> None:
    """Best-effort cleanup: removes the worktree directory and its git
    registration. Does not delete the branch — that stays around as the
    reviewable artifact until the user merges/deletes it themselves."""
    _run_git(["worktree", "remove", "--force", worktree_path], cwd=repo_path, timeout=30)
    _run_git(["worktree", "prune"], cwd=repo_path, timeout=30)


def commit_worktree_changes(worktree_path: str, message: str) -> dict:
    """Commits whatever changes exist in the worktree (the worker's own
    branch, never main) so the diff is a real, revertable artifact instead
    of an uncommitted pile the user has to archaeology through."""
    add_result = _run_git(["add", "-A"], cwd=worktree_path, timeout=30)
    if add_result.returncode != 0:
        return {"committed": False, "reason": f"git add failed: {add_result.stderr}"}

    status = _run_git(["status", "--porcelain"], cwd=worktree_path, timeout=30)
    if status.stdout.strip() == "":
        return {"committed": False, "reason": "no changes to commit"}

    commit_result = _run_git(["commit", "-m", message], cwd=worktree_path, timeout=30)
    if commit_result.returncode != 0:
        return {"committed": False, "reason": f"git commit failed: {commit_result.stderr}"}

    return {"committed": True, "reason": None}


def diff_stat(worktree_path: str, base_ref: str = "HEAD") -> str:
    """Returns a diff summary of the worktree's branch vs its base — this is
    the exact ticket diff, unpolluted by any other ticket or the user's own
    WIP, because the worktree started from a clean base."""
    result = _run_git(["diff", "--stat", base_ref], cwd=worktree_path, timeout=30)
    return result.stdout.strip() or "(no changes)"


def full_diff(worktree_path: str, base_ref: str, max_chars: int = 20000) -> str:
    """Returns the actual patch content (not just a summary) of the worktree's
    branch vs its base, for feeding to the judge pass — the judge needs to
    see what changed line-by-line to rule on acceptance criteria, a summary
    isn't enough. Truncated to max_chars from the end (most relevant recent
    hunks) if the diff is larger, since judge prompts have a practical size
    ceiling and a truncation note is better than silently cutting mid-hunk
    without saying so."""
    result = _run_git(["diff", base_ref], cwd=worktree_path, timeout=30)
    diff = result.stdout
    if not diff.strip():
        return "(no changes)"
    if len(diff) > max_chars:
        return (
            f"[diff truncated to last {max_chars} chars of {len(diff)} total]\n"
            + diff[-max_chars:]
        )
    return diff
