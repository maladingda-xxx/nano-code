import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

import agent
import context_budget
import model_api
import session_store
import tools
from agent_core import ModelReply, run_turn
from changes import undo_last_edit
from exceptions import (
    AgentAPIError, ModelResponseError, ToolInputError, ToolSecurityError, TurnPersistenceError,
)
from execution_store import _process_running, pending_executions, record_execution, recover_execution
from model_api import ChatModel
from workspace import find_root, read_project_instructions


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "_workspace_root", tmp_path)
    monkeypatch.setattr(session_store, "_workspace_root", tmp_path)
    monkeypatch.setattr(agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("MAX_MODEL_REQUESTS", raising=False)
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
        last = messages[-1]
        if last["role"] == "user" and last.get("content", "").startswith("仅剩"):
            last = next(message for message in reversed(messages[:-1]) if message["role"] == "tool")
        if last["role"] == "tool":
            assert next(expected) in last["content"]
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
    assert events[-1]["changes"] == {"files": ["values.py"], "edits": 2}
    assert events[-1]["tool_calls"] == 5


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
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 1, "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "world"}}],
        })

    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("MODEL_NAME", raising=False)
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
    output_events = [event for event in events if event["type"] == "tool_output"]
    if allowed:
        assert {event["stream"] for event in output_events} == {"stdout", "stderr"}
        for event in output_events:
            assert events.index(request) < events.index(event) < events.index(result)
            for key in ("session_id", "turn_id", "round", "tool_call_id"):
                assert event[key] == result[key]
        assert outcome["status"] == "failed"
        assert outcome["exit_code"] == 3
        assert outcome["stdout"].startswith("…（前文已截断）")
        assert outcome["stderr"].strip() == "failure"
        assert outcome["truncated"]
    else:
        assert not output_events
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
        last = messages[-1]
        if last["role"] == "user" and last.get("content", "").startswith("仅剩"):
            last = next(message for message in reversed(messages[:-1]) if message["role"] == "tool")
        if last["role"] == "user":
            return _completion(None, "tool_calls", [bad_call])
        assert last["tool_call_id"] == "write"
        assert "工具错误" in last["content"]
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


def test_command_streams_before_exit_in_target_directory(workspace, monkeypatch):
    project = workspace / "project"
    project.mkdir()
    monkeypatch.setenv("NANO_TEST_COMMAND_ENV", "available")
    script = """
import os
import sys
import time
from pathlib import Path

prefix = "开始".encode("utf-8")
os.write(1, prefix[:1])
time.sleep(0.1)
os.write(1, prefix[1:])
print(f"ready:{Path.cwd().name}:{os.environ['NANO_TEST_COMMAND_ENV']}", flush=True)
while not Path("ack").exists():
    time.sleep(0.01)
print("done", file=sys.stderr, flush=True)
"""
    output = {"stdout": "", "stderr": ""}

    def on_output(stream, text):
        output[stream] += text
        if "ready:project:available" in output["stdout"]:
            (project / "ack").touch()

    result = tools.run_command(
        argv=["python", "-c", script], cwd="project", timeout_seconds=5,
        confirm=lambda argv, directory: directory == project, on_output=on_output,
    )
    assert result.status == "ok"
    assert output["stdout"].startswith("开始ready:project:available")
    assert output["stderr"].strip() == "done"
    assert result.stdout == output["stdout"]
    assert result.stderr == output["stderr"]


@pytest.mark.parametrize("stop", ["timeout", "interrupted"])
def test_command_stops_child_process_tree(workspace, stop):
    script = """
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
print(child.pid, flush=True)
time.sleep(15)
"""
    call = tool_call(
        "command", "execute_command", argv=["python", "-c", script],
        timeout_seconds=2, verification=True,
    )

    def emit(event):
        if stop == "interrupted" and event["stream"] == "stdout":
            raise KeyboardInterrupt

    result = agent.execute_tool_call(call, emit, confirm=lambda *_: True, command_timeout=3)
    assert result.status == stop, result.text
    assert result.exit_code is None
    assert result.verification == "inconclusive"
    pid = int(result.stdout.strip())
    deadline = time.monotonic() + 3
    while _process_running(pid):
        if sys.platform == "linux":
            try:
                if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z "):
                    break
            except FileNotFoundError:
                break
        assert time.monotonic() < deadline, f"child process {pid} survived {stop}"
        time.sleep(0.02)


