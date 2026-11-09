import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

import agent
import session_store
import tools
from agent_core import ModelReply, run_turn
from changes import undo_last_edit
from exceptions import AgentAPIError, ToolInputError, ToolSecurityError, TurnPersistenceError
from execution_store import pending_executions, record_execution, recover_execution
from model_api import ChatModel
from workspace import find_root, read_project_instructions


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "_workspace_root", tmp_path)
    monkeypatch.setattr(session_store, "_workspace_root", tmp_path)
    return tmp_path


def tool_call(call_id, name, **arguments):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_find_edit_test_and_retry(workspace):
    (workspace / "values.py").write_text("def cap(value):\n    return min(value, 10)\n")
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_values.py").write_text(
        "import unittest\nfrom values import cap\n\n"
        "class ValueTest(unittest.TestCase):\n"
        "    def test_cap(self):\n"
        "        self.assertEqual(cap(20), 8)\n"
    )
    command = ["python", "-B", "-m", "unittest", "discover", "-s", "tests"]
    actions = [
        tool_call("search", "search_workspace", query="def cap"),
        tool_call("edit1", "edit_file", file_path="values.py",
                  old_text="min(value, 10)", new_text="min(value, 9)"),
        tool_call("test1", "execute_command", argv=command, verification=True),
        tool_call("edit2", "edit_file", file_path="values.py",
                  old_text="min(value, 9)", new_text="min(value, 8)"),
        tool_call("test2", "execute_command", argv=command, verification=True),
    ]
    expected = iter(["values.py:1", "+    return min(value, 9)", "退出码: 1",
                     "+    return min(value, 8)", "退出码: 0"])
    calls = iter(actions)

    def request(messages, allow_tools, emit):
        if messages[-1]["role"] == "tool":
            assert next(expected) in messages[-1]["content"]
        call = next(calls, None)
        if call is None:
            assert not allow_tools
        return ModelReply(tool_calls=[call]) if call else ModelReply("测试通过")

    events = []
    messages = [{"role": "system", "content": "Fix the test"}]
    status = run_turn(
        messages, "Fix the cap", request_model=request, emit=events.append,
        dispatch=lambda call: agent.execute_tool_call(
            call, confirm=lambda argv, cwd: argv == command and cwd == workspace,
        ),
        max_tool_rounds=5, save=session_store.auto_save,
    )
    assert status == "completed"
    assert messages[-1]["content"] == "测试通过"
    assert session_store.load_session("__auto__") == [
        {key: value for key, value in message.items() if value is not None}
        for message in messages
    ]
    results = {
        event["tool_call_id"]: event["outcome"]
        for event in events if event["type"] == "tool_result"
    }
    assert results["test1"]["verification"] == "failed"
    assert results["test1"]["exit_code"] == 1
    assert results["test2"]["verification"] == "passed"
    assert results["test2"]["exit_code"] == 0
    assert events[-1]["status"] == "completed"
    assert events[-1]["reason"] == "tool_round_limit"
    assert events[-1]["verification"] == {"status": "passed", "tool_call_id": "test2", "round": 5}


def test_edit_preserves_crlf_and_undo_protects_manual_changes(workspace):
    path = workspace / "windows.txt"
    original = b"before\r\ntarget\r\nafter\r\n"
    path.write_bytes(original)
    result = tools.edit_file("windows.txt", "before\ntarget", "before\nchanged")
    assert "+changed" in result
    edited = b"before\r\nchanged\r\nafter\r\n"
    assert path.read_bytes() == edited
    path.write_bytes(edited + b"manual\r\n")
    assert "已保留" in undo_last_edit(workspace)
    assert path.read_bytes().endswith(b"manual\r\n")
    path.write_bytes(edited)
    assert "已撤销" in undo_last_edit(workspace)
    assert path.read_bytes() == original


