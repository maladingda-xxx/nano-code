import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_core import ModelReply
from nano_code.config import Config, write_json
from nano_code.runtime import Session


SERVER = r"""
import json
import sys
import time
from pathlib import Path

def send(**message):
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

def report(document, *items):
    send(method="textDocument/publishDiagnostics", params={
        "uri": document["uri"], "version": document["version"], "diagnostics": items,
    })

previous = None
error = {"range": {"start": {"line": 1, "character": 4}}, "severity": 1,
         "code": "reportUndefinedVariable", "message": "missing_name is undefined"}
while header := sys.stdin.buffer.readline():
    size = int(header.split(b":", 1)[1])
    assert sys.stdin.buffer.readline() == b"\r\n"
    message = json.loads(sys.stdin.buffer.read(size))
    method = message["method"]
    if method == "initialize":
        send(id=message["id"], result={"capabilities": {"textDocumentSync": 2}})
    elif method == "textDocument/didOpen":
        previous = message["params"]["textDocument"]
        if sys.argv[1] == "slow":
            Path("server-ready").touch()
            time.sleep(20)
        report(previous, error, error)
        report(previous, error)
    elif method == "textDocument/didChange":
        params = message["params"]
        assert params["textDocument"]["version"] > previous["version"]
        change, = params["contentChanges"]
        assert change["range"]["end"] == {"line": 2, "character": 4}
        assert "missing_name" not in change["text"]
        report(previous, error)
        report(params["textDocument"])
        report(previous, error)
        report(params["textDocument"])
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "settings"))
    server = tmp_path / "fake_server.py"
    server.write_text(SERVER, encoding="utf-8")
    return tmp_path, server


def _config(project, mode="normal", timeout=3):
    root, server = project
    return Config(root, overrides={"permission_mode": "acceptEdits", "python_lsp": {
        "argv": [sys.executable, str(server), mode], "timeout": timeout,
    }})


def _call(call_id, tool, **arguments):
    return ModelReply(tool_calls=[{"id": call_id, "type": "function",
                                  "function": {"name": tool, "arguments": json.dumps(arguments)}}])


def _write():
    return _call("write", "write_file", file_path="example.py", content='x = 1\r\n    missing_name\r\n# 😀')


def test_edit_diagnostics_replace_old_versions_and_do_not_set_verification(project):
    root, _ = project
    events, requests = [], []
    command = ["python", "-c", "raise SystemExit(3)"]

    def model(messages, allow_tools, emit):
        requests.append("\n".join(m["content"] for m in messages if m.get("_runtime_notice")))
        if len(requests) == 1:
            return _write()
        if len(requests) == 2:
            return _call("read", "read_file", file_path="example.py")
        if len(requests) == 3:
            return _call("fix", "edit_file", file_path="example.py", old_text="missing_name", new_text="print(x)")
        if len(requests) == 4:
            return _call("test", "execute_command", argv=command, verification=True)
        return ModelReply("Done.")

    with Session(root, config=_config(project), model=model, emit=events.append, ask=lambda *_: True) as session:
        result = session.run("Update example.py and run the test command.")
        server = session._python_diagnostics._process
        assert server.poll() is None
        assert all(not m.get("_runtime_notice") for m in session.messages)
    assert server.poll() is not None
    changes = [event for event in events if event["type"] == "python_diagnostics"]
    assert len(changes) == 2
    assert changes[0]["path"] == "example.py" and changes[0]["version"] < changes[1]["version"]
    assert changes[0]["diagnostics"] == [{"line": 2, "column": 5, "severity": "error",
                                         "rule": "reportUndefinedVariable", "message": "missing_name is undefined"}]
    assert changes[1]["diagnostics"] == []
    assert "example.py:2:5 error" in requests[1] and "reportUndefinedVariable" in requests[2]
    assert "missing_name" not in requests[3] and "missing_name" not in requests[4]
    assert result["verification"]["status"] == "failed"
    assert next(e for e in events if e.get("tool_call_id") == "test" and e["type"] == "tool_result")["outcome"]["exit_code"] == 3


def test_untrusted_server_is_ignored_and_missing_server_does_not_fail_edit(project):
    root, _ = project
    write_json(root / ".nano" / "settings.json", {"python_lsp": {"argv": [str(root / "missing.exe")]}})
    config = Config(root)
    assert not config.data["python_lsp"]
    config.trust()
    config = Config(root, overrides={"permission_mode": "acceptEdits"})
    actions = iter([_write(), ModelReply("Done.")])
    events = []
    with Session(root, config=config, model=lambda *_: next(actions), emit=events.append) as session:
        assert session.run("Write a file.")["status"] == "completed"
    assert "missing_name" in (root / "example.py").read_text(encoding="utf-8")
    assert sum(e.get("reason") == "python_lsp_unavailable" for e in events) == 1


def test_slow_server_times_out_without_blocking_the_edit(project):
    root, _ = project
    actions = iter([_write(), ModelReply("Done.")])
    with Session(root, config=_config(project, "slow", 0.15), model=lambda *_: next(actions)) as session:
        result = session.run("Write a file.")
        assert result["status"] == "completed" and result["verification"]["status"] == "not_run"
        assert not any(e["type"] == "python_diagnostics" for e in session.transcript)
    assert (root / "example.py").is_file()


def test_cancelling_pending_diagnostics_stops_the_process(project):
    root, _ = project
    edit_recorded = threading.Event()

    def emit(event):
        if event["type"] == "tool_result":
            edit_recorded.set()

    with Session(root, config=_config(project, "slow", 10), model=lambda *_: _write(), emit=emit) as session:
        with ThreadPoolExecutor(max_workers=1) as executor:
            run = executor.submit(session.run, "Write a file.")
            assert edit_recorded.wait(3)
            diagnostics = session._python_diagnostics
            with diagnostics._condition:
                assert diagnostics._condition.wait_for(lambda: diagnostics._process is not None, 3)
                process = diagnostics._process
            session.cancel()
            assert run.result(timeout=4)["status"] == "interrupted"
        assert process.poll() is not None
    assert (root / "example.py").is_file()
