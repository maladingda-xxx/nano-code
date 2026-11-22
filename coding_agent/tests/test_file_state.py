"""Read-before-edit and duplicate-read behavior."""

import json

import pytest

import agent
import file_state
import session_store
import tools
from agent_core import ModelReply, run_turn
from edit_context import edit_scope
from nano_code.config import Config
from nano_code.context_pruning import prune_results
from nano_code.runtime import Session


@pytest.fixture(autouse=True)
def clean_file_state():
    file_state.reset_all()
    yield
    file_state.reset_all()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "_workspace_root", tmp_path)
    monkeypatch.setattr(session_store, "_workspace_root", tmp_path)
    return tmp_path


def call(call_id, name, **arguments):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _run(calls, *, session_id="file-state", dispatch=None):
    pending = list(calls)
    events = []

    def request(messages, allow_tools, emit):
        if pending:
            return ModelReply(tool_calls=[pending.pop(0)])
        return ModelReply("done")

    status = run_turn(
        [{"role": "system", "content": "system"}],
        "work",
        request_model=request,
        dispatch=dispatch or agent.execute_tool_call,
        emit=events.append,
        max_tool_rounds=max(1, len(calls)),
        session_id=session_id,
    )
    return status, events


def _tool_results(events):
    return [event for event in events if event["type"] == "tool_result"]