def test_rejects_ambiguous_and_missing_text_and_can_undo_creation(workspace):
    path = workspace / "sample.txt"
    tools.write_file("sample.txt", "aba")
    with pytest.raises(ToolInputError, match="多处"):
        tools.edit_file("sample.txt", "a", "b")
    with pytest.raises(ToolInputError, match="没有匹配"):
        tools.edit_file("sample.txt", "c", "b")
    assert path.read_text() == "aba"
    assert "已撤销" in undo_last_edit(workspace)
    assert not path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="requires Git")
def test_workspace_search_respects_git_ignores_and_loads_instructions(workspace):
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / ".gitignore").write_text("ignored.txt\n")
    (workspace / "ignored.txt").write_text("needle")
    (workspace / ".env").write_text("needle")
    (workspace / "AGENTS.md").write_text("root instructions")
    child = workspace / "sub"
    child.mkdir()
    (child / "AGENTS.md").write_text("subdirectory instructions")
    (child / "code.txt").write_text("needle")
    assert tools.search_workspace("needle") == "sub/code.txt:1: needle"
    assert tools.search_workspace("needle", "sub") == "sub/code.txt:1: needle"
    assert find_root(child) == workspace
    instructions = read_project_instructions(workspace, child)
    assert instructions.index("root instructions") < instructions.index("subdirectory instructions")
    with pytest.raises(ToolSecurityError):
        tools.read_file(".env")
    with pytest.raises(ToolSecurityError):
        tools.read_file("../outside.txt")


def test_denied_command_and_shell_operators_do_not_execute(workspace):
    argv = ["python", "-c", "from pathlib import Path; Path('created').touch()"]
    assert "未获授权" in tools.execute_command(argv=argv)
    assert not (workspace / "created").exists()
    with pytest.raises(ToolSecurityError):
        tools.execute_command(argv=argv + ["&&", "other"], confirm=lambda *_: True)
    with pytest.raises(ToolSecurityError):
        tools.execute_command(argv=argv, cwd="..", confirm=lambda *_: True)


def test_interrupt_pairs_every_pending_call_and_saves_history(workspace):
    calls = [tool_call("a", "read_file"), tool_call("b", "edit_file")]
    messages, events, dispatched = [], [], []

    def interrupt(call):
        dispatched.append(call["id"])
        raise KeyboardInterrupt

    status = run_turn(
        messages, "Do something", request_model=lambda *_: ModelReply(tool_calls=calls),
        dispatch=interrupt, emit=events.append, max_tool_rounds=2,
        save=session_store.auto_save,
    )
    assert status == "interrupted"
    assert dispatched == ["a"]
    assert [m["tool_call_id"] for m in session_store.load_session("__auto__")
            if m["role"] == "tool"] == ["a", "b"]
    assert events[-1]["type"] == "turn_end"
    assert events[-1]["status"] == "interrupted"
    assert events[-1]["reason"] == "user_interrupt"


