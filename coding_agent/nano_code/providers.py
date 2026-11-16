"""OpenAI-compatible and Anthropic Messages adapters for the shared runtime."""

import json
import time
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from cancellation import check_cancelled
from context_budget import fit_request
from exceptions import AgentAPIError, ModelRequestBudgetError, ModelResponseError
from model_api import ChatModel, RequestUsage, _checked_reply


def effort_levels(data):
    endpoint = urlsplit(data["base_url"])
    if (
        data["provider"] == "openai" and endpoint.scheme == "https"
        and endpoint.hostname == "api.deepseek.com"
        and data["model"] in {"deepseek-flash", "deepseek-v4-pro"}
    ):
        return ("low", "high", "max")
    return ()


def create_model(config, tools):
    d = config.data
    if not config.api_key:
        raise ValueError(f"未配置 API Key。请设置 {d['api_key_env']}，或运行 nano-code setup。")
    if d["provider"] == "anthropic":
        return AnthropicModel(config, tools)
    host = urlsplit(d["base_url"]).hostname
    effort = d.get("effort")
    return ChatModel(
        OpenAI(api_key=config.api_key, base_url=d["base_url"], max_retries=0, timeout=d["request_timeout"]),
        name=d["model"], temperature=d["temperature"], top_p=d["top_p"], streaming=d["streaming"],
        max_attempts=d["max_retries"], retry_delay=d["retry_base_delay"], tools=tools, context_limit=d["context_limit"],
        request_limit=d.get("request_limit"), disable_thinking=host == "api.deepseek.com",
        effort=effort if effort in effort_levels(d) else None,
        stream_usage=host in {"api.openai.com", "api.deepseek.com"} or d.get("stream_usage") is True,
    )


def anthropic_messages(messages):
    system, turns = [], []
    for m in messages:
        role = m["role"]
        if role in {"system", "developer"}:
            system.append(m.get("content") or "")
            continue
        content = []
        if role == "tool":
            role = "user"
            content.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m.get("content") or ""})
        else:
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
        if not content:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(content)
        else:
            turns.append({"role": role, "content": content})
    return "\n\n".join(system), turns


class AnthropicModel:
    def __init__(self, config, tools):
        self.config, self.tools = config, tools
        self.name = config.data["model"]
        self.requests_sent = 0
        self.streaming = config.data["streaming"]
        self.context_limit = config.data["context_limit"]
        self.client = httpx.Client(timeout=config.data["request_timeout"], headers={
            "x-api-key": config.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        })

    def __call__(self, messages, allow_tools, emit):
        d = self.config.data
        fitted, output = fit_request(messages, self.tools if allow_tools else [], limit=self.context_limit, max_output=4096, emit=emit)
        system, turns = anthropic_messages(fitted)
        payload = {"model": self.name, "system": system, "messages": turns, "max_tokens": output, "stream": self.streaming}
        if allow_tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"]["description"], "input_schema": t["function"]["parameters"]} for t in self.tools]
        base = d["base_url"].rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        for attempt in range(d["max_retries"]):
            check_cancelled()
            if d.get("request_limit") and self.requests_sent >= d["request_limit"]:
                raise ModelRequestBudgetError("已达到模型请求上限。")
            self.requests_sent += 1
            emit({"type": "model_request", "number": self.requests_sent, "attempt": attempt + 1})
            usage = RequestUsage(emit)
            try:
                with self.client.stream("POST", url, json=payload) as response:
                    check_cancelled()
                    response.raise_for_status()
                    if not self.streaming:
                        raw = json.loads(response.read())
                        snapshot = raw.get("usage") or {}
                        usage.report(snapshot.get("input_tokens"), snapshot.get("output_tokens"))
                    else:
                        raw = self._stream(response, emit, usage)
                check_cancelled()
                break
            except (ValueError, AttributeError, KeyError, TypeError) as error:
                raise ModelResponseError("Anthropic 响应格式无效。", reason="invalid_response") from error
            except httpx.HTTPError as error:
                code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 0
                if attempt + 1 == d["max_retries"] or code and code not in {408, 409, 429, 500, 502, 503, 504, 529}:
                    raise AgentAPIError(f"Anthropic 请求失败（{code or type(error).__name__}）") from error
                for _ in range(int(10 * min(30, d["retry_base_delay"] * 2 ** min(attempt, 8)))):
                    check_cancelled()
                    time.sleep(0.1)
        reason = raw.get("stop_reason")
        if reason not in {"end_turn", "tool_use", "stop_sequence"}:
            raise ModelResponseError(f"Anthropic 响应未正常结束: {reason}", reason="response_truncated" if reason == "max_tokens" else "incomplete_response")
        text, calls = [], []
        for block in raw.get("content", []):
            if block["type"] == "text":
                text.append(block["text"])
            elif block["type"] == "tool_use":
                calls.append({"id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(block["input"])}})
        if not self.streaming and text:
            emit({"type": "text_delta", "text": "\n".join(text)})
        return _checked_reply("\n".join(text) or None, calls,
                              "tool_calls" if reason == "tool_use" else "stop", allow_tools=allow_tools)

    def _stream(self, response, emit, usage):
        raw, blocks, fragments, stopped = {}, {}, {}, False
        for line in response.iter_lines():
            check_cancelled()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            kind = event["type"]
            if kind == "message_start":
                raw = event["message"]
                snapshot = raw.get("usage") or {}
                usage.report(snapshot.get("input_tokens"), snapshot.get("output_tokens"))
            elif kind == "content_block_start":
                index = event["index"]
                blocks[index] = event["content_block"]
                fragments[index] = ""
            elif kind == "content_block_delta":
                index, delta = event["index"], event["delta"]
                if delta["type"] == "text_delta":
                    blocks[index]["text"] += delta["text"]
                    emit({"type": "text_delta", "text": delta["text"]})
                elif delta["type"] == "input_json_delta":
                    fragments[index] += delta["partial_json"]
            elif kind == "message_delta":
                raw.update(event["delta"])
                snapshot = event.get("usage") or {}
                usage.report(snapshot.get("input_tokens"), snapshot.get("output_tokens"))
            elif kind == "message_stop":
                stopped = True
            elif kind == "error":
                raise ModelResponseError("Anthropic stream error", reason="stream_interrupted")
        if not stopped:
            raise ModelResponseError("Anthropic stream disconnected", reason="incomplete_response")
        for index, fragment in fragments.items():
            if fragment:
                blocks[index]["input"] = json.loads(fragment)
        raw["content"] = [blocks[i] for i in sorted(blocks)]
        return raw

    def summarize(self, messages, emit):
        return self(messages, False, emit).text or ""
