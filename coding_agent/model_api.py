"""OpenAI-compatible requests and conversion to the core's reply format."""

import random
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI

from agent_core import EventHandler, ModelReply
from exceptions import AgentAPIError


class ChatModel:
    def __init__(
        self, client: OpenAI, name: str, temperature: float, top_p: float,
        streaming: bool, max_attempts: int, retry_delay: float, tools: list[dict],
    ):
        self.client = client
        self.name = name
        self.temperature = temperature
        self.top_p = top_p
        self.streaming = streaming
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.tools = tools

    def _create(self, emit: EventHandler, **kwargs: Any) -> Any:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.client.chat.completions.create(model=self.name, **kwargs)
            except (APIConnectionError, APIStatusError) as error:
                retryable = isinstance(error, APIConnectionError) or (
                    error.status_code in {408, 409, 429} or error.status_code >= 500
                )
                if not retryable or attempt == self.max_attempts:
                    raise AgentAPIError(f"模型请求失败: {error}") from error
                delay = self.retry_delay * 2 ** (attempt - 1) + random.uniform(0, 1)
                emit({"type": "notice", "text": f"模型请求失败，{delay:.1f} 秒后重试。"})
                time.sleep(delay)
        raise AgentAPIError("模型请求次数必须大于零。")

    def summarize(self, messages: list[dict], emit: EventHandler) -> str:
        response = self._create(emit, messages=messages, temperature=0, max_tokens=500)
        return response.choices[0].message.content if response.choices else ""

    def __call__(self, messages: list[dict], allow_tools: bool, emit: EventHandler) -> ModelReply:
        # Keep the fallback request compatible with endpoints that reject tool calling.
        streaming = self.streaming and allow_tools
        kwargs = {"tools": self.tools, "tool_choice": "auto"} if allow_tools else {}
        response = self._create(
            emit, messages=messages, temperature=self.temperature, top_p=self.top_p,
            max_tokens=4096, stream=streaming, **kwargs,
        )
        if streaming:
            return self._stream_reply(response, emit)
        if not response.choices:
            return ModelReply()
        message = response.choices[0].message
        self._emit_usage(response.usage, emit)
        if message.content:
            emit({"type": "text_delta", "text": message.content})
        calls = [
            {"id": call.id, "type": "function", "function": call.function.model_dump()}
            for call in (message.tool_calls or [])
        ]
        return ModelReply(message.content, calls)

    @staticmethod
    def _emit_usage(usage, emit: EventHandler) -> None:
        if usage:
            emit({
                "type": "usage", "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens, "total_tokens": usage.total_tokens,
            })

    def _stream_reply(self, stream, emit: EventHandler) -> ModelReply:
        text = []
        calls = {}
        finish_reason = None
        try:
            with stream:
                for chunk in stream:
                    self._emit_usage(chunk.usage, emit)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    finish_reason = choice.finish_reason or finish_reason
                    delta = choice.delta
                    if delta.content:
                        text.append(delta.content)
                        emit({"type": "text_delta", "text": delta.content})
                    for call in delta.tool_calls or []:
                        entry = calls.setdefault(call.index, {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                        })
                        if call.id:
                            entry["id"] = call.id
                        if call.function:
                            entry["function"]["name"] += call.function.name or ""
                            entry["function"]["arguments"] += call.function.arguments or ""
        except Exception as error:
            raise AgentAPIError(f"流式响应中断: {error}") from error
        if calls and (finish_reason not in {"tool_calls", "stop"} or any(
            not call["id"] or not call["function"]["name"] for call in calls.values()
        )):
            raise AgentAPIError("工具调用流不完整。")
        return ModelReply("".join(text) or None, [calls[index] for index in sorted(calls)])
