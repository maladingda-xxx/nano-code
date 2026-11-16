import asyncio
import io
import json
import sys
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_core import ModelReply
from nano_code import Session, sessions
from nano_code.cli import main
from nano_code.config import Config, write_json
from nano_code.mcp_client import MCPClient
from nano_code.providers import AnthropicModel
from nano_code.rpc import serve
from nano_code.ui import interactive


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