def test_cli_json_with_an_openai_compatible_response(workspace, monkeypatch, capsys):
    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][-1] == {"role": "user", "content": "hello"}
        assert body["tools"]
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 1, "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "world"}}],
        })

    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("STREAMING_ENABLED", "false")
    monkeypatch.setattr(agent, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ))
    assert agent.main(["-p", "hello", "--json"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "reply" and event["text"] == "world" for event in events)
    assert events[-1]["type"] == "turn_end"
    assert events[-1]["status"] == "completed"
    assert events[-1]["verification"]["status"] == "not_run"
    assert len({event["session_id"] for event in events}) == 1
    assert len({event["turn_id"] for event in events}) == 1


@pytest.mark.parametrize("allowed", [False, True])
def test_json_reports_verification_independently_of_cli_exit(workspace, monkeypatch, capsys, allowed):
    argv = ["python", "-c", "import sys; print('x' * 3600); print('failure', file=sys.stderr); sys.exit(3)"]
    call = tool_call("verify", "execute_command", argv=argv, verification=True)
    replies = iter([ModelReply(tool_calls=[call]), ModelReply("已完成本轮")])
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("MAX_TOOL_ROUNDS", "2")
    monkeypatch.setattr(agent, "ChatModel", lambda *args, **kwargs: lambda *_: next(replies))
    args = ["-p", "run the verification", "--json"]
    if allowed:
        args.extend(["--allow-command", json.dumps(argv)])
    assert agent.main(args) == 0

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    request = next(event for event in events if event["type"] == "tool_request")
    result = next(event for event in events if event["type"] == "tool_result")
    for key in ("session_id", "turn_id", "round", "tool_call_id"):
        assert request[key] == result[key]
    outcome = result["outcome"]
    assert outcome["elapsed_ms"] >= 0
    if allowed:
        assert outcome["status"] == "failed"
        assert outcome["exit_code"] == 3
        assert outcome["stdout"].startswith("…（前文已截断）")
        assert outcome["stderr"].strip() == "failure"
        assert outcome["truncated"]
    else:
        assert outcome["status"] == "denied"
        assert outcome["exit_code"] is None
    assert events[-1]["verification"]["status"] == ("failed" if allowed else "not_run")


def test_edit_after_verification_requires_a_new_check(workspace):
    calls = [
        tool_call("verify", "execute_command", argv=["python", "-V"], verification=True),
        tool_call("write", "write_file", file_path="a.txt", content="after the check"),
    ]
    replies = iter([ModelReply(tool_calls=calls), ModelReply("修改后需重新验证")])
    events = []
    run_turn(
        [], "write after verifying", request_model=lambda *_: next(replies),
        dispatch=lambda call: agent.execute_tool_call(call, confirm=lambda *_: True),
        emit=events.append, max_tool_rounds=2,
    )
    assert events[-1]["verification"] == {"status": "stale", "tool_call_id": "verify", "round": 1}


def test_session_load_can_read_legacy_subdirectory_save(workspace, monkeypatch):
    directory = workspace / "sub"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.setattr(session_store, "_workspace_root", None)
    old = [{"role": "user", "content": "old prompt"}]
    assert session_store.save_session("saved", old)
    session_store.set_session_root(workspace)
    assert session_store.load_session("saved") == old
    assert [session["name"] for session in session_store.list_saved_sessions()] == ["saved"]
    new = [{"role": "user", "content": "new prompt"}]
    assert session_store.save_session("saved", new)
    assert session_store.load_session("saved") == new


def test_diff_shows_change_to_final_newline(workspace):
    (workspace / "sample.txt").write_bytes(b"text")
    diff = tools.edit_file("sample.txt", "text", "text\n")
    assert "-text\n\\ No newline at end of file\n+text" in diff


@pytest.mark.parametrize("finish_reason", ["tool_calls", None])
def test_streaming_text_with_tools_and_incomplete_calls(finish_reason):
    function = SimpleNamespace(name="read_file", arguments='{"file_path":"a.txt"}')
    delta = SimpleNamespace(
        content="Reading.",
        tool_calls=[SimpleNamespace(index=0, id="read", type="function", function=function)],
    )
    chunk = SimpleNamespace(
        usage=None, choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
    )

    class Stream:
        def __enter__(self):
            return iter([chunk])

        def __exit__(self, *_):
            pass

        def __iter__(self):
            return iter([chunk])

    model = ChatModel(None, "fake", 0, 1, True, 1, 0, [])
    if finish_reason is None:
        with pytest.raises(AgentAPIError, match="不完整"):
            model._stream_reply(Stream(), lambda event: None)
    else:
        reply = model._stream_reply(Stream(), lambda event: None)
        assert reply.text == "Reading."
        assert reply.tool_calls[0]["id"] == "read"


def _mock_model(handler, *, streaming=False):
    client = OpenAI(
        api_key="test", base_url="https://provider.test/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return ChatModel(client, "fake", 0, 1, streaming, 1, 0, tools.TOOLS)


def _completion(content, finish_reason="stop", calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return httpx.Response(200, json={
        "id": "reply", "object": "chat.completion", "created": 1, "model": "fake",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    })


@pytest.mark.parametrize(("ending", "reason"), [
    ("length", "response_truncated"),
    ("eof", "incomplete_response"),
    ("disconnect", "stream_interrupted"),
    ("incomplete_call", "invalid_tool_calls"),
])
def test_incomplete_stream_never_commits_reply_or_dispatches_tool(workspace, ending, reason):
    delta = {"content": "partial reply"}
    finish_reason = None
    if ending == "length":
        finish_reason = "length"
    elif ending == "incomplete_call":
        finish_reason = "tool_calls"
        delta["tool_calls"] = [{
            "index": 0, "type": "function",
            "function": {"name": "write_file", "arguments": '{"file_path":"x","content":"y"}'},
        }]
    chunk = {
        "id": "reply", "object": "chat.completion.chunk", "created": 1, "model": "fake",
        "choices": [{"index": 0, "finish_reason": finish_reason, "delta": delta}],
    }

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            if ending == "disconnect":
                raise httpx.ReadError("connection lost")

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    messages, events = [], []
    status = run_turn(
        messages, "hello", request_model=_mock_model(handler, streaming=True),
        dispatch=agent.execute_tool_call, emit=events.append, max_tool_rounds=2,
        save=session_store.auto_save,
    )
    assert status == "error"
    assert len(requests) == 1
    assert events[-1]["reason"] == reason
    assert any(event["type"] == "text_delta" for event in events)
    assert all(event["type"] not in {"reply", "tool_request"} for event in events)
    assert session_store.load_session("__auto__") == [{"role": "user", "content": "hello"}]
    assert not (workspace / "x").exists()


@pytest.mark.parametrize(("finish_reason", "status", "reason"), [
    ("stop", "completed", "model_fallback"),
    ("length", "error", "response_truncated"),
])
def test_request_failure_falls_back_and_checks_final_reply(finish_reason, status, reason):
    def handler(request):
        body = json.loads(request.content)
        if body.get("tools"):
            return httpx.Response(400, json={"error": {"message": "tools unsupported"}})
        assert body["stream"] is False
        return _completion("fallback", finish_reason)

    events = []
    assert run_turn(
        [], "hello", request_model=_mock_model(handler, streaming=True),
        dispatch=agent.execute_tool_call, emit=events.append, max_tool_rounds=2,
    ) == status
    assert events[-1]["reason"] == reason
    assert any(event["type"] == "reply" for event in events) == (status == "completed")


def test_invalid_tool_arguments_are_reported_to_model(workspace):
    bad_call = tool_call("write", "write_file", file_path="x", content="unexpected")
    bad_call["function"]["arguments"] = '{"file_path":"x","content":'

    def handler(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "user":
            return _completion(None, "tool_calls", [bad_call])
        assert messages[-1]["tool_call_id"] == "write"
        assert "工具错误" in messages[-1]["content"]
        return _completion("parameters rejected")

    events = []
    assert run_turn(
        [], "hello", request_model=_mock_model(handler), dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=2,
    ) == "completed"
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["outcome"]["status"] == "invalid_arguments"
    assert not (workspace / "x").exists()


@pytest.mark.parametrize(("crash_after", "expected"), [
    ("read", ["ok", "not_started", "not_started"]),
    ("write", ["ok", "unknown", "not_started"]),
    ("command", ["ok", "ok", "unknown"]),
])
def test_hard_exit_recovers_work_without_replaying_tools(
    workspace, monkeypatch, capsys, crash_after, expected,
):
    (workspace / "source.txt").write_text("original")
    script = """
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from agent import execute_tool_call
from agent_core import ModelReply, run_turn
from execution_store import record_execution
from tools import set_workspace_root

set_workspace_root(Path.cwd())
crash_after = sys.argv[2]

def call(call_id, name, **arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}

calls = [
    call("read", "read_file", file_path="source.txt"),
    call("write", "write_file", file_path="source.txt", content="+write", mode="append"),
    call("command", "execute_command", argv=[
        "python", "-c", "from pathlib import Path; p=Path('source.txt'); p.write_text(p.read_text()+'+command')",
    ]),
]

def dispatch(tool):
    result = execute_tool_call(tool, confirm=lambda *_: True)
    assert result.status == "ok"
    if tool["id"] == crash_after and crash_after != "read":
        os._exit(73)
    return result

def emit(event):
    if crash_after == "read" and event["type"] == "tool_result":
        os._exit(73)

run_turn([], "work", request_model=lambda *_: ModelReply(tool_calls=calls),
         dispatch=dispatch, emit=emit, max_tool_rounds=1, checkpoint=record_execution)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(Path(agent.__file__).parent), crash_after],
        cwd=workspace, capture_output=True, timeout=20,
    )
    assert completed.returncode == 73, completed.stderr
    pending, = pending_executions()
    assert not pending["active"]
    assert [step["status"] for step in pending["tools"]] == expected

    monkeypatch.setenv("API_KEY", "test")
    answers = iter([f"/recover {pending['turn_id']}", "/exit"])
    monkeypatch.setattr(agent.Prompt, "ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(agent.ChatModel, "__call__", lambda *args: pytest.fail("恢复时调用了模型"))
    assert agent.main([]) == 0

    output = capsys.readouterr().out
    assert "已恢复对话" in output
    if "unknown" in expected:
        assert "结果待核查" in output
    if "not_started" in expected:
        assert "未开始" in output
    history = session_store.load_session("__auto__")
    results = [message for message in history if message["role"] == "tool"]
    assert [result["tool_call_id"] for result in results] == ["read", "write", "command"]
    for result, state in zip(results, expected):
        if state == "unknown":
            assert "先核查" in result["content"]
        elif state == "not_started":
            assert "未开始" in result["content"]
    suffix = {"read": "", "write": "+write", "command": "+write+command"}
    assert (workspace / "source.txt").read_text() == "original" + suffix[crash_after]
    assert pending_executions() == []


def test_persistence_failure_stops_before_tool_dispatch(workspace):
    calls = [tool_call("write", "write_file", file_path="x", content="unexpected")]

    def checkpoint(event, messages):
        if event["type"] == "tool_request":
            raise TurnPersistenceError("disk unavailable")
        record_execution(event, messages)

    events = []
    assert run_turn(
        [], "hello", request_model=lambda *_: ModelReply(tool_calls=calls),
        dispatch=agent.execute_tool_call, emit=events.append, max_tool_rounds=1,
        checkpoint=checkpoint,
    ) == "error"
    assert events[-1]["reason"] == "persistence_error"
    assert not (workspace / "x").exists()
    assert len(pending_executions()) == 1


def test_failed_autosave_preserves_final_reply_in_execution_record(workspace):
    events = []
    assert run_turn(
        [], "hello", request_model=lambda *_: ModelReply("final reply"),
        dispatch=agent.execute_tool_call, emit=events.append, max_tool_rounds=1,
        save=lambda _: False, checkpoint=record_execution,
    ) == "error"
    assert events[-1]["reason"] == "persistence_error"
    pending, = pending_executions()
    assert recover_execution(pending["turn_id"]) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "final reply"},
    ]
    assert pending_executions() == []


def test_recovery_waits_for_owner_process_to_exit(workspace):
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from execution_store import record_execution

record_execution(
    {"type": "turn_start", "session_id": "session", "turn_id": "live"},
    [{"role": "user", "content": "work"}],
)
print("ready", flush=True)
input()
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(Path(agent.__file__).parent)],
        cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as process:
        try:
            assert process.stdout.readline().strip() == "ready"
            pending, = pending_executions()
            assert pending["active"]
            with pytest.raises(TurnPersistenceError, match="可能仍在运行"):
                recover_execution("live")
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    assert recover_execution("live") == [{"role": "user", "content": "work"}]