def test_command_timeout_cannot_exceed_user_limit(workspace):
    argv = ["python", "-c", "from pathlib import Path; Path('created').touch()"]
    with pytest.raises(ToolInputError, match="不超过 2 秒"):
        tools.run_command(argv=argv, timeout_seconds=3, timeout_limit=2, confirm=lambda *_: True)
    assert not (workspace / "created").exists()


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_checks_budget_after_each_tool_batch(workspace, monkeypatch, capsys, interactive):
    monkeypatch.setattr(context_budget, "_encoding", lambda: None)
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("STREAMING_ENABLED", "false")
    monkeypatch.setenv("CONTEXT_LIMIT_TOKENS", "6500")
    monkeypatch.setenv("MAX_TOOL_ROUNDS", "4")
    prompt = "check the current task"
    long_output = "退出码: 1\n" + "x" * 7700 + "\nLAST-ERROR"
    monkeypatch.setitem(
        tools.AVAILABLE_TOOLS, "read_file",
        lambda file_path: long_output if file_path != "small" else "new evidence",
    )
    first_calls = [
        tool_call("a", "read_file", file_path="large"),
        tool_call("b", "read_file", file_path="large"),
    ]
    latest_call = tool_call("c", "read_file", file_path="small")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        visible = [
            message for message in messages
            if not (message["role"] == "user" and message.get("content", "").startswith("仅剩"))
        ]
        assert messages[0] == requests[0]["messages"][0]
        assert {"role": "user", "content": prompt} in messages
        assert all(message.get("tool_call_id") != "old" for message in messages)
        assert context_budget.estimate_tokens(messages) + body["max_tokens"] < 6500
        if len(requests) == 1:
            assert body["max_tokens"] == 1625
            return _completion(None, "tool_calls", first_calls)
        if len(requests) == 2:
            assert visible[-3]["tool_calls"] == first_calls
            assert [message["tool_call_id"] for message in visible[-2:]] == ["a", "b"]
            for message in visible[-2:]:
                assert "工具输出已压缩" in message["content"]
                assert message["content"].startswith("退出码: 1")
                assert message["content"].endswith("LAST-ERROR")
            return _completion(None, "tool_calls", [latest_call])
        assert visible[-2]["tool_calls"] == [latest_call]
        assert visible[-1]["tool_call_id"] == "c"
        assert not any(message.get("tool_call_id") in {"a", "b"} for message in messages)
        return _completion("done")

    monkeypatch.setattr(agent, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ))
    if interactive:
        assert session_store.auto_save([
            {"role": "system", "content": "previous system"},
            {"role": "user", "content": "previous task"},
            {"role": "assistant", "tool_calls": [tool_call("old", "read_file", file_path="old")]},
            {"role": "tool", "tool_call_id": "old", "content": long_output + long_output},
        ])
        answers = iter(["y", prompt, "/exit"])
        monkeypatch.setattr(agent.Prompt, "ask", lambda *args, **kwargs: next(answers))
        assert agent.main([]) == 0
        history = session_store.load_session("__auto__")
        results = {
            message["tool_call_id"]: message["content"]
            for message in history if message["role"] == "tool"
        }
        assert results["old"] == long_output + long_output
        assert results["a"] == results["b"] == long_output
    else:
        assert agent.main(["-p", prompt, "--json"]) == 0
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        notices = [event for event in events if event.get("reason") == "context_compacted"]
        assert len(notices) == 2
        assert all(event["estimated_after"] <= event["message_budget"] for event in notices)
        assert next(event for event in events if event.get("tool_call_id") == "a"
                    and event["type"] == "tool_result")["result"] == long_output
    assert len(requests) == 3


@pytest.mark.parametrize("include_tools", [False, True])
def test_budget_exhaustion_stops_before_provider_call(monkeypatch, include_tools):
    monkeypatch.setattr(context_budget, "_encoding", lambda: None)

    def unexpected_request(request):
        pytest.fail("超出预算时发起了请求")

    client = OpenAI(
        api_key="test", base_url="https://provider.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(unexpected_request)),
    )
    tool_schema = tools.TOOLS if include_tools else []
    model = ChatModel(client, "fake", 0, 1, False, 1, 0, tool_schema, context_limit=400)
    prompt = "short" if include_tools else "x" * 1000
    events = []
    assert run_turn(
        [], prompt, request_model=model, dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=1,
    ) == "error"
    assert events[-1]["reason"] == "context_budget_exceeded"
    assert all(event["type"] not in {"tool_request", "reply"} for event in events)


