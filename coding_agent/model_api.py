"""OpenAI-compatible requests and conversion to the core's reply format."""

import json
import random
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI

from agent_core import EventHandler, ModelReply, public_tool_call
from context_budget import DEFAULT_MAX_OUTPUT, fit_request
from cancellation import check_cancelled
from exceptions import AgentAPIError, ModelRequestBudgetError, ModelResponseError

TOOL_MARKUP_MARKERS = ("<｜｜DSML｜｜", "<|tool_call|>", "<tool_calls>")


class RequestUsage:
    """Convert one request's cumulative provider reports into usage deltas."""

    def __init__(self, emit: EventHandler):
        self.emit = emit
        self.reported: dict[str, int] = {}

    def report(self, input_tokens=None, output_tokens=None, total_tokens=None):
        counts = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        if total_tokens is None:
            parts = [counts[key] if counts[key] is not None else self.reported.get(key)
                     for key in ("input_tokens", "output_tokens")]
            if all(isinstance(value, int) for value in parts):
                counts["total_tokens"] = sum(parts)
        delta = {}
        for key, value in counts.items():
            if type(value) is not int or value < 0:
                continue
            previous = self.reported.get(key)
            if previous is None or value > previous:
                delta[key] = value - (previous or 0)
                self.reported[key] = value
        if delta:
            self.emit({"type": "usage", **delta})

    def chat(self, usage):
        if usage is not None:
            self.report(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)


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


def _gemini_signature(call):
    extra = getattr(call, "extra_content", None)
    google = extra.get("google") if isinstance(extra, dict) else None
    signature = google.get("thought_signature") if isinstance(google, dict) else None
    return signature if isinstance(signature, str) and signature else None


def _completion_reply(response, *, allow_tools: bool, gemini: bool = False) -> ModelReply:
    choices = getattr(response, "choices", None)
    if not choices:
        return _checked_reply(None, [], None, allow_tools=allow_tools)
    choice = choices[0]
    message = getattr(choice, "message", None)
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        entry = call.model_dump(include={"id", "type", "function"})
        if gemini and (signature := _gemini_signature(call)):
            entry["_gemini_signature"] = signature
        calls.append(entry)
    return _checked_reply(
        getattr(message, "content", None), calls, getattr(choice, "finish_reason", None),
        allow_tools=allow_tools,
    )


