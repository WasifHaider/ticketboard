"""Shared subprocess wrapper for invoking the Claude Code CLI."""
import shutil
import subprocess

_CLAUDE_BIN = None


def _claude_bin() -> str:
    global _CLAUDE_BIN
    if _CLAUDE_BIN is None:
        _CLAUDE_BIN = shutil.which("claude") or "claude"
    return _CLAUDE_BIN


def run_claude_code(repo_path: str, prompt: str, max_turns: int, timeout_s: int) -> dict:
    """Run Claude Code CLI in print mode with structured JSON output.
    Used for actual build tasks (worker.py). repo_path is expected to be an
    isolated git worktree (see git_worktree.py), never the user's live repo
    — --allow-dangerously-skip-permissions is safe here specifically because
    the worker never invokes this against anything but a throwaway worktree
    that gets diffed/tested/discarded, not committed to main directly."""
    cmd = [
        _claude_bin(), "-p",
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
    ]
    return _run(cmd, repo_path, timeout_s, capture_key="stdout", stdin_text=prompt)


def run_claude_chat(repo_path: str, prompt: str, max_turns: int = 15,
                     timeout_s: int = 600) -> dict:
    """Run Claude Code CLI in print mode with plain text output, for
    conversational discussion turns (chat.py). Returns {'success', 'reply', 'stderr'}."""
    cmd = [_claude_bin(), "-p", "--max-turns", str(max_turns)]
    result = _run(cmd, repo_path, timeout_s, capture_key="stdout", stdin_text=prompt)
    result["reply"] = result.pop("stdout", "").strip()
    return result


def _run(cmd: list, repo_path: str, timeout_s: int, capture_key: str,
         stdin_text: str | None = None) -> dict:
    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout_s,
            input=stdin_text,
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            capture_key: result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "returncode": None,
            capture_key: exc.stdout or "",
            "stderr": f"timed out after {timeout_s}s",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "returncode": None,
            capture_key: "",
            "stderr": "claude CLI not found on PATH",
        }
