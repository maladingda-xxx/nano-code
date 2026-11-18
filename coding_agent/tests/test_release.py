import asyncio
import io
import json
import sqlite3
import sys
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from agent_core import ModelReply
from context_budget import context_breakdown, fit_request
from nano_code import Session, sessions
from nano_code.cli import main
from nano_code.commands import execute_command
from nano_code.config import Config, write_json
from nano_code.mcp_client import MCPClient
from nano_code.providers import AnthropicModel
from nano_code.rpc import serve
from nano_code.ui import InputCompleter, interactive


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("API_KEY", "test-placeholder")
    return root


def call(name, **arguments):
    return {"id": uuid4().hex, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def test_edit_verify_and_resume(project):
    command = ["python", "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'hello'"]
    replies = iter([
        ModelReply(tool_calls=[call("write_file", file_path="result.txt", content="hello")]),
        ModelReply(tool_calls=[call("execute_command", argv=command, verification=True)]),
        ModelReply(text="Done."),
    ])
    with Session(project, model=lambda *_: next(replies), ask=lambda *_: "yes") as session:
        result = session.run("Create and verify a file")
        uid = session.uid
        assert result["verification"]["status"] == "passed"
        assert result["changes"]["files"] == ["result.txt"]
    with Session(project, resume=uid) as restored:
        assert restored.messages[-1]["content"] == "Done."
        assert restored.uid == uid
        with pytest.raises(ValueError, match="正在"):
            with sessions.lease(uid), sessions.lease(uid):
                pass
    with Session(project, resume=uid, fork_session=True) as forked:
        assert forked.uid != uid
        assert forked.messages[-1]["content"] == "Done."


def test_untrusted_project_cannot_grant_permissions(project):
    local = project / ".nano/settings.json"
    data = {"base_url": "https://other.example", "permission_mode": "acceptEdits",
            "permissions": {"allow": ["execute_command"]}, "hooks": {"Stop": []}}
    write_json(local, data)
    config = Config(project)
    with Session(project, config=config) as session:
        assert session.config.data["base_url"] != "https://other.example"
        assert not session.permissions.authorize("execute_command", {"argv": ["python"]})
        assert not session.permissions.authorize("write_file", {"file_path": "a"})
        assert not session.hooks.settings
    config.trust()
    assert Config(project).trusted
    write_json(local, {**data, "model": "new"})
    assert not Config(project).trusted


def test_plan_and_scoped_instructions(project):
    nested = project / "nested"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Apply the nested convention.", encoding="utf-8")
    target = nested / "result.txt"
    with Session(project) as session:
        session.permissions.mode = "plan"
        edit = call("write_file", file_path="nested/result.txt", content="ok")
        assert session.dispatch(edit, lambda *_: None).status == "denied"
        session.permissions.mode = "acceptEdits"
        result = session.dispatch(edit, lambda *_: None)
        assert "Apply the nested convention." in result.text and not target.exists()
        assert session.dispatch(edit, lambda *_: None).status == "ok"
        assert target.read_text(encoding="utf-8") == "ok"


def test_auto_rules_and_plan_toggle(project):
    argv = ["python", "-c", "print('verified')"]
    local = project / ".nano/settings.json"
    write_json(local, {"permissions": {"autoAllow": [{"tool": "execute_command", "cwd": ".", "argv": argv}]}})
    with Session(project, config=Config(project, overrides={"permission_mode": "auto"})) as untrusted:
        assert untrusted.dispatch(call("execute_command", argv=argv), lambda _: None).status == "denied"
    Config(project).trust()
    approvals = []
    console = Console(file=io.StringIO())
    with Session(project, ask=lambda name, args: approvals.append((name, args)) or False) as session:
        command = call("execute_command", argv=argv, verification=True)
        assert session.dispatch(command, lambda _: None).status == "denied"
        execute_command(session, "/auto", None, console=console)
        assert session.dispatch(command, lambda _: None).exit_code == 0
        assert len(approvals) == 1
        different = call("execute_command", argv=[*argv[:-1], "print('different')"])
        assert session.dispatch(different, lambda _: None).status == "denied"
        (project / "subdir").mkdir()
        assert session.dispatch(call("execute_command", argv=argv, cwd="subdir"), lambda _: None).status == "denied"
        assert len(approvals) == 3

        execute_command(session, "/plan", None, console=console)
        assert session.dispatch(command, lambda _: None).status == "denied"
        edit = call("write_file", file_path="result.txt", content="ok")
        assert session.dispatch(edit, lambda _: None).status == "denied"
        for line in ("/init", "/undo", "/restore abcd", "/memory example", "/export report.md"):
            with pytest.raises(ValueError, match="计划模式"):
                execute_command(session, line, None, console=console)
        with pytest.raises(ValueError, match="运行中"):
            execute_command(session, "/permissions auto", None, console=console, running=True)
        execute_command(session, "/plan", None, console=console)
        assert session.permissions.mode == "auto"
        session.permissions.rules["deny"] = [{"tool": "execute_command", "argv": argv}]
        assert session.dispatch(command, lambda _: None).status == "denied"
        assert len(approvals) == 3
        assert session.dispatch(edit, lambda _: None).status == "ok"
        assert (project / "result.txt").read_text(encoding="utf-8") == "ok"


def test_model_commands_and_context_keep_request_state(project, monkeypatch, capsys):
    monkeypatch.setenv("REASONING_EFFORT", "low")
    config = Config(project, overrides={"provider": "openai", "base_url": "https://api.deepseek.com",
                    "model": "deepseek-flash", "models": ["deepseek-v4-pro"], "context_limit": 12000})
    output = io.StringIO()
    console = Console(file=output, width=100)
    with Session(project, config=config) as session:
        model = session.ensure_model()
        model.requests_sent = 2
        execute_command(session, "/model", None, console=console)
        execute_command(session, "/effort high", None, console=console)
        execute_command(session, "/model deepseek-flash", None, console=console)
        assert model.effort == "high"
        completer = InputCompleter(session)
        assert [c.text for c in completer.get_completions(Document("/model deepseek-v"), None)] == ["deepseek-v4-pro"]
        assert [c.text for c in completer.get_completions(Document("/effort m"), None)] == ["max"]
        execute_command(session, "/model custom-model", None, console=console)
        assert session.ensure_model() is model and model.requests_sent == 2
        assert model.name == "custom-model" and model.effort is None
        with pytest.raises(ValueError, match="/effort"):
            execute_command(session, "/effort high", None, console=console)
        execute_command(session, "/context", None, console=console)
        budget = context_breakdown(session.messages, session.tool_schemas, limit=12000)
        _, output_reserve = fit_request(session.messages, session.tool_schemas, limit=12000, max_output=4096,
                                        emit=lambda _: None)
        view = output.getvalue()
        assert "deepseek-v4-pro" in view
        assert "工具与协议" in view and f"{budget.overhead:,}" in view
        assert "回复预留" in view and f"{output_reserve:,}" in view
        assert "剩余输入空间" in view and f"{budget.remaining:,}" in view
    assert main(["--cwd", str(project), "--base-url", "https://api.deepseek.com", "--model", "deepseek-flash",
                 "--effort", "max", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "max"


@pytest.mark.parametrize("outcome", ["saved", "non_reducing", "cancelled", "save_failed"])
def test_compaction_preserves_original_history(project, monkeypatch, outcome):
    monkeypatch.chdir(project)
    long_text = "Detailed context to archive. " * 300

    class Summarizer:
        def summarize(self, messages, emit):
            assert messages[-1]["content"].endswith("keep pending tests")
            if outcome == "cancelled":
                session.cancel()
            return long_text * 4 if outcome == "non_reducing" else "Pending tests are recorded."

    with Session(project, model=Summarizer()) as session:
        session.messages += [{"role": "user", "content": long_text}, {"role": "assistant", "content": long_text}]
        assert session.persist()
        original = sessions.load(session.uid)["messages"]
        if outcome == "save_failed":
            write = sessions.legacy._write_session

            def fail_active(conn, name, messages, uid):
                if uid == session.uid:
                    raise sqlite3.OperationalError("simulated save failure")
                write(conn, name, messages, uid=uid)

            monkeypatch.setattr(sessions.legacy, "_write_session", fail_active)
        if outcome in {"cancelled", "save_failed"}:
            with pytest.raises(KeyboardInterrupt if outcome == "cancelled" else sqlite3.OperationalError):
                session.compact("keep pending tests")
        else:
            console = Console(file=io.StringIO())
            execute_command(session, "/compact keep pending tests", None, console=console)
            if outcome == "saved":
                assert "→" in console.file.getvalue()
            else:
                assert "未缩小" in console.file.getvalue()
        assert not session.busy

        archives = [entry for entry in sessions.list_sessions() if entry["uid"] != session.uid]
        if outcome == "saved":
            assert len(archives) == 1 and sessions.load(archives[0]["uid"])["messages"] == original
            assert "Pending tests" in sessions.load(session.uid)["messages"][1]["content"]
        else:
            assert not archives and sessions.load(session.uid)["messages"] == original
            assert session.messages[1]["content"] == long_text


def test_stale_resume_cannot_overwrite_newer_history(project):
    model = lambda *_: ModelReply(text="Done.")
    with Session(project, model=model) as first:
        first.run("First turn")
        with Session(project, resume=first.uid, model=model) as stale:
            first.run("Second turn")
            with pytest.raises(ValueError, match="重新加载"):
                stale.run("Would overwrite the second turn")
            stale.resume(first.uid)
            assert stale.run("Third turn")["status"] == "completed"
            assert any(m.get("content") == "Second turn" for m in stale.messages)


def test_sdk_cancel_stops_command_and_saves_history(project):
    running = Event()
    command = call("execute_command", argv=["python", "-u", "-c", "import time; print('started'); time.sleep(20)"])
    with Session(project, model=lambda *_: ModelReply(tool_calls=[command]), ask=lambda *_: "yes",
                 emit=lambda event: running.set() if event["type"] == "tool_output" else None) as session:
        worker = Thread(target=session.run, args=("Run a bounded test process",), daemon=True)
        worker.start()
        assert running.wait(8)
        session.cancel()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert session.last_result["status"] == "interrupted"
        assert sessions.load(session.uid)["messages"][-1]["role"] == "tool"


def test_anthropic_tool_round_trip(project):
    config = Config(project, overrides={"provider": "anthropic", "base_url": "https://example.test", "streaming": False})
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/messages"
        if len(requests) == 1:
            stop = "tool_use"
            content = [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"file_path": "example.txt"}}]
        else:
            assert body["messages"][-1]["content"][0]["tool_use_id"] == "t1"
            assert "fixture" in body["messages"][-1]["content"][0]["content"]
            stop = "end_turn"
            content = [{"type": "text", "text": "I read the file."}]
        return httpx.Response(200, json={"stop_reason": stop, "content": content, "usage": {"input_tokens": 8, "output_tokens": 4}})

    (project / "example.txt").write_text("fixture", encoding="utf-8")
    with Session(project, config=config) as session:
        model = AnthropicModel(config, session.tool_schemas)
        model.client.close()
        model.client = httpx.Client(transport=httpx.MockTransport(reply))
        session.model = model
        assert session.run("Read the file.")["status"] == "completed"
        assert session.usage["total_tokens"] == 24


def test_mcp_stdio_discovers_and_calls_tools(project):
    server = project / "server.py"
    server.write_text('''
import json
import sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    elif msg["method"] == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    else:
        result = {"content": [{"type": "text", "text": msg["params"]["arguments"]["value"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
''', encoding="utf-8")
    client = MCPClient("fixture", {"command": sys.executable, "args": [str(server)], "timeout": 3}, project)
    try:
        assert client.connect()[0]["name"] == "echo"
        assert client.call("echo", {"value": "hello"})["content"][0]["text"] == "hello"
    finally:
        client.close()


def test_rpc_status(project):
    requests = [{"jsonrpc": "2.0", "id": i, "method": name}
                for i, name in enumerate(("initialize", "status", "unknown", "shutdown"))]
    incoming = io.StringIO("\n".join(map(json.dumps, requests)))
    responses = io.StringIO()
    with Session(project) as session:
        assert serve(session, incoming, responses) == 0
        messages = [json.loads(line) for line in responses.getvalue().splitlines()]
        assert messages[0]["result"]["protocol_version"] == 1
        assert not messages[1]["result"]["busy"]
        assert messages[2]["error"]["code"] == -32601


def test_print_entry_point_reports_json_and_persists(project, monkeypatch, capsys):
    monkeypatch.setattr("nano_code.runtime.create_model", lambda *_: lambda *_: ModelReply(text="Visible result."))
    args = ["--cwd", str(project), "-p", "Show output", "--output-format", "json"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "Visible result."
    assert result["protocol_version"] == 1
    with Session(project, resume=result["session_id"]) as session:
        assert session.messages[-1]["content"] == "Visible result."


def test_terminal_accepts_prompt_approval_and_exit(project):
    replies = iter([ModelReply(tool_calls=[call("write_file", file_path="result.txt", content="ok")]),
                    ModelReply(text="Done.")])

    async def exercise():
        with create_pipe_input() as pipe, Session(project, model=lambda *_: next(replies)) as session:
            terminal = asyncio.create_task(interactive(session, terminal_input=pipe, terminal_output=DummyOutput()))
            await asyncio.sleep(0.1)
            pipe.send_text("Write the file\r")
            for _ in range(100):
                if session.busy and session.transcript and session.transcript[-1]["type"] == "tool_request":
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.15)
            pipe.send_text("y\r")
            for _ in range(100):
                if session.last_result:
                    break
                await asyncio.sleep(0.02)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(terminal, timeout=3)
            assert session.last_result and session.last_result["status"] == "completed"
            assert (project / "result.txt").read_text(encoding="utf-8") == "ok"

    asyncio.run(exercise())


def test_terminal_cycles_permission_modes(project):
    async def exercise():
        with create_pipe_input() as pipe, Session(project) as session:
            terminal = asyncio.create_task(interactive(session, terminal_input=pipe, terminal_output=DummyOutput()))
            try:
                for mode in ("acceptEdits", "plan", "auto", "default"):
                    pipe.send_text("\x1b[Z")
                    async with asyncio.timeout(3):
                        while session.permissions.mode != mode:
                            await asyncio.sleep(0.02)
            finally:
                pipe.send_text("/exit\r")
                assert await asyncio.wait_for(terminal, timeout=3) == 0

    asyncio.run(exercise())
