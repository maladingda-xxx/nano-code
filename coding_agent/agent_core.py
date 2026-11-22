"""Execute a turn independently of the terminal and model API."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from time import perf_counter
from uuid import uuid4

from edit_context import edit_scope
from cancellation import check_cancelled
from exceptions import AgentAPIError, ModelResponseError, TurnPersistenceError

EventHandler = Callable[[dict], None]
RequestModel = Callable[[list[dict], bool, EventHandler], "ModelReply"]


def public_tool_call(call):
    return {key: value for key, value in call.items() if not key.startswith("_")}


@dataclass
class ModelReply:
    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str | None = field(default=None, repr=False)
    reasoning_source: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolResult:
    text: str
    status: str = "ok"
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False
    verification: str | None = None
    invalidates_verification: bool = False


def run_turn(
    messages: list[dict],
    prompt: str,
    *,
    request_model: RequestModel,
    dispatch: Callable[[dict], ToolResult | str] | None = None,
    emit: EventHandler,
    max_tool_rounds: int,
    prepare: Callable[[list[dict]], list[dict]] | None = None,
    save: Callable[[list[dict]], object] | None = None,
    session_id: str | None = None,
    prepare_with_events: Callable[[list[dict], EventHandler], list[dict]] | None = None,
    checkpoint: Callable[[dict, list[dict]], None] | None = None,
    dispatch_with_events: Callable[[dict, EventHandler], ToolResult | str] | None = None,
) -> str:
    """Append a turn to history and report progress via structured events."""
    if dispatch is None and dispatch_with_events is None:
        raise ValueError("run_turn requires a tool dispatcher")
    scope = {"session_id": session_id or uuid4().hex, "turn_id": uuid4().hex}

    def publish(event: dict, *, durable: bool = False) -> None:
        scoped = {**event, **scope}
        if durable and checkpoint:
            checkpoint(scoped, messages)
        emit(scoped)

    def request_reply(request_messages: list[dict], allow_tools: bool) -> ModelReply:
        try:
            return request_model(request_messages, allow_tools, publish)
        except ModelResponseError as error:
            if error.reason != "response_truncated":
                raise
        check_cancelled()
        publish({"type": "notice", "reason": "response_truncated",
                 "text": "单次模型输出已截断，正在从已完成的步骤继续。"})
        instruction = (
            "根据已有工具结果继续当前任务。若还需操作，先执行一个小步骤，把大型改动拆成多轮工具调用。"
            "已经执行的操作无需重复。任务完成后简要报告结果与验证情况。" if allow_tools else
            "上一次生成被截断。请按原任务要求给出精简、完整的答复，保留关键结果和未完成事项。"
        )
        return request_model([*request_messages, {
            "role": "user", "content": instruction, "_runtime_notice": True, "_output_retry": True,
        }], allow_tools, publish)

    messages.append({"role": "user", "content": prompt})
    status = "completed"
    reason = "reply"
    verification = {"status": "not_run", "tool_call_id": None, "round": None}
    changes: dict[str, int] = {}
    tool_calls_made = 0
    budget_notice_sent = False
    pending = []
    batch = None
    batch_index = 0
    try:
        if checkpoint:
            publish({"type": "turn_start"}, durable=True)
        if prepare_with_events:
            messages[:] = prepare_with_events(messages, publish)
        elif prepare:
            messages[:] = prepare(messages)
        for round_number in range(max_tool_rounds + 1):
            check_cancelled()
            allow_tools = round_number < max_tool_rounds
            request_messages = messages
            if not allow_tools:
                reason = "tool_round_limit"
                publish({"type": "notice", "text": "已达到工具调用上限，正在总结已有结果。"})
                # Tools are omitted from the final request, so tell the model to stop
                # emitting tool calls instead of relying on the user-visible notice.
                request_messages = [*messages, {
                    "role": "user",
                    "content": "工具调用轮数已用完。请不要再请求工具，基于已有工具结果直接给出最终答复。",
                    "_runtime_notice": True,
                }]
            elif (
                round_number > 0 and max_tool_rounds - round_number <= 2
                and not budget_notice_sent
            ):
                remaining = max_tool_rounds - round_number
                budget_notice_sent = True
                publish({"type": "notice", "text": f"剩余 {remaining} 个工具轮，提示模型优先完成必要工作和验证。"})
                request_messages = [*messages, {
                    "role": "user",
                    "content": (
                        f"仅剩 {remaining} 个工具轮。请优先完成必要的修改和验证，并如实报告未完成事项。"
                    ),
                    "_runtime_notice": True,
                }]
            publish({"type": "model_start", "round": round_number + 1})
            try:
                reply = request_reply(request_messages, allow_tools)
            except AgentAPIError as error:
                if not allow_tools or not error.allow_fallback:
                    raise
                publish({"type": "notice", "text": "模型调用失败，正在尝试无工具模式。"})
                reply = request_reply(messages, False)
                allow_tools = False
                reason = "model_fallback"
            if reply.tool_calls and not allow_tools:
                raise ModelResponseError("当前请求未启用工具调用。", reason="invalid_tool_calls")
            assistant = {"role": "assistant", "content": reply.text}
            if reply.reasoning_content is not None:
                assistant["reasoning_content"] = reply.reasoning_content
                if reply.reasoning_source is not None:
                    assistant["_reasoning_source"] = reply.reasoning_source
            if not reply.tool_calls:
                if not reply.text or not reply.text.strip():
                    raise ModelResponseError("模型未返回有效文本。", reason="empty_response")
                messages.append(assistant)
                publish({"type": "reply", "text": reply.text}, durable=True)
                break

            pending = list(reply.tool_calls)
            batch_index = len(messages)
            batch = {**assistant, "tool_calls": pending.copy()}
            messages.append(batch)
            while pending:
                check_cancelled()
                call = pending[0]
                tool_calls_made += 1
                tool_scope = {"call": public_tool_call(call), "tool_call_id": call["id"], "round": round_number + 1}
                publish({"type": "tool_request", **tool_scope}, durable=True)
                started = perf_counter()
                with edit_scope(scope["session_id"], scope["turn_id"], call["id"], round_number + 1):
                    if dispatch_with_events is not None:
                        def publish_tool(event: dict) -> None:
                            publish({**event, "tool_call_id": call["id"], "round": round_number + 1})

                        result = dispatch_with_events(call, publish_tool)
                    else:
                        result = dispatch(call)
                elapsed_ms = round((perf_counter() - started) * 1000)
                if isinstance(result, str):
                    result = ToolResult(result)
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result.text,
                })
                pending.pop(0)
                if (
                    result.status == "ok" and call["function"]["name"] in {"edit_file", "write_file"}
                    and not result.text.startswith("内容相同")
                ):
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                    except (TypeError, ValueError):
                        arguments = {}
                    file_path = arguments.get("file_path") if isinstance(arguments, dict) else None
                    if isinstance(file_path, str) and file_path:
                        changes[file_path] = changes.get(file_path, 0) + 1
                if result.verification is not None:
                    verification = {
                        "status": result.verification, "tool_call_id": call["id"],
                        "round": round_number + 1,
                    }
                elif result.invalidates_verification and verification["status"] in {"passed", "failed"}:
                    verification["status"] = "stale"
                outcome = asdict(result)
                outcome.pop("text")
                publish({
                    "type": "tool_result", **tool_scope, "result": result.text,
                    "outcome": {**outcome, "elapsed_ms": elapsed_ms},
                }, durable=True)
                if result.status == "interrupted":
                    raise KeyboardInterrupt
    except (KeyboardInterrupt, TurnPersistenceError) as error:
        if isinstance(error, TurnPersistenceError):
            status = "error"
            reason = "persistence_error"
            publish({"type": "error", "reason": reason, "text": str(error)})
            pending_text = "执行记录保存失败。先检查工作区和执行记录，再决定是否重试。"
        else:
            status = "interrupted"
            reason = "user_interrupt"
            pending_text = "用户中断了本轮。请先检查当前工作区，再判断是否需要重试。"
        if batch_index < len(messages) and messages[batch_index] is batch:
            completed_ids = {
                message["tool_call_id"] for message in messages[batch_index + 1:]
                if message["role"] == "tool"
            }
            for call in pending:
                if call["id"] in completed_ids:
                    continue
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": pending_text,
                })
    except AgentAPIError as error:
        status = "error"
        reason = error.reason
        publish({"type": "error", "reason": reason, "text": str(error)})
    finally:
        if save and reason != "persistence_error" and save(messages) is False:
            status = "error"
            reason = "persistence_error"
            publish({"type": "error", "reason": reason, "text": "无法自动保存会话。"})

    end = {
        "type": "turn_end", "status": status, "reason": reason, "verification": verification,
        "changes": {"files": sorted(changes), "edits": sum(changes.values())},
        "tool_calls": tool_calls_made,
    }
    try:
        publish(end, durable=reason != "persistence_error")
    except TurnPersistenceError as error:
        status = "error"
        publish({"type": "error", "reason": "persistence_error", "text": str(error)})
        publish({**end, "status": status, "reason": "persistence_error"})
    return status
