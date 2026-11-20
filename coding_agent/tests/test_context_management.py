from types import SimpleNamespace as NS
import json

import pytest
import httpx
import tools

from context_budget import context_breakdown
from model_api import RequestUsage
from agent_core import ModelReply
from exceptions import AgentAPIError
from nano_code.config import Config
from nano_code.context import SUMMARY_FIELDS, digest
from nano_code.context_pruning import prune_results, read_output
from nano_code.runtime import Session
from nano_code.memory import project_memory
from nano_code.providers import AnthropicModel


def tool_history():
    return [
        {"role": "user", "content": "Run the regression suite."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "cmd", "type": "function",
            "function": {"name": "execute_command", "arguments": '{"argv":["pytest"]}'},
        }]},
        {"role": "tool", "tool_call_id": "cmd", "content": (
            "exit code: 1\n" + "routine log line\n" * 300 + "FAILED test_regression.py::test_bug\n"
            + "more log lines\n" * 300 + "1 failed\n"
        )},
    ]


def test_budget_accounts_for_safety_and_output():
    budget = context_breakdown(tool_history()[:1], [], limit=1000, max_output=128, safety=30)
    assert budget.reserved_output == 128
    assert budget.remaining == 1000 - budget.messages - budget.overhead - 128 - 30
    assert budget.method


def test_pruning_keeps_failure_and_reference_without_mutating_history():
    history = tool_history()
    clipped, count = prune_results(history, cap=800)
    assert count == 1
    assert clipped[1] == history[1]
    assert clipped[2]["tool_call_id"] == "cmd"
    assert "test_regression.py::test_bug" in clipped[2]["content"]
    assert "1 failed" in clipped[2]["content"]
    assert "read_tool_output" in clipped[2]["content"]
    assert len(clipped[2]["content"]) < len(history[2]["content"])
    assert "routine log line" in read_output(history, "2", 0, 50)


def test_chat_usage_reports_cache_and_reasoning_as_subsets():
    events = []
    tracker = RequestUsage(events.append)
    usage = NS(prompt_tokens=20, completion_tokens=8, total_tokens=28,
               prompt_tokens_details=NS(cached_tokens=10), completion_tokens_details=NS(reasoning_tokens=3))
    tracker.chat(usage)
    tracker.chat(usage)
    assert events == [{"type": "usage", "input_tokens": 20, "output_tokens": 8, "total_tokens": 28,
                       "cache_read_tokens": 10, "reasoning_tokens": 3}]


class SummaryModel:
    def __init__(self, invalid=False):
        self.invalid = invalid
        self.summaries = []
        self.requests = []

    def summarize(self, messages, emit):
        self.summaries.append(messages)
        if self.invalid == "api_error":
            raise AgentAPIError("fixture API error")
        return "not JSON" if self.invalid else json.dumps({
            key: "Keep pending tests" if key == "current_task" else "Unknown" for key in SUMMARY_FIELDS
        })

    def __call__(self, messages, allow_tools, emit):
        self.requests.append(messages)
        return ModelReply(text="done")


def long_session(session):
    session.messages.extend([
        {"role": "user", "content": "Old constraint. " * 500},
        {"role": "assistant", "content": "Work in progress. " * 500},
        *tool_history(),
    ])


def test_manual_compaction_keeps_raw_and_restores_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    model = SummaryModel()
    with Session(tmp_path, model=model) as session:
        long_session(session)
        original = list(session.messages)
        assert "→" in session.compact()
        assert session.messages == original
        assert "无需" in session.compact()
        uid = session.uid
    with Session(tmp_path, resume=uid, model=model) as resumed:
        assert digest(resumed.messages) == digest(original)
        view, _ = resumed._context_view(resumed.messages)
        assert any("Keep pending tests" in (m.get("content") or "") for m in view)
        assert [m["tool_call_id"] for m in view if m["role"] == "tool"] == ["cmd"]
        resumed.fork()
        assert resumed.uid != uid and resumed.context.snapshot is not None


