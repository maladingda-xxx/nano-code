"""Execute a turn independently of the terminal and model API."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from time import perf_counter
from uuid import uuid4

from exceptions import AgentAPIError, ModelResponseError, TurnPersistenceError

EventHandler = Callable[[dict], None]
RequestModel = Callable[[list[dict], bool, EventHandler], "ModelReply"]


@dataclass
class ModelReply:
    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


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

    messages.append({"role": "user", "content": prompt})
    status = "completed"
    reason = "reply"
    verification = {"status": "not_run", "tool_call_id": None, "round": None}
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
            allow_tools = round_number < max_tool_rounds
            if not allow_tools:
                reason = "tool_round_limit"
                publish({"type": "notice", "text": "已达到工具调用上限，正在总结已有结果。"})
            publish({"type": "model_start", "round": round_number + 1})
            try:
                reply = request_model(messages, allow_tools, publish)
            except AgentAPIError as error:
                if not allow_tools or isinstance(error, ModelResponseError):
                    raise
                publish({"type": "notice", "text": "模型调用失败，正在尝试无工具模式。"})
                reply = request_model(messages, False, publish)
                allow_tools = False
                reason = "model_fallback"
            if reply.tool_calls and not allow_tools:
                raise ModelResponseError("当前请求未启用工具调用。", reason="invalid_tool_calls")
            if not reply.tool_calls:
                if not reply.text or not reply.text.strip():
                    raise ModelResponseError("模型未返回有效文本。", reason="empty_response")
                messages.append({"role": "assistant", "content": reply.text})
                publish({"type": "reply", "text": reply.text}, durable=True)
                break

            pending = list(reply.tool_calls)
            batch_index = len(messages)
            batch = {
                "role": "assistant", "content": reply.text, "tool_calls": pending.copy(),
            }
            messages.append(batch)
            while pending:
                call = pending[0]
                tool_scope = {"call": call, "tool_call_id": call["id"], "round": round_number + 1}
                publish({"type": "tool_request", **tool_scope}, durable=True)
                started = perf_counter()
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
        reason = error.reason if isinstance(error, ModelResponseError) else "api_error"
        publish({"type": "error", "reason": reason, "text": str(error)})
    finally:
        if save and reason != "persistence_error" and save(messages) is False:
            status = "error"
            reason = "persistence_error"
            publish({"type": "error", "reason": reason, "text": "无法自动保存会话。"})

    end = {"type": "turn_end", "status": status, "reason": reason, "verification": verification}
    try:
        publish(end, durable=reason != "persistence_error")
    except TurnPersistenceError as error:
        status = "error"
        publish({"type": "error", "reason": "persistence_error", "text": str(error)})
        publish({**end, "status": status, "reason": "persistence_error"})
    return status
