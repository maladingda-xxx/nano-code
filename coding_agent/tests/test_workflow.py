import json
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

import agent
import session_store
import tools
from agent_core import ModelReply, run_turn
from changes import undo_last_edit
from exceptions import AgentAPIError, ToolInputError, ToolSecurityError
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
        content="Reading.", tool_calls=[SimpleNamespace(index=0, id="read", function=function)],
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