def test_model_request_limit_counts_retries_and_blocks_fallback(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            503 if len(requests) == 1 else 400, json={"error": {"message": "test"}},
        )

    monkeypatch.setattr(model_api.time, "sleep", lambda delay: None)
    client = OpenAI(
        api_key="test", base_url="https://provider.test/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    model = ChatModel(client, "fake", 0, 1, False, 3, 0, tools.TOOLS, request_limit=2)
    events = []
    assert run_turn(
        [], "hello", request_model=model, dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=2,
    ) == "error"
    assert len(requests) == 2
    assert all(body.get("tools") and "thinking" not in body for body in requests)
    assert [event["number"] for event in events if event["type"] == "model_request"] == [1, 2]
    assert events[-1]["reason"] == "model_request_budget_exceeded"


def test_final_round_tells_model_to_stop_calling_tools(workspace):
    (workspace / "note.txt").write_text("hello")
    calls = iter([tool_call("read", "read_file", file_path="note.txt")])
    seen = []

    def request(messages, allow_tools, emit):
        seen.append((allow_tools, messages[-1]))
        call = next(calls, None)
        if call:
            assert allow_tools
            return ModelReply(tool_calls=[call])
        assert not allow_tools
        return ModelReply("done")

    messages = [{"role": "system", "content": "system"}]
    assert run_turn(
        messages, "look", request_model=request, dispatch=agent.execute_tool_call,
        emit=lambda event: None, max_tool_rounds=1,
    ) == "completed"
    assert [allow_tools for allow_tools, _ in seen] == [True, False]
    reminder = seen[-1][1]
    assert reminder["role"] == "user"
    assert "工具调用轮数已用完" in reminder["content"]
    assert messages[-1]["content"] == "done"
    assert all("工具调用轮数已用完" not in (message.get("content") or "") for message in messages)


def test_tool_markup_without_tools_is_rejected():
    with pytest.raises(ModelResponseError) as error:
        model_api._checked_reply(
            "<｜｜DSML｜｜ calls>", [], "stop", allow_tools=False,
        )
    assert error.value.reason == "tool_markup_without_tools"


def test_runtime_notice_keeps_current_tool_batch_when_compacting(monkeypatch):
    monkeypatch.setattr(context_budget, "_encoding", lambda: None)
    tool_calls = [{
        "id": "1", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "OLD" * 1000},
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "1", "content": "TOOL RESULT"},
        {"role": "user", "content": "REMINDER", "_runtime_notice": True},
    ]
    limit = context_budget.estimate_tokens(messages[3:]) + 100
    events = []
    fitted, _ = context_budget.fit_request(
        messages, [], limit=limit, max_output=50, emit=events.append,
    )
    assert any(message.get("content") == "TOOL RESULT" for message in fitted)
    assert any(message.get("content") == "REMINDER" for message in fitted)
    assert all("_runtime_notice" not in message for message in fitted)
    assert not any(message.get("content") == "OLD" * 1000 for message in fitted)
    assert events[-1]["reason"] == "context_compacted"


def test_low_budget_notice_asks_model_to_commit_changes(workspace):
    (workspace / "note.txt").write_text("hello")
    calls = iter([tool_call("read", "read_file", file_path="note.txt")])
    seen = []

    def request(messages, allow_tools, emit):
        seen.append((allow_tools, messages[-1]))
        call = next(calls, None)
        if call:
            assert allow_tools
            return ModelReply(tool_calls=[call])
        assert allow_tools
        return ModelReply("done")

    messages = [{"role": "system", "content": "system"}]
    events = []
    assert run_turn(
        messages, "look", request_model=request, dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=3,
    ) == "completed"
    assert [allow_tools for allow_tools, _ in seen] == [True, True]
    reminder = seen[-1][1]
    assert reminder["role"] == "user"
    assert "仅剩 2 个工具轮" in reminder["content"]
    assert all("仅剩" not in (message.get("content") or "") for message in messages)
    assert events[-1]["changes"] == {"files": [], "edits": 0}
    assert events[-1]["tool_calls"] == 1


def test_turn_end_tracks_successful_write(workspace):
    calls = iter([tool_call("write", "write_file", file_path="new.txt", content="data")])

    def request(messages, allow_tools, emit):
        call = next(calls, None)
        return ModelReply(tool_calls=[call]) if call else ModelReply("done")

    events = []
    assert run_turn(
        [{"role": "system", "content": "system"}], "write",
        request_model=request, dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=1,
    ) == "completed"
    assert events[-1]["changes"] == {"files": ["new.txt"], "edits": 1}


def test_noop_edit_is_not_reported_as_change(workspace):
    (workspace / "same.txt").write_text("same")
    calls = iter([
        tool_call("edit", "edit_file", file_path="same.txt", old_text="same", new_text="same"),
    ])

    def request(messages, allow_tools, emit):
        call = next(calls, None)
        return ModelReply(tool_calls=[call]) if call else ModelReply("done")

    events = []
    assert run_turn(
        [{"role": "system", "content": "system"}], "edit",
        request_model=request, dispatch=agent.execute_tool_call,
        emit=events.append, max_tool_rounds=1,
    ) == "completed"
    assert events[-1]["changes"] == {"files": [], "edits": 0}
