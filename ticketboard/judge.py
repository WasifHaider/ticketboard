"""Independent acceptance-criteria judge pass.

Runs AFTER tests pass but BEFORE a ticket is marked done. A fresh, separate
Claude Code invocation is given only the acceptance criteria and the actual
diff — never the builder's own summary or self-report — and rules pass/fail
per criterion, citing specific lines. This catches "tests pass but wrong
feature" failures that a test-only gate cannot: a continuation of the same
build session can be talked into agreeing its own flawed work is correct,
but a fresh read of the diff against the criteria, with no context from the
build conversation, is a materially stronger check.

The judge runs read-only (default Claude Code permissions, same as chat.py)
— it must never modify the worktree, only read and rule on it.
"""
import json
import re

from ticketboard import config, git_worktree
from ticketboard.claude_cli import run_claude_chat


def build_judge_prompt(ticket: dict, diff: str) -> str:
    return (
        "You are an independent reviewer judging whether a code change "
        "satisfies a ticket's acceptance criteria. You were NOT involved in "
        "writing this change and have no context beyond what's given here — "
        "judge only what the diff actually does, not what it claims to do.\n\n"
        f"Acceptance criteria:\n{ticket['acceptance_criteria']}\n\n"
        f"Objective (context only, criteria above are the actual bar):\n"
        f"{ticket['objective']}\n\n"
        "Diff:\n"
        f"```diff\n{diff}\n```\n\n"
        "Rule on this by responding with ONLY a JSON object (no other text, "
        "no markdown fences) in exactly this shape:\n"
        '{"verdict": "pass"|"fail", '
        '"criteria": [{"criterion": "<short quote or paraphrase>", '
        '"met": true|false, "citation": "<specific diff line/reasoning, or '
        'empty string if met>"}], '
        '"summary": "<one sentence overall reason>"}\n\n'
        "verdict is \"pass\" only if every criterion is met. Be strict: "
        "if the diff doesn't clearly satisfy a criterion, mark it unmet "
        "rather than giving the benefit of the doubt."
    )


def _extract_json(text: str) -> dict:
    """Claude Code chat replies are plain text, not guaranteed to be pure
    JSON even when asked — strip any leading/trailing prose or code fences
    defensively before parsing."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)
    return json.loads(text)


def run_judge(ticket: dict, worktree_path: str, base_commit: str | None) -> dict:
    """Returns {'success', 'verdict' ('pass'/'fail'/None), 'reason', 'raw'}.

    'success' is about whether the judge ran at all (CLI/parse errors) —
    distinct from 'verdict', which is the judge's actual ruling. A judge that
    fails to run is NOT the same as a ticket that fails review: callers
    should treat success=False as "judge inconclusive," not "ticket failed,"
    and fall back to a safe default (see worker.py's handling)."""
    if base_commit is None:
        return {
            "success": False, "verdict": None,
            "reason": "no base_commit recorded for this ticket; cannot compute diff",
            "raw": None,
        }

    diff = git_worktree.full_diff(worktree_path, base_commit, config.JUDGE_MAX_DIFF_CHARS)
    if diff == "(no changes)":
        return {
            "success": True, "verdict": "fail",
            "reason": "no changes were made — nothing to judge against the criteria",
            "raw": None,
        }

    prompt = build_judge_prompt(ticket, diff)
    result = run_claude_chat(worktree_path, prompt, config.JUDGE_MAX_TURNS, config.JUDGE_TIMEOUT_S)

    if not result["success"]:
        return {
            "success": False, "verdict": None,
            "reason": f"judge CLI call failed: {result['stderr'][:500]}",
            "raw": result.get("reply"),
        }

    try:
        parsed = _extract_json(result["reply"])
    except (json.JSONDecodeError, AttributeError) as exc:
        return {
            "success": False, "verdict": None,
            "reason": f"could not parse judge response as JSON: {exc}",
            "raw": result["reply"],
        }

    verdict = parsed.get("verdict")
    if verdict not in ("pass", "fail"):
        return {
            "success": False, "verdict": None,
            "reason": f"judge returned an unrecognized verdict: {verdict!r}",
            "raw": result["reply"],
        }

    unmet = [c for c in parsed.get("criteria", []) if not c.get("met", True)]
    reason_parts = [parsed.get("summary", "")]
    if unmet:
        reason_parts.append("Unmet criteria: " + "; ".join(
            f"{c.get('criterion', '?')} ({c.get('citation', 'no citation')})"
            for c in unmet
        ))

    return {
        "success": True, "verdict": verdict,
        "reason": " ".join(p for p in reason_parts if p).strip() or "(no reason given)",
        "raw": result["reply"],
    }
