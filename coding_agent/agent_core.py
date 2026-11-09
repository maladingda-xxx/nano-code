"""Execute a turn independently of the terminal and model API."""

from collections.abc import Callable
from dataclasses import dataclass, field

from exceptions import AgentAPIError

EventHandler = Callable[[dict], None]
RequestModel = Callable[[list[dict], bool, EventHandler], "ModelReply"]


@dataclass
class ModelReply:
    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


def run_turn(
    messages: list[dict],
    prompt: str,
    *,
    request_model: RequestModel,
    dispatch: Callable[[dict], str],
    emit: EventHandler,
    max_tool_rounds: int,
    prepare: Callable[[list[dict]], list[dict]] | None = None,
    save: Callable[[list[dict]], object] | None = None,
) -> str:
    """Append a turn to history and report progress via structured events."""
    messages.append({"role": "user", "content": prompt})
    status = "completed"
    pending = []
    batch = None
    try:
        if prepare:
            messages[:] = prepare(messages)
        for round_number in range(max_tool_rounds + 1):
            allow_tools = round_number < max_tool_rounds
            if not allow_tools:
                emit({"type": "notice", "text": "已达到工具调用上限，正在总结已有结果。"})
            emit({"type": "model_start", "round": round_number + 1})
            try:
                reply = request_model(messages, allow_tools, emit)
            except AgentAPIError:
                if not allow_tools:
                    raise
                emit({"type": "notice", "text": "模型调用失败，正在尝试无工具模式。"})
                reply = request_model(messages, False, emit)
                allow_tools = False
            if not reply.tool_calls or not allow_tools:
                if not reply.text:
                    raise AgentAPIError("模型未返回有效文本。")
                messages.append({"role": "assistant", "content": reply.text})
                emit({"type": "reply", "text": reply.text})
                break

            pending = list(reply.tool_calls)
            batch = {
                "role": "assistant", "content": reply.text, "tool_calls": pending.copy(),
            }
            messages.append(batch)
            while pending:
                call = pending[0]
                emit({"type": "tool_request", "call": call})
                result = dispatch(call)
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result,
                })
                pending.pop(0)
                emit({"type": "tool_result", "call": call, "result": result})
    except KeyboardInterrupt:
        status = "interrupted"
        completed_ids = {
            message["tool_call_id"] for message in messages if message["role"] == "tool"
        }
        for call in pending if any(message is batch for message in messages) else []:
            if call["id"] not in completed_ids:
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": "用户中断了本轮。请先检查当前工作区，再判断是否需要重试。",
                })
    except AgentAPIError as error:
        status = "error"
        emit({"type": "error", "text": str(error)})
    finally:
        if save:
            save(messages)

    emit({"type": "turn_end", "status": status})
    return status
