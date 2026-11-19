import asyncio
import base64
import hashlib
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import pytest
from openai import OpenAI
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nano_code import Session
from nano_code.commands import diagnostics
from nano_code.config import Config
from nano_code.provider_login import openrouter_login, prepare_login
from nano_code.provider_profiles import Login, PROVIDERS, azure_endpoint
from nano_code.ui import interactive


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("API_KEY", "legacy-fixture")
    return root


def config_for(project):
    return Config(project, overrides={
        "provider": "openai", "base_url": PROVIDERS["deepseek"].base_url,
        "model": "deepseek-flash", "streaming": False, "effort": "auto",
    })


def mock_login_http(monkeypatch, handler):
    client = httpx.AsyncClient

    def create(**kwargs):
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("nano_code.provider_login.httpx.AsyncClient", create)


def chat_response(model, message, reason="stop"):
    return {"id": "response", "model": model, "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": reason}]}


def sse_response(events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join(f"data: {json.dumps(event)}\n\n" for event in events))


def attach_chat_transport(model, handler):
    client = model.client
    model.client = OpenAI(
        api_key=client.api_key, base_url=client.base_url, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.close()


class Capture(DummyOutput):
    def __init__(self):
        self.text = ""

    def write(self, data):
        self.text += data

    def write_raw(self, data):
        self.text += data


async def until(condition):
    async with asyncio.timeout(4):
        while not condition():
            await asyncio.sleep(0.02)


def test_terminal_login_and_model_search(project, monkeypatch):
    credential = "anthropic-terminal-fixture"

    def catalog(request):
        assert str(request.url) == "https://api.anthropic.com/v1/models"
        assert request.headers["x-api-key"] == credential
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"data": [{"id": "claude-test"}, {"id": "bad\x1bid"}]})

    mock_login_http(monkeypatch, catalog)
    output = Capture()

    async def exercise():
        with create_pipe_input() as pipe, Session(project, config=config_for(project)) as session:
            terminal = asyncio.create_task(interactive(session, terminal_input=pipe, terminal_output=output))
            pipe.send_text("/login inline-secret-fixture\r")
            pipe.send_text("/login\r")
            await until(lambda: "选择登录方式" in output.text)
            pipe.send_text("\x1b[B\r")
            await until(lambda: "选择模型供应商" in output.text)
            pipe.send_text("anthropic\r")
            await until(lambda: "输入隐藏" in output.text)
            pipe.send_text(credential + "\r")
            await until(lambda: session.config.logins.read("anthropic") is not None)
            assert session.config.active_login is None
            pipe.send_text("/model\r")
            await until(lambda: "选择模型" in output.text)
            pipe.send_text("/anthropic\r")
            await until(lambda: session.current_model_choice == "anthropic:claude-test")
            assert session.config.api_key == credential
            assert diagnostics(session)["provider"] == "anthropic"

            def chat(request):
                assert str(request.url) == "https://api.anthropic.com/v1/messages"
                assert request.headers["x-api-key"] == credential
                assert json.loads(request.content)["model"] == "claude-test"
                return httpx.Response(200, json={"stop_reason": "end_turn",
                                                  "content": [{"type": "text", "text": "Claude replied"}]})

            model = session.ensure_model()
            model.client.close()
            model.client = httpx.Client(transport=httpx.MockTransport(chat), headers={"x-api-key": credential})
            pipe.send_text("Ping\r")
            await until(lambda: session.last_result is not None)
            assert session.last_result["status"] == "completed"
            assert session.messages[-1]["content"] == "Claude replied"
            pipe.send_text("/exit\r")
            await asyncio.wait_for(terminal, 4)
            history = (session.config.home / "history").read_text(encoding="utf-8")
            assert "/login" in history and credential not in history and "inline-secret-fixture" not in history
            assert credential not in output.text

    asyncio.run(exercise())
    assert config_for(project).logins.read("anthropic").models == ("claude-test",)


