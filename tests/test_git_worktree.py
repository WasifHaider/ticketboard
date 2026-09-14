import subprocess

import pytest

from ticketboard import git_worktree


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run(["git", "init", "-q"], cwd=str(repo_dir))
    run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir))
    run(["git", "config", "user.name", "Test"], cwd=str(repo_dir))
    (repo_dir / "README.md").write_text("hello\n")
    run(["git", "add", "-A"], cwd=str(repo_dir))
    run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo_dir))
    return repo_dir


def test_repo_is_clean_true_for_committed_repo(repo):
    assert git_worktree.repo_is_clean(str(repo)) is True


def test_repo_is_clean_false_for_dirty_repo(repo):
    (repo / "dirty.txt").write_text("uncommitted")
    assert git_worktree.repo_is_clean(str(repo)) is False


def test_create_ticket_worktree_refuses_dirty_repo(repo):
    (repo / "dirty.txt").write_text("uncommitted")
    with pytest.raises(git_worktree.DirtyRepoError):
        git_worktree.create_ticket_worktree(str(repo), ticket_id=1)


def test_create_ticket_worktree_creates_branch_and_dir(repo):
    result = git_worktree.create_ticket_worktree(str(repo), ticket_id=1)
    assert result["branch_name"] == "ticket/1"
    assert "ticket-1" in result["worktree_path"]

    from pathlib import Path
    assert Path(result["worktree_path"]).exists()
    assert (Path(result["worktree_path"]) / "README.md").exists()


def test_commit_worktree_changes_commits_new_file(repo):
    result = git_worktree.create_ticket_worktree(str(repo), ticket_id=2)
    worktree_path = result["worktree_path"]

    from pathlib import Path
    (Path(worktree_path) / "new_feature.py").write_text("print('hi')\n")

    commit_result = git_worktree.commit_worktree_changes(worktree_path, "add feature")
    assert commit_result["committed"] is True

    # verify it's a real commit, not just staged
    log = run(["git", "log", "--oneline", "-1"], cwd=worktree_path)
    assert "add feature" in log.stdout


def test_commit_worktree_changes_noop_when_nothing_changed(repo):
    result = git_worktree.create_ticket_worktree(str(repo), ticket_id=3)
    commit_result = git_worktree.commit_worktree_changes(result["worktree_path"], "noop")
    assert commit_result["committed"] is False
    assert "no changes" in commit_result["reason"]


def test_diff_stat_reflects_only_this_ticket_changes(repo):
    base_branch = run(["git", "symbolic-ref", "--short", "HEAD"], cwd=str(repo)).stdout.strip()
    result = git_worktree.create_ticket_worktree(str(repo), ticket_id=4)
    worktree_path = result["worktree_path"]

    from pathlib import Path
    (Path(worktree_path) / "isolated.py").write_text("x = 1\n")
    git_worktree.commit_worktree_changes(worktree_path, "isolated change")

    diff = git_worktree.diff_stat(worktree_path, base_ref=base_branch)
    assert "isolated.py" in diff


def test_remove_ticket_worktree_cleans_up(repo):
    result = git_worktree.create_ticket_worktree(str(repo), ticket_id=5)
    from pathlib import Path
    worktree_path = Path(result["worktree_path"])
    assert worktree_path.exists()

    git_worktree.remove_ticket_worktree(str(repo), str(worktree_path), result["branch_name"])
    assert not worktree_path.exists()


def test_create_ticket_worktree_recreates_if_already_exists(repo):
    result1 = git_worktree.create_ticket_worktree(str(repo), ticket_id=6)
    from pathlib import Path
    (Path(result1["worktree_path"]) / "leftover.txt").write_text("x")

    # second call for the same ticket id should not blow up
    result2 = git_worktree.create_ticket_worktree(str(repo), ticket_id=6)
    assert Path(result2["worktree_path"]).exists()
    assert not (Path(result2["worktree_path"]) / "leftover.txt").exists()