class ChatModel:
    def __init__(
        self, client: OpenAI, name: str, temperature: float, top_p: float,
        streaming: bool, max_attempts: int, retry_delay: float, tools: list[dict],
        *, context_limit: int = 80000, request_limit: int | None = None,
        disable_thinking: bool = False, effort: str | None = None, stream_usage: bool = False,
        vendor: str | None = None, private_errors: bool = False,
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
        self.effort = effort
        self.stream_usage = stream_usage
        self.vendor = vendor
        self.private_errors = private_errors

    @property
    def reasoning_source(self):
        if self.vendor == "minimax" or self.vendor == "kimi" and self.name.startswith(("kimi-k3", "kimi-k2.5", "kimi-k2.6")):
            return self.vendor
        return None

    def _request_messages(self, messages, tools):
        prepared = []
        for message in messages:
            message = dict(message)
            source = message.pop("_reasoning_source", None)
            if message["role"] == "assistant":
                if calls := message.get("tool_calls"):
                    message["tool_calls"] = [public_tool_call(call) for call in calls]
                    if self.vendor == "gemini":
                        for index, (original, outgoing) in enumerate(zip(calls, message["tool_calls"])):
                            signature = original.get("_gemini_signature")
                            if index == 0 and not signature and self.name.startswith(("gemini-3", "gemini-4")):
                                # Gemini permits this value for tool calls imported from other models.
                                signature = "skip_thought_signature_validator"
                            if signature:
                                outgoing["extra_content"] = {"google": {"thought_signature": signature}}
                if self.vendor == "openrouter" and source == f"openrouter:{self.name}":
                    reasoning = json.loads(message.pop("reasoning_content"))
                    details = reasoning.get("reasoning_details")
                    if isinstance(details, list):
                        message["reasoning_details"] = details
                    elif isinstance(text := reasoning.get("reasoning"), str):
                        message["reasoning"] = text
                elif self.disable_thinking and self.effort and tools:
                    if source is not None:
                        message.pop("reasoning_content", None)
                    message.setdefault("reasoning_content", "")
                elif self.reasoning_source is None or source != self.reasoning_source:
                    message.pop("reasoning_content", None)
            prepared.append(message)
        return prepared

    def _save_router_reasoning(self, reply, details, text):
        if isinstance(details, list) and details:
            payload = {"reasoning_details": details}
        elif isinstance(text, str) and text:
            payload = {"reasoning": text}
        else:
            return
        reply.reasoning_content = json.dumps(payload)
        reply.reasoning_source = f"openrouter:{self.name}"

    def _create(self, emit: EventHandler, **kwargs: Any) -> Any:
        tools = kwargs.get("tools") or []
        kwargs["messages"], max_tokens = fit_request(
            self._request_messages(kwargs["messages"], tools), tools, limit=self.context_limit,
            max_output=kwargs.pop("max_tokens"), emit=emit,
        )
        openai_reasoning = self.vendor == "openai" and self.name.startswith(("gpt-5", "o1", "o3", "o4"))
        modern_limit = openai_reasoning or self.vendor in {"azure", "minimax"}
        kwargs["max_completion_tokens" if modern_limit else "max_tokens"] = max_tokens
        if modern_limit or self.reasoning_source or self.vendor == "xai":
            kwargs.pop("temperature", None)
            kwargs.pop("top_p", None)
        if self.vendor == "minimax":
            kwargs["extra_body"] = {"reasoning_split": True}
            if self.name == "MiniMax-M3.1-Flash":
                kwargs["extra_body"]["reasoning_effort"] = "low"
        if self.effort and not self.disable_thinking:
            extra = kwargs.setdefault("extra_body", {})
            if self.vendor == "openrouter":
                extra["reasoning"] = {"effort": self.effort}
            else:
                extra["reasoning_effort"] = self.effort
        if self.vendor == "xai" and self.name == "grok-4.3":
            kwargs["reasoning_effort"] = "none"
        if self.disable_thinking:
            # The disabled mode is the previous default for official DeepSeek.
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if self.effort else "disabled"}}
            if self.effort:
                kwargs["extra_body"]["reasoning_effort"] = self.effort
        if kwargs.get("stream") and self.stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        for attempt in range(1, self.max_attempts + 1):
            check_cancelled()
            if self.request_limit is not None and self.requests_sent >= self.request_limit:
                raise ModelRequestBudgetError(f"已达到模型请求上限（{self.request_limit} 次）。")
            self.requests_sent += 1
            request_event = {"type": "model_request", "number": self.requests_sent, "attempt": attempt}
            if self.effort:
                request_event["effort"] = self.effort
            emit(request_event)
            try:
                response = self.client.chat.completions.create(model=self.name, **kwargs)
                try:
                    check_cancelled()
                except KeyboardInterrupt:
                    if kwargs.get("stream"):
                        response.close()
                    else:
                        RequestUsage(emit).chat(response.usage)
                    raise
                return response
            except (APIConnectionError, APIStatusError) as error:
                retryable = isinstance(error, APIConnectionError) or (
                    error.status_code in {408, 409, 429} or error.status_code >= 500
                )
                if not retryable or attempt == self.max_attempts:
                    if self.private_errors:
                        status = getattr(error, "status_code", None)
                        detail = f"HTTP {status}" if status else "网络连接失败"
                        if status in {401, 403}:
                            raise ModelResponseError(
                                f"供应商拒绝访问（{detail}）。请检查 /login 和模型权限。",
                                reason="authentication_failed",
                            ) from None
                        raise AgentAPIError(f"模型请求失败（{detail}）。") from None
                    raise AgentAPIError(f"模型请求失败: {error}") from error
                if self.requests_sent == self.request_limit:
                    raise ModelRequestBudgetError(
                        f"请求失败且已达到模型请求上限（{self.request_limit} 次）。"
                    ) from error
                delay = min(30, self.retry_delay * 2 ** min(attempt - 1, 8) + random.uniform(0, 1))
                emit({"type": "notice", "text": f"模型请求失败，{delay:.1f} 秒后重试。"})
                while delay > 0:
                    check_cancelled()
                    time.sleep(min(delay, 0.1))
                    delay -= 0.1
        raise AgentAPIError("模型请求次数必须大于零。")

    def summarize(self, messages: list[dict], emit: EventHandler) -> str:
        response = self._create(emit, messages=messages, temperature=0,
                                max_tokens=4096 if self.reasoning_source or self.vendor in {"openai", "azure"} else 500)
        RequestUsage(emit).chat(response.usage)
        return _completion_reply(response, allow_tools=False).text or ""

    def __call__(self, messages: list[dict], allow_tools: bool, emit: EventHandler) -> ModelReply:
        # Keep the fallback request compatible with endpoints that reject tool calling.
        streaming = self.streaming and allow_tools
        kwargs = {"tools": self.tools, "tool_choice": "auto"} if allow_tools else {}
        response = self._create(
            emit, messages=messages, temperature=self.temperature, top_p=self.top_p,
            max_tokens=16384 if self.reasoning_source else DEFAULT_MAX_OUTPUT, stream=streaming, **kwargs,
        )
        if streaming:
            return self._stream_reply(response, emit)
        RequestUsage(emit).chat(response.usage)
        reply = _completion_reply(response, allow_tools=allow_tools, gemini=self.vendor == "gemini")
        if self.vendor == "openrouter":
            message = response.choices[0].message
            self._save_router_reasoning(reply, getattr(message, "reasoning_details", None),
                                        getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None))
        elif self.reasoning_source or self.disable_thinking and self.effort:
            reply.reasoning_content = getattr(response.choices[0].message, "reasoning_content", None)
            reply.reasoning_source = self.reasoning_source
        if reply.text:
            emit({"type": "text_delta", "text": reply.text})
        return reply

    def _stream_reply(self, stream, emit: EventHandler) -> ModelReply:
        text = []
        reasoning = []
        router_details = []
        calls = {}
        finish_reason = None
        usage = RequestUsage(emit)
        with stream:
            chunks = iter(stream)
            while True:
                check_cancelled()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                except Exception as error:
                    detail = type(error).__name__ if self.private_errors else error
                    raise ModelResponseError(
                        f"流式响应中断：{detail}", reason="stream_interrupted",
                    ) from (None if self.private_errors else error)
                usage.chat(chunk.usage)
                check_cancelled()
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if (self.reasoning_source or self.disable_thinking and self.effort) and getattr(delta, "reasoning_content", None):
                    reasoning.append(delta.reasoning_content)
                if self.vendor == "openrouter":
                    details = getattr(delta, "reasoning_details", None)
                    if isinstance(details, list):
                        router_details.extend(details)
                    fragment = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if isinstance(fragment, str):
                        reasoning.append(fragment)
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
                    if self.vendor == "gemini" and (signature := _gemini_signature(call)):
                        entry["_gemini_signature"] = signature
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
        reply = _checked_reply(
            "".join(text) or None, [calls[index] for index in sorted(calls)],
            finish_reason, allow_tools=True,
        )
        if self.vendor == "openrouter":
            self._save_router_reasoning(reply, router_details, "".join(reasoning))
        elif reasoning:
            reply.reasoning_content = "".join(reasoning)
            reply.reasoning_source = self.reasoning_source
        return reply