def test_browser_authorizes_and_lists_models(project, monkeypatch):
    authorization = {}

    def browser(url):
        parsed = urlsplit(url)
        assert parsed.netloc == "openrouter.ai" and parsed.path == "/auth"
        authorization.update({key: values[0] for key, values in parse_qs(parsed.query).items()})
        callback = authorization["callback_url"]
        with pytest.raises(HTTPError, match="400"):
            urlopen(callback + "?code=discard&state=wrong", timeout=2)
        query = urlencode({"code": "one-time-code", "state": authorization["state"]})
        with urlopen(callback + "?" + query, timeout=2) as response:
            assert response.status == 200
        return True

    def router(request):
        if request.url.path == "/api/v1/auth/keys":
            payload = json.loads(request.content)
            verifier = payload["code_verifier"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            assert challenge == authorization["code_challenge"]
            assert payload["code"] == "one-time-code" and payload["code_challenge_method"] == "S256"
            return httpx.Response(200, json={"key": "key-from-browser"})
        assert request.headers["authorization"] == "Bearer key-from-browser"
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        assert request.url.path == "/api/v1/models/user"
        return httpx.Response(200, json={"data": [
            {"id": "example/coder:free", "supported_parameters": ["tools"]},
            {"id": "example/text-only", "supported_parameters": []},
        ]})

    monkeypatch.setattr("nano_code.provider_login.webbrowser.open", browser)
    mock_login_http(monkeypatch, router)
    output = Capture()

    async def exercise():
        with create_pipe_input() as pipe, Session(project, config=config_for(project)) as session:
            terminal = asyncio.create_task(interactive(session, terminal_input=pipe, terminal_output=output))
            pipe.send_text("/login\r")
            await until(lambda: "选择登录方式" in output.text)
            pipe.send_text("\r")
            await until(lambda: "选择模型供应商" in output.text)
            pipe.send_text("\r")
            await until(lambda: session.config.logins.read("openrouter") is not None)
            assert "openrouter:example/coder:free" in session.model_choices()
            assert "openrouter:example/text-only" not in session.model_choices()
            session.set_model("openrouter:example/coder:free")
            assert session.config.data["model"] == "example/coder:free"
            assert session.config.api_key == "key-from-browser"
            pipe.send_text("/exit\r")
            await asyncio.wait_for(terminal, 4)

    asyncio.run(exercise())


def test_cancel_browser_wait_keeps_prior_login(project, monkeypatch):
    monkeypatch.setattr("nano_code.provider_login.webbrowser.open", lambda _: False)
    output = Capture()
    config = config_for(project)
    previous = Login("openrouter", "current-key", ("existing/model",))
    config.logins.save(previous)

    async def exercise():
        with create_pipe_input() as pipe, Session(project, config=config) as session:
            terminal = asyncio.create_task(interactive(session, terminal_input=pipe, terminal_output=output))
            pipe.send_text("/login\r")
            await until(lambda: "选择登录方式" in output.text)
            pipe.send_text("\r")
            await until(lambda: "选择模型供应商" in output.text)
            pipe.send_text("\r")
            await until(lambda: "https://openrouter.ai/auth" in output.text)
            pipe.send_text("\x03")
            await until(lambda: output.text.count("❯") > 2)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(terminal, 4)

    asyncio.run(exercise())
    assert config.logins.read("openrouter") == previous
    with pytest.raises(ValueError, match="超时"):
        asyncio.run(openrouter_login(lambda _: None, timeout=0.05))


def test_switch_requests_keep_provider_and_credential_isolated(project):
    config = config_for(project)
    records = [Login("openai", "only-openai", ("shared",)), Login("openrouter", "only-router", ("shared",))]
    for login in records:
        config.logins.save(login)
    with Session(project, config=config_for(project)) as session:
        session.set_effort("high")
        session.messages.append({"role": "user", "content": "Existing conversation"})
        received = []

        def chat(request):
            payload = json.loads(request.content)
            assert any(msg["content"] == "Existing conversation" for msg in payload["messages"])
            received.append((request.url.host, request.headers["authorization"], payload["model"]))
            return httpx.Response(200, json=chat_response(payload["model"], {"content": "OK"}))

        for provider in ("openai", "openrouter"):
            assert f"{provider}:shared" in session.model_choices()
            old = session.model
            session.set_model(f"{provider}:shared")
            if old:
                assert old.client.is_closed()
            attach_chat_transport(session.ensure_model(), chat)
            assert session.run(f"Test {provider}")["status"] == "completed"

        session.set_model("legacy:deepseek-flash")
        assert session.config.api_key == "legacy-fixture"
        assert session.config.data["effort"] == "high"
        assert session.ensure_model().requests_sent == 2
        assert received == [("api.openai.com", "Bearer only-openai", "shared"),
                            ("openrouter.ai", "Bearer only-router", "shared")]


def test_rejected_credentials_are_redacted_and_existing_record_survives(project, monkeypatch):
    config = config_for(project)
    previous = Login("openai", "old-key", ("gpt-5.4-mini",))
    config.logins.save(previous)
    mock_login_http(monkeypatch, lambda _: httpx.Response(401, json={"error": "new-secret-fixture"}))
    with pytest.raises(ValueError, match="HTTP 401") as error:
        asyncio.run(prepare_login("openai", "new-secret-fixture"))
    assert "new-secret-fixture" not in str(error.value)
    assert config.logins.read("openai") == previous
    with Session(project, config=config) as session:
        session.set_model("openai:gpt-5.4-mini")
        attach_chat_transport(session.ensure_model(), lambda _: httpx.Response(401, json={"error": "old-key"}))
        assert session.run("Ping")["reason"] == "authentication_failed"
        assert session.model.requests_sent == 1
        assert "old-key" not in json.dumps(session.transcript)


def test_azure_endpoint_is_resource_scoped():
    assert azure_endpoint("https://my-model.openai.azure.com/") == "https://my-model.openai.azure.com/openai/v1"
    with pytest.raises(ValueError):
        azure_endpoint("https://my-model.openai.azure.com.example.test/openai/v1")
    with pytest.raises(ValueError):
        azure_endpoint("http://my-model.openai.azure.com")


@pytest.mark.parametrize("streaming", [False, True])
def test_new_router_model_effort_completes_tool_round(project, monkeypatch, streaming):
    name = "example/future-coder"
    previous = "example/plain-model"
    details = [{"type": "reasoning.encrypted", "data": "opaque-fixture", "id": "reasoning-1", "index": 0}]

    def catalog(request):
        assert request.url.path == "/api/v1/models/user"
        assert request.headers["authorization"] == "Bearer saved-key"
        return httpx.Response(200, json={"data": [
            {"id": name, "supported_parameters": ["tools"], "reasoning": {
                "supported_efforts": ["xhigh", "medium", "none"], "mandatory": True,
            }},
            {"id": previous, "supported_parameters": ["tools", "include_reasoning"]},
        ]})

    mock_login_http(monkeypatch, catalog)
    config = config_for(project)
    config.data["streaming"] = streaming
    config.logins.directory.mkdir(parents=True)
    # Simulate an existing login saved before capability metadata was added.
    (config.logins.directory / "openrouter.json").write_text(
        json.dumps({"credential": "saved-key", "models": [previous]}), encoding="utf-8",
    )
    requests = []

    def chat(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = {
                "content": None, "reasoning_details": details, "tool_calls": [{
                    "id": "call-1", "type": "function", "function": {"name": "git_status", "arguments": "{}"},
                }],
            }
            reason = "tool_calls"
        else:
            message, reason = {"content": "Done"}, "stop"
        if body.get("stream"):
            for call in message.get("tool_calls", ()):
                call["index"] = 0
            return sse_response([{
                "id": "response", "model": body["model"], "object": "chat.completion.chunk", "created": 0,
                "choices": [{"index": 0, "delta": {"role": "assistant", **message}, "finish_reason": reason}],
            }])
        return httpx.Response(200, json=chat_response(body["model"], message, reason))

    with Session(project, config=config) as session:
        assert f"openrouter:{name}" not in session.model_choices()
        asyncio.run(session.refresh_model_catalogs())
        choice = f"openrouter:{name}"
        assert choice in session.model_choices()
        assert session.model_efforts(choice) == ("auto", "medium", "xhigh")
        session.set_model(choice)
        session.set_effort("xhigh")
        attach_chat_transport(session.ensure_model(), chat)
        assert session.run("Check status")["status"] == "completed"
        session.set_model(f"openrouter:{previous}")
        assert session.config.data["effort"] == "auto"
        assert session.run("Switch route")["status"] == "completed"

    assert requests[0]["reasoning"] == requests[1]["reasoning"] == {"effort": "xhigh"}
    assert any(msg.get("reasoning_details") == details for msg in requests[1]["messages"])
    assert "reasoning" not in requests[2]
    assert all("reasoning_details" not in msg for msg in requests[2]["messages"])
    assert config.logins.read("openrouter").models == (previous,)


@pytest.mark.parametrize("streaming", [False, True])
def test_claude_catalog_effort_and_signed_tool_history(project, monkeypatch, streaming):
    name = "claude-future-fixture"
    signed_blocks = [
        {"type": "thinking", "thinking": "private thinking", "signature": "fixture-signature"},
        {"type": "tool_use", "id": "toolu_fixture", "name": "git_status", "input": {}},
    ]

    def catalog(request):
        assert request.headers["x-api-key"] == "fixture-key"
        return httpx.Response(200, json={"data": [{"id": name, "capabilities": {"effort": {
            "supported": True, "low": {"supported": True}, "medium": {"supported": False},
            "high": {"supported": True},
        }}}]})

    mock_login_http(monkeypatch, catalog)
    login, verified = asyncio.run(prepare_login("anthropic", "fixture-key"))
    assert verified and login.reasoning[name] == ("low", "high")
    config = config_for(project)
    config.logins.save(login)
    requests = []

    def chat(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            blocks, reason = signed_blocks, "tool_use"
        else:
            blocks, reason = [{"type": "text", "text": "Done"}], "end_turn"
        if streaming:
            events = [{"type": "message_start", "message": {}}]
            for index, block in enumerate(blocks):
                if block["type"] == "thinking":
                    events.append({"type": "content_block_start", "index": index,
                                   "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                    events.extend({"type": "content_block_delta", "index": index,
                                   "delta": {"type": field + "_delta", field: block[field]}}
                                  for field in ("thinking", "signature"))
                else:
                    events.append({"type": "content_block_start", "index": index, "content_block": block})
            return sse_response([*events, {"type": "message_delta", "delta": {"stop_reason": reason}},
                                 {"type": "message_stop"}])
        return httpx.Response(200, json={"stop_reason": reason, "content": blocks})

    restored_config = config_for(project)
    restored_config.data["streaming"] = streaming
    with Session(project, config=restored_config) as session:
        session.set_model(f"anthropic:{name}")
        assert session.model_efforts(session.current_model_choice) == ("auto", "low", "high")
        session.set_effort("high")
        model = session.ensure_model()
        model.client.close()
        model.client = httpx.Client(transport=httpx.MockTransport(chat))
        assert session.run("Check status")["status"] == "completed"
        session.set_effort("auto")
        assert session.run("Continue")["status"] == "completed"

    assert requests[0]["output_config"] == requests[1]["output_config"] == {"effort": "high"}
    assert any(msg["content"] == signed_blocks for msg in requests[1]["messages"])
    assert "output_config" not in requests[2]


@pytest.mark.parametrize("name", ["kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3.2-fixture"])
def test_kimi_reasoning_effort_changes_next_request(project, name):
    config = config_for(project)
    config.logins.save(Login("kimi", "test-key", (name,)))
    requests = []

    def chat(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(name, {"content": "Done"}))

    with Session(project, config=config) as session:
        assert session.model_efforts(f"kimi:{name}") == ("auto", "low", "high", "max")
        assert session.model_efforts("kimi:kimi-k2.6") == ("auto",)
        session.set_model(f"kimi:{name}")
        session.set_effort("low")
        model = session.ensure_model()
        attach_chat_transport(model, chat)

        for level in ("low", "high", "max", "auto"):
            session.set_effort(level)
            assert session.ensure_model() is model
            assert session.run("Test reasoning effort")["status"] == "completed"

    assert [payload.get("reasoning_effort") for payload in requests] == ["low", "high", "max", None]
    assert "reasoning_effort" not in requests[-1]
    assert all("thinking" not in payload for payload in requests)


@pytest.mark.parametrize("provider, name", [
    ("gemini", "gemini-4-hydrogen"), ("kimi", "kimi-k3.1-pro"), ("minimax", "MiniMax-M3.1-Flash"),
])
def test_provider_metadata_survives_tool_followup_and_restart(project, provider, name):
    config = config_for(project)
    login = Login(provider, "test-key", (name,))
    config.logins.save(login)
    requests = []

    def chat(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            call = {"id": "test-tool", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}
            message = {"tool_calls": [call], "content": None}
            if provider == "gemini":
                call["extra_content"] = {"google": {"thought_signature": "opaque-signature"}}
            else:
                message["reasoning_content"] = "provider reasoning"
            return httpx.Response(200, json=chat_response(name, message, "tool_calls"))
        previous = next(m for m in payload["messages"] if m.get("tool_calls"))
        if provider == "gemini":
            assert previous["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "opaque-signature"
        else:
            assert previous["reasoning_content"] == "provider reasoning"
            assert "_reasoning_source" not in previous and "temperature" not in payload
        return httpx.Response(200, json=chat_response(name, {"content": "Done"}))

    with Session(project, config=config) as session:
        uid = session.uid
        session.set_model(f"{provider}:{name}")
        attach_chat_transport(session.ensure_model(), chat)
        assert session.run("Check status")["status"] == "completed"
        assert "opaque-signature" not in json.dumps(session.transcript)
    with Session(project, config=config_for(project), resume=uid) as session:
        session.set_model(f"{provider}:{name}")
        attach_chat_transport(session.ensure_model(), chat)
        assert session.run("Continue")["status"] == "completed"
