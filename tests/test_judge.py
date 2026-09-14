import json
from unittest.mock import patch

import pytest

from ticketboard import judge


def make_ticket(acceptance_criteria="Returns 200 with status ok",
                 objective="Add health endpoint"):
    return {"id": 1, "title": "T1", "objective": objective,
            "acceptance_criteria": acceptance_criteria, "files_hint": None}


def test_build_judge_prompt_includes_criteria_and_diff():
    ticket = make_ticket()
    prompt = judge.build_judge_prompt(ticket, "diff --git a/x.py b/x.py\n+added line")
    assert "Returns 200 with status ok" in prompt
    assert "added line" in prompt
    assert "JSON" in prompt


def test_extract_json_handles_plain_json():
    text = '{"verdict": "pass", "criteria": [], "summary": "ok"}'
    parsed = judge._extract_json(text)
    assert parsed["verdict"] == "pass"


def test_extract_json_handles_fenced_json():
    text = '```json\n{"verdict": "fail", "criteria": [], "summary": "no"}\n```'
    parsed = judge._extract_json(text)
    assert parsed["verdict"] == "fail"


def test_extract_json_handles_surrounding_prose():
    text = 'Here is my ruling:\n{"verdict": "pass", "criteria": [], "summary": "ok"}\nDone.'
    parsed = judge._extract_json(text)
    assert parsed["verdict"] == "pass"


def test_run_judge_no_base_commit_is_inconclusive():
    result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit=None)
    assert result["success"] is False
    assert result["verdict"] is None
    assert "base_commit" in result["reason"]


def test_run_judge_no_changes_is_a_fail():
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="(no changes)"):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")
    assert result["success"] is True
    assert result["verdict"] == "fail"
    assert "no changes" in result["reason"]


def test_run_judge_passes_on_clean_verdict():
    fake_reply = json.dumps({
        "verdict": "pass",
        "criteria": [{"criterion": "returns 200", "met": True, "citation": ""}],
        "summary": "All good.",
    })
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="diff content"), \
         patch("ticketboard.judge.run_claude_chat",
               return_value={"success": True, "reply": fake_reply, "stderr": ""}):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")

    assert result["success"] is True
    assert result["verdict"] == "pass"
    assert "All good" in result["reason"]


def test_run_judge_fails_on_unmet_criteria_with_citation():
    fake_reply = json.dumps({
        "verdict": "fail",
        "criteria": [
            {"criterion": "returns 200", "met": True, "citation": ""},
            {"criterion": "handles auth", "met": False, "citation": "no auth check found"},
        ],
        "summary": "Missing auth handling.",
    })
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="diff content"), \
         patch("ticketboard.judge.run_claude_chat",
               return_value={"success": True, "reply": fake_reply, "stderr": ""}):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")

    assert result["verdict"] == "fail"
    assert "handles auth" in result["reason"]
    assert "no auth check found" in result["reason"]


def test_run_judge_cli_failure_is_inconclusive_not_a_fail():
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="diff content"), \
         patch("ticketboard.judge.run_claude_chat",
               return_value={"success": False, "reply": "", "stderr": "timeout"}):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")

    assert result["success"] is False
    assert result["verdict"] is None
    assert "timeout" in result["reason"]


def test_run_judge_unparseable_reply_is_inconclusive():
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="diff content"), \
         patch("ticketboard.judge.run_claude_chat",
               return_value={"success": True, "reply": "not json at all", "stderr": ""}):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")

    assert result["success"] is False
    assert result["verdict"] is None


def test_run_judge_unrecognized_verdict_is_inconclusive():
    fake_reply = json.dumps({"verdict": "maybe", "criteria": [], "summary": "unsure"})
    with patch("ticketboard.judge.git_worktree.full_diff", return_value="diff content"), \
         patch("ticketboard.judge.run_claude_chat",
               return_value={"success": True, "reply": fake_reply, "stderr": ""}):
        result = judge.run_judge(make_ticket(), "/tmp/worktree", base_commit="abc123")

    assert result["success"] is False
    assert result["verdict"] is None
