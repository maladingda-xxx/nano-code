import json

import httpx
from openai import OpenAI
from prompt_toolkit.formatted_text import fragment_list_width

from model_api import ChatModel
from nano_code import Session
from nano_code.config import Config
from nano_code.providers import AnthropicModel, create_model, effort_levels
from nano_code.work_status import WorkStatus


def text(fragments):
    return "".join(fragment[1] for fragment in fragments)


def test_status_keeps_fixed_art_width_and_shows_latest_turn_metrics():
    work = WorkStatus()
    work.start(100)
    frames = [work.fragments(80, now=100 + (frame / 4)) for frame in range(6)]
    assert len({text(frame) for frame in frames}) > 3
    assert {text(frame).index("composing") for frame in frames} == {5}
    assert all("\n" not in text(frame) for frame in frames)
    assert "↓ — tokens" in text(frames[0])
    work.event({"type": "usage", "output_tokens": 2300})
    work.event({"type": "model_request", "effort": "max"})
    assert "(1m 24s · ↓ 2.3k tokens · max effort)" in text(work.fragments(80, now=184))
    work.event({"type": "tool_request", "call": {"function": {"name": "execute_command"}}})
    assert "execute_command" in text(work.fragments(80, now=185))
    narrow = work.fragments(26, now=185)
    assert fragment_list_width(narrow) <= 26 and "\n" not in text(narrow)
    assert "composing···" in text(narrow) and "1m 25s" in text(narrow)
    assert "tokens" not in text(narrow)
    assert text(work.fragments(80, waiting="等待授权")) == "等待授权"
    assert fragment_list_width(work.fragments(7, waiting="等待授权")) == 6
    work.cancel()
    assert "composing" not in text(work.fragments(80))
    work.event({"type": "turn_end"})
    assert not work.fragments(80)


def test_trumpet_and_potion_alternate_only_when_a_new_turn_starts():
    work = WorkStatus()
    work.start(100)
    assert "composing···" in text(work.fragments(80, now=101))
    work.event({"type": "turn_end"})
    work.start(200)
    frames = [work.fragments(80, now=200 + index / 4) for index in range(6)]
    assert {text(frame).index("brewing···") for frame in frames} == {5}
    assert len({frame[1][1] for frame in frames}) > 1  # The fluid changes height.
    assert [frame[3][1] for frame in frames[:3]] == [".", "∘", "°"]
    work.event({"type": "model_request", "effort": "max"})
    work.event({"type": "usage", "output_tokens": 25})
    assert "brewing··· (2s · ↓ 25 tokens · max effort)" in text(work.fragments(80, now=202))
    narrow = work.fragments(16, color=False, now=202)
    assert fragment_list_width(narrow) <= 16 and "brewing···" in text(narrow)
    assert "2s" in text(narrow) and "\n" not in text(narrow)
    assert "brewing" not in text(work.fragments(80, waiting="等待授权"))
    work.cancel()
    assert "brewing" not in text(work.fragments(80))
    work.stop()
    assert not work.fragments(80)
    work.start(300)
    assert "composing··· (0s · ↓ — tokens · thinking)" in text(work.fragments(80, now=300))


def chat_stream(chunks):
    rows = ["data: " + json.dumps({
        "id": "mock", "object": "chat.completion.chunk", "created": 0, "model": "mock", **chunk,
    }) for chunk in chunks]
    return httpx.Response(200, text="\n\n".join([*rows, "data: [DONE]", ""]),
                          headers={"content-type": "text/event-stream"})


def test_chat_usage_delivers_incremental_values_once_per_request():
    def response(_request):
        snapshot = {"prompt_tokens": 50, "completion_tokens": 2300, "total_tokens": 2350}
        return chat_stream([
            {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}},
            {"choices": [], "usage": snapshot},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": snapshot},
        ])

    events = []
    with OpenAI(api_key="fixture", base_url="https://example.test",
                http_client=httpx.Client(transport=httpx.MockTransport(response))) as client:
        model = ChatModel(client, "mock", 0.3, 1.0, True, 1, 0, [])
        for _ in range(2):
            assert model([{"role": "user", "content": "test"}], True, events.append).text == "hello"
    usage = [event for event in events if event["type"] == "usage"]
    assert [event["output_tokens"] for event in usage] == [100, 2200, 100, 2200]
    assert sum(event.get("total_tokens", 0) for event in usage) == 4700


def test_anthropic_usage_counts_repeated_cumulative_snapshots_once(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "fixture")
    config = Config(tmp_path, overrides={"provider": "anthropic", "base_url": "https://example.test",
                                         "model": "mock", "streaming": True})
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    model = AnthropicModel(config, [])
    model.client.close()
    wire = "\n\n".join([*("data: " + json.dumps(event) for event in events), ""])
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=wire))) as client:
        model.client = client
        seen = []
        assert model([{"role": "user", "content": "test"}], False, seen.append).text == "hi"
    usage = [event for event in seen if event["type"] == "usage"]
    assert [event["output_tokens"] for event in usage] == [1, 3]
    assert sum(event["total_tokens"] for event in usage) == 29


def test_deepseek_tool_loop_uses_selected_effort_and_replays_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "fixture")
    monkeypatch.setenv("NANO_CODE_HOME", str(tmp_path / "config"))
    config = Config(tmp_path, overrides={"provider": "openai", "base_url": "https://api.deepseek.com",
                                         "model": "deepseek-flash", "streaming": False, "effort": "max"})
    requests, events = [], []

    def response(request):
        data = json.loads(request.content)
        requests.append(data)
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "reasoning_content": "provider state",
                       "tool_calls": [{"id": "t1", "type": "function", "function": {
                           "name": "read_file", "arguments": '{"file_path":"example.txt"}',
                       }}]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Done", "reasoning_content": "final state"}
            reason = "stop"
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message,
                                                      "finish_reason": reason}]})

    (tmp_path / "example.txt").write_text("hello", encoding="utf-8")
    with Session(tmp_path, config=config, emit=events.append) as session:
        model = create_model(config, session.tool_schemas)
        model.client.close()
        model.client = OpenAI(api_key="fixture", base_url=config.data["base_url"],
                              http_client=httpx.Client(transport=httpx.MockTransport(response)))
        session.model = model
        assert session.run("Inspect the fixture")["status"] == "completed"
        session.resume(session.uid)
        assert session.messages[-1]["reasoning_content"] == "final state"
    assert all(data["thinking"] == {"type": "enabled"} and data["reasoning_effort"] == "max" for data in requests)
    assert requests[1]["messages"][-2]["reasoning_content"] == "provider state"
    assert [e["effort"] for e in events if e["type"] == "model_request"] == ["max", "max"]
    assert not effort_levels({**config.data, "base_url": "https://example.test"})
