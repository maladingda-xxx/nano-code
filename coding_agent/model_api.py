"""OpenAI-compatible requests and conversion to the core's reply format."""

import random
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI

from agent_core import EventHandler, ModelReply
from context_budget import fit_request
from exceptions import AgentAPIError, ModelRequestBudgetError, ModelResponseError

TOOL_MARKUP_MARKERS = ("<｜｜DSML｜｜", "<|tool_call|>", "<tool_calls>")


def _checked_reply(
    text: str | None, calls: list[dict], finish_reason: str | None, *, allow_tools: bool,
) -> ModelReply:
    if finish_reason == "length":
        raise ModelResponseError("模型回复达到长度上限，内容已截断。", reason="response_truncated")
    if finish_reason == "content_filter":
        raise ModelResponseError("模型回复被服务端内容过滤中止。", reason="response_filtered")
    if finish_reason is None:
        raise ModelResponseError("模型回复不完整，缺少结束标记。", reason="incomplete_response")
    if finish_reason not in {"stop", "tool_calls"}:
        raise ModelResponseError(
            f"模型返回了不支持的结束原因：{finish_reason}", reason="unexpected_finish_reason",
        )
    if (finish_reason == "tool_calls" and not calls) or (calls and not allow_tools):
        raise ModelResponseError("模型返回了异常的工具调用。", reason="invalid_tool_calls")
    if (
        not allow_tools and not calls and text
        and any(marker in text for marker in TOOL_MARKUP_MARKERS)
    ):
        raise ModelResponseError(
            "模型在未启用工具时输出了工具调用标记。", reason="tool_markup_without_tools",
        )

    ids = set()
    for call in calls:
        call_id = call.get("id")
        function = call.get("function")
        if (
            call.get("type") != "function"
            or not isinstance(call_id, str) or not call_id.strip() or call_id in ids
            or not isinstance(function, dict)
            or any(
                not isinstance(function.get(key), str) or not function[key].strip()
                for key in ("name", "arguments")
            )
        ):
            raise ModelResponseError("模型返回的工具调用不完整或重复。", reason="invalid_tool_calls")
        ids.add(call_id)
    if not calls and not (text and text.strip()):
        raise ModelResponseError("模型未返回有效文本。", reason="empty_response")
    return ModelReply(text, calls)


def _completion_reply(response, *, allow_tools: bool) -> ModelReply:
    choices = getattr(response, "choices", None)
    if not choices:
        return _checked_reply(None, [], None, allow_tools=allow_tools)
    choice = choices[0]
    message = getattr(choice, "message", None)
    calls = [
        call.model_dump(include={"id", "type", "function"})
        for call in (getattr(message, "tool_calls", None) or [])
    ]
    return _checked_reply(
        getattr(message, "content", None), calls, getattr(choice, "finish_reason", None),
        allow_tools=allow_tools,
    )


class ChatModel:
    def __init__(
        self, client: OpenAI, name: str, temperature: float, top_p: float,
        streaming: bool, max_attempts: int, retry_delay: float, tools: list[dict],
        *, context_limit: int = 80000, request_limit: int | None = None,
        disable_thinking: bool = False,
    ):
        self.client = client
        self.name = name
        self.temperature = temperature
        self.top_p = top_p
        self.streaming = streaming
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.tools = tools
        self.context_limit = context_limit
        self.request_limit = request_limit
        self.requests_sent = 0
        self.disable_thinking = disable_thinking

    def _create(self, emit: EventHandler, **kwargs: Any) -> Any:
        kwargs["messages"], kwargs["max_tokens"] = fit_request(
            kwargs["messages"], kwargs.get("tools") or [], limit=self.context_limit,
            max_output=kwargs["max_tokens"], emit=emit,
        )
        if self.disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        for attempt in range(1, self.max_attempts + 1):
            if self.request_limit is not None and self.requests_sent >= self.request_limit:
                raise ModelRequestBudgetError(f"已达到模型请求上限（{self.request_limit} 次）。")
            self.requests_sent += 1
            emit({"type": "model_request", "number": self.requests_sent, "attempt": attempt})
            try:
                return self.client.chat.completions.create(model=self.name, **kwargs)
            except (APIConnectionError, APIStatusError) as error:
                retryable = isinstance(error, APIConnectionError) or (
                    error.status_code in {408, 409, 429} or error.status_code >= 500
                )
                if not retryable or attempt == self.max_attempts:
                    raise AgentAPIError(f"模型请求失败: {error}") from error
                if self.requests_sent == self.request_limit:
                    raise ModelRequestBudgetError(
                        f"请求失败且已达到模型请求上限（{self.request_limit} 次）。"
                    ) from error
                delay = self.retry_delay * 2 ** (attempt - 1) + random.uniform(0, 1)
                emit({"type": "notice", "text": f"模型请求失败，{delay:.1f} 秒后重试。"})
                time.sleep(delay)
        raise AgentAPIError("模型请求次数必须大于零。")

    def summarize(self, messages: list[dict], emit: EventHandler) -> str:
        response = self._create(emit, messages=messages, temperature=0, max_tokens=500)
        return _completion_reply(response, allow_tools=False).text or ""

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
        reply = _completion_reply(response, allow_tools=allow_tools)
        self._emit_usage(response.usage, emit)
        if reply.text:
            emit({"type": "text_delta", "text": reply.text})
        return reply

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
        with stream:
            chunks = iter(stream)
            while True:
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                except Exception as error:
                    raise ModelResponseError(
                        f"流式响应中断：{error}", reason="stream_interrupted",
                    ) from error
                self._emit_usage(chunk.usage, emit)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if delta.content:
                    text.append(delta.content)
                    emit({"type": "text_delta", "text": delta.content})
                for call in delta.tool_calls or []:
                    index = getattr(call, "index", None)
                    if not isinstance(index, int) or index < 0:
                        raise ModelResponseError("工具调用缺少有效序号。", reason="invalid_tool_calls")
                    entry = calls.setdefault(index, {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                    })
                    if call.id:
                        entry["id"] = call.id
                    if call.type:
                        entry["type"] = call.type
                    if call.function:
                        for key in ("name", "arguments"):
                            fragment = getattr(call.function, key)
                            if fragment is not None:
                                if not isinstance(fragment, str):
                                    raise ModelResponseError(
                                        "工具调用片段格式有误。", reason="invalid_tool_calls",
                                    )
                                entry["function"][key] += fragment
        return _checked_reply(
            "".join(text) or None, [calls[index] for index in sorted(calls)],
            finish_reason, allow_tools=True,
        )