@pytest.mark.parametrize("invalid", [False, True, "api_error"])
def test_auto_compaction_and_failure_fallback(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    model = SummaryModel(invalid)
    config = Config(tmp_path, overrides={"context_limit": 26000, "context_prune_at": 0.2,
                                         "context_compact_at": 0.3})
    with Session(tmp_path, config=config, model=model) as session:
        long_session(session)
        assert session.run("Keep the scope limited.")["status"] == "completed"
        assert len(model.summaries) == 1
        assert (session.context.snapshot is None) == bool(invalid)
        assert model.requests[-1][-1]["content"] == "Keep the scope limited."
        assert "Old constraint." in session.messages[1]["content"]


def test_selects_relevant_memory_and_skips_stale_sections(tmp_path):
    memory = tmp_path / ".nano" / "MEMORY.md"
    memory.parent.mkdir()
    memory.write_text(
        "## SQL database [verified 2026-10-01]\nUse the write-ahead log.\n"
        "## SQL [stale]\nDeprecated instructions.\n"
        "## Marketing\nCampaign colors are blue.\n", encoding="utf-8",
    )
    selected = project_memory(tmp_path, "Update SQL database migration")
    assert "write-ahead" in selected and "verified 2026-10-01" in selected
    assert "Deprecated" not in selected and "Campaign" not in selected


def test_anthropic_cached_input_counts_once():
    events = []
    tracker = RequestUsage(events.append)
    tracker.anthropic({"input_tokens": 10, "cache_read_input_tokens": 70,
                       "cache_creation_input_tokens": 20, "output_tokens": 1})
    for _ in range(2):
        tracker.anthropic({"output_tokens": 4})
    assert events[0] == {"type": "usage", "input_tokens": 100, "output_tokens": 1, "total_tokens": 101,
                         "cache_read_tokens": 70, "cache_write_tokens": 20}
    assert events[1:] == [{"type": "usage", "output_tokens": 3, "total_tokens": 3}]


@pytest.mark.parametrize("official", [True, False])
def test_anthropic_cache_marks_only_official_static_prefix(tmp_path, monkeypatch, official):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("API_KEY", "fixture")
    endpoint = "https://api.anthropic.com" if official else "https://example.test"
    config = Config(tmp_path, overrides={
        "provider": "anthropic", "base_url": endpoint, "model": "claude-test",
        "streaming": False, "anthropic_prompt_cache": True,
    })
    sent = []

    def route(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}]})

    model = AnthropicModel(config, [])
    model.client.close()
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        model.client = client
        for task in ("task one", "task two"):
            model([{"role": "system", "content": "static"}, {"role": "user", "content": task}],
                  False, lambda event: None)
    expected = [{"type": "text", "text": "static", "cache_control": {"type": "ephemeral"}}] if official else "static"
    assert sent[0]["system"] == sent[1]["system"] == expected
    assert sent[0]["messages"] != sent[1]["messages"]


def test_large_file_and_search_support_paging(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_workspace_root", tmp_path)
    (tmp_path / "data.txt").write_text("".join(f"match row {n}\n" for n in range(1, 900)), encoding="utf-8")
    first = tools.read_file("data.txt")
    assert "match row 200\n" in first and "match row 201\n" not in first
    assert "offset=200" in first
    second = tools.read_file("data.txt", offset=200, limit=3)
    assert "match row 201" in second and "match row 203" in second
    assert "match row 204" not in second
    hits = tools.search_in_file("data.txt", "match", offset=200, limit=2)
    assert ">>>    201" in hits and ">>>    202" in hits and "offset=202" in hits


def test_compacted_working_memory_uses_recorded_edits_and_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    config = Config(tmp_path, overrides={"permission_mode": "acceptEdits"})

    class EditModel(SummaryModel):
        def __call__(self, messages, allow_tools, emit):
            if messages[-1]["role"] == "tool":
                return ModelReply(text="File added; verification pending.")
            return ModelReply(tool_calls=[{
                "id": "write", "type": "function", "function": {
                    "name": "write_file", "arguments": '{"file_path":"state.txt","content":"done"}',
                },
            }])

    class FollowupModel(SummaryModel):
        def __call__(self, messages, allow_tools, emit):
            self.requests.append(messages)
            results = [m for m in messages if m.get("tool_call_id") == "reopen"]
            if results:
                assert results[0]["content"].endswith("done")
                return ModelReply(text="Verified file still exists.")
            return ModelReply(tool_calls=[{
                "id": "reopen", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"file_path":"state.txt"}',
                },
            }])

    with Session(tmp_path, config=config, model=EditModel()) as session:
        long_session(session)
        session.tasks("Run pending checks", "pending")
        assert session.run("Write state.txt and retain pending verification.")["status"] == "completed"
        assert "→" in session.compact()
        uid = session.uid
    model = FollowupModel()
    with Session(tmp_path, resume=uid, config=config, model=model) as resumed:
        assert resumed.run("Continue checking the remaining work.")["status"] == "completed"
        memory = model.requests[-1][-1]["content"]
        assert '"recorded_edit_paths": ["state.txt"]' in memory
        assert "[pending] Run pending checks" in memory


def test_successive_compactions_keep_exact_active_request(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    with Session(tmp_path, model=SummaryModel()) as session:
        long_session(session)
        assert "→" in session.compact()
        earlier = session.context.snapshot.end
        session.messages += [{"role": "assistant", "content": "More findings " * 450},
                             {"role": "user", "content": "Keep the output format exactly."}]
        assert "→" in session.compact()
        assert session.context.snapshot.count == 2
        assert session.context.snapshot.end > earlier
        view, _ = session._context_view(session.messages)
        assert view[-1]["content"] == "Keep the output format exactly."
        assert "无需" in session.compact()


def test_compact_a_single_long_completed_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    with Session(tmp_path, model=SummaryModel()) as session:
        goal = {"role": "user", "content": "Preserve the result format and all existing inputs."}
        session.messages.extend([goal, {"role": "assistant", "content": "Observed evidence. " * 600}])
        assert "→" in session.compact()
        view, _ = session._context_view(session.messages)
        assert view[-1] == goal
        assert session.messages[-1]["content"].startswith("Observed evidence.")
        assert "无需" in session.compact()