def test_edit_without_read_is_blocked(workspace):
    (workspace / "file.txt").write_text("before\n", encoding="utf-8")

    status, events = _run(
        [call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after")],
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    result = _tool_results(events)[0]
    assert result["outcome"]["status"] == "blocked"
    assert "尚未读取" in result["result"]
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "before\n"


def test_read_then_edit_in_one_batch_succeeds(workspace):
    (workspace / "file.txt").write_text("before\n", encoding="utf-8")

    status, events = _run(
        [
            call("read", "read_file", file_path="file.txt"),
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
        ],
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    results = {event["tool_call_id"]: event for event in _tool_results(events)}
    assert results["read"]["outcome"]["status"] == "ok"
    assert results["edit"]["outcome"]["status"] == "ok"
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "after\n"


def test_read_state_survives_a_turn_boundary(workspace):
    (workspace / "file.txt").write_text("before\n", encoding="utf-8")

    _run([call("read", "read_file", file_path="file.txt")], session_id="same-session")
    status, events = _run(
        [call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after")],
        session_id="same-session",
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    assert _tool_results(events)[0]["outcome"]["status"] == "ok"
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "after\n"


def test_external_change_after_read_blocks_edit(workspace):
    path = workspace / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    _run([call("read", "read_file", file_path="file.txt")], session_id="same-session")

    path.write_text("changed and longer\n", encoding="utf-8")
    status, events = _run(
        [call("edit", "edit_file", file_path="file.txt", old_text="changed", new_text="after")],
        session_id="same-session",
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    result = _tool_results(events)[0]
    assert result["outcome"]["status"] == "blocked"
    assert "已发生变化" in result["result"]
    assert path.read_text(encoding="utf-8") == "changed and longer\n"


def test_overwrite_requires_a_full_read(workspace):
    path = workspace / "file.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    status, events = _run(
        [
            call("read", "read_file", file_path="file.txt", offset=0, limit=1),
            call("write", "write_file", file_path="file.txt", content="new\n"),
        ],
        session_id="same-session",
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    results = {event["tool_call_id"]: event for event in _tool_results(events)}
    assert results["write"]["outcome"]["status"] == "blocked"
    assert "完整读取" in results["write"]["result"]
    assert path.read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    status, events = _run(
        [
            call("read", "read_file", file_path="file.txt"),
            call("write", "write_file", file_path="file.txt", content="new\n"),
        ],
        session_id="same-session",
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )
    assert status == "completed"
    assert all(event["outcome"]["status"] == "ok" for event in _tool_results(events))
    assert path.read_text(encoding="utf-8") == "new\n"


def test_new_file_write_then_edit_does_not_require_a_read(workspace):
    status, events = _run(
        [
            call("write", "write_file", file_path="new.txt", content="hello\n"),
            call("edit", "edit_file", file_path="new.txt", old_text="hello", new_text="bye"),
        ],
        dispatch=lambda call, emit=None: agent.execute_tool_call(
            call, emit, enforce_read_state=True,
        ),
    )

    assert status == "completed"
    assert all(event["outcome"]["status"] == "ok" for event in _tool_results(events))
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "bye\n"


def test_repeated_identical_read_returns_unchanged(workspace):
    (workspace / "file.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    status, events = _run(
        [
            call("read1", "read_file", file_path="file.txt"),
            call("read2", "read_file", file_path="file.txt"),
        ],
        dispatch=lambda call, emit=None: agent.execute_tool_call(call, emit),
    )

    assert status == "completed"
    results = {event["tool_call_id"]: event for event in _tool_results(events)}
    assert results["read1"]["outcome"]["status"] == "ok"
    assert results["read2"]["outcome"]["status"] == "unchanged"
    assert "文件未变化" in results["read2"]["result"]
    assert "alpha" not in results["read2"]["result"]


def test_duplicate_reads_in_one_model_batch_both_return_content(workspace):
    (workspace / "file.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    replies = iter([
        ModelReply(tool_calls=[
            call("read1", "read_file", file_path="file.txt"),
            call("read2", "read_file", file_path="file.txt"),
        ]),
        ModelReply(text="done"),
    ])
    events = []

    run_turn(
        [{"role": "system", "content": "system"}],
        "work",
        request_model=lambda *_: next(replies),
        dispatch=agent.execute_tool_call,
        emit=events.append,
        max_tool_rounds=1,
        session_id="same-batch",
    )

    results = {event["tool_call_id"]: event for event in _tool_results(events)}
    assert results["read1"]["outcome"]["status"] == "ok"
    assert results["read2"]["outcome"]["status"] == "ok"
    assert "alpha" in results["read1"]["result"]
    assert "alpha" in results["read2"]["result"]


def test_different_page_is_not_deduped(workspace):
    (workspace / "file.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    with edit_scope("session", "turn"):
        first = tools.read_file("file.txt", limit=2)
        second = tools.read_file("file.txt", offset=1, limit=2)

    assert "one" in first and "two" in first
    assert "two" in second and "three" in second
    assert not isinstance(second, file_state.UnchangedOutput)


def test_read_state_is_isolated_by_session(workspace):
    path = workspace / "file.txt"
    path.write_text("before\n", encoding="utf-8")

    with edit_scope("session-a", "turn-a"):
        tools.read_file("file.txt")

    with edit_scope("session-b", "turn-b"):
        blocked = agent.execute_tool_call(
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
            enforce_read_state=True,
        )
    assert blocked.status == "blocked"
    assert "尚未读取" in blocked.text
    assert path.read_text(encoding="utf-8") == "before\n"

    with edit_scope("session-b", "turn-b"):
        tools.read_file("file.txt")
        allowed = agent.execute_tool_call(
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
            enforce_read_state=True,
        )
    assert allowed.status == "ok"
    assert path.read_text(encoding="utf-8") == "after\n"


def test_pruned_read_result_invalidates_state(workspace):
    path = workspace / "file.txt"
    path.write_text("x" * 2000, encoding="utf-8")

    with edit_scope("session", "turn", round=1):
        tools.read_file("file.txt")
        assert file_state.lookup(path) is not None

    messages = [
        {
            "role": "assistant", "content": None,
            "tool_calls": [call("read", "read_file", file_path="file.txt")],
        },
        {"role": "tool", "tool_call_id": "read", "content": "x" * 2000},
    ]
    pruned, count = prune_results(messages, cap=100)

    assert count == 1
    assert len(pruned[1]["content"]) < 2000
    with edit_scope("session", "turn", round=1):
        assert file_state.lookup(path) is None


def test_folded_read_expires_old_state_without_discarding_a_fresh_read(workspace):
    path = workspace / "file.txt"
    path.write_text("before\n" + "routine source text\n" * 110, encoding="utf-8")
    with edit_scope("session", "turn", "old-read", 1):
        previous = tools.read_file("file.txt")
    old = [{"role": "assistant", "tool_calls": [call("old-read", "read_file", file_path="file.txt")]},
           {"role": "tool", "tool_call_id": "old-read", "content": previous}]
    latest = [{"role": "assistant", "tool_calls": [call("search", "search_workspace", query="before")]},
              {"role": "tool", "tool_call_id": "search", "content": "a match"}]
    prune_results([*old, *latest], cap=4000, aggressive=True)
    with edit_scope("session", "turn", "repeat", 3):
        assert not isinstance(fresh := tools.read_file("file.txt"), file_state.UnchangedOutput)
    latest = [{"role": "assistant", "tool_calls": [call("repeat", "read_file", file_path="file.txt")]},
              {"role": "tool", "tool_call_id": "repeat", "content": fresh}]
    prune_results([*old, *latest], cap=4000, aggressive=True)
    with edit_scope("session", "turn", "edit", 4):
        result = agent.execute_tool_call(
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
            enforce_read_state=True,
        )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8").startswith("after\n")


def test_reset_session_forces_a_new_read(workspace):
    path = workspace / "file.txt"
    path.write_text("before\n", encoding="utf-8")

    with edit_scope("session", "turn"):
        tools.read_file("file.txt")
    file_state.reset_session("session")

    with edit_scope("session", "turn"):
        blocked = agent.execute_tool_call(
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
            enforce_read_state=True,
        )
    assert blocked.status == "blocked"
    assert path.read_text(encoding="utf-8") == "before\n"


def test_session_runtime_enforces_read_before_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    (tmp_path / "file.txt").write_text("before\n", encoding="utf-8")
    config = Config(tmp_path, overrides={"permission_mode": "acceptEdits"})
    replies = iter([
        ModelReply(tool_calls=[
            call("edit", "edit_file", file_path="file.txt", old_text="before", new_text="after"),
        ]),
        ModelReply(text="done"),
    ])
    events = []

    with Session(tmp_path, config=config, model=lambda *_: next(replies), emit=events.append) as session:
        session.run("edit the file")

    result = next(event for event in events if event["type"] == "tool_result")
    assert result["outcome"]["status"] == "blocked"
    assert "尚未读取" in result["result"]
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "before\n"
