"""Build a bounded request view without rewriting the session history."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from itertools import chain

from exceptions import ContextBudgetError

DEFAULT_MAX_OUTPUT = 4096


@dataclass(frozen=True)
class ModelBudget:
    limit: int
    output: int
    safety: int
    tokenizer: str
    window: int | None
    source: str


@dataclass(frozen=True)
class ContextBudget:
    limit: int
    messages: int
    overhead: int
    reserved_output: int
    safety: int = 0
    method: str = "estimated"

    @property
    def message_budget(self):
        return self.limit - self.overhead - self.reserved_output - self.safety

    @property
    def remaining(self):
        return self.message_budget - self.messages

    @property
    def input_tokens(self):
        return self.messages + self.overhead

    @property
    def pressure(self):
        return self.input_tokens / max(1, self.limit - self.reserved_output - self.safety)


@cache
def _encoding(model: str = ""):
    try:
        import tiktoken

        return tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("cl100k_base")
    except Exception:
        # The optional tokenizer can also be unavailable offline.
        return None


def _estimate(value, model: str = "") -> int:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    encoding = _encoding(model) if model else _encoding()
    if encoding is not None:
        return len(encoding.encode_ordinary(text))
    return (len(text.encode("utf-8", errors="replace")) + 1) // 2


def estimate_tokens(messages: list[dict], model: str = "") -> int:
    return sum(4 + _estimate(message, model) for message in messages)


def context_breakdown(messages, tools, *, limit: int, max_output: int = DEFAULT_MAX_OUTPUT,
                      safety: int = 0, model: str = "") -> ContextBudget:
    # Small context windows leave up to a quarter of their space for generation.
    output = min(max_output, max(1, limit // 4))
    overhead = 8 + (_estimate(tools, model) if tools else 0)
    encoding = _encoding(model) if model else _encoding()
    method = "tiktoken + protocol estimate" if encoding is not None else "UTF-8 heuristic estimate"
    return ContextBudget(limit, estimate_tokens(messages, model), overhead, output, safety, method)


def _split_turns(messages: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    instructions = []
    turns = []
    for message in messages:
        role = message["role"]
        if role in {"system", "developer"}:
            instructions.append(message)
        else:
            if message.get("_runtime_notice") and turns:
                turns[-1].append(message)
                continue
            if not turns or role == "user":
                turns.append([])
            turns[-1].append(message)
    return instructions, turns


def _public(messages: list[dict]) -> list[dict]:
    """Strip internal request markers before sending messages to a provider."""
    return [
        {key: value for key, value in message.items() if not key.startswith("_")}
        for message in messages
    ]


def _tool_batches(messages: list[dict]) -> list[list[dict]]:
    blocks: list[list[dict]] = []
    for message in messages:
        if blocks and (message.get("_runtime_notice") or
                       message["role"] == "tool" and blocks[-1][0].get("tool_calls")):
            blocks[-1].append(message)
        else:
            blocks.append([message])
    return blocks


def _fit_latest_batch(batch: list[dict], available: int, model: str) -> list[dict] | None:
    if estimate_tokens(batch, model) <= available:
        return batch
    max_length = max(
        (len(message["content"]) for message in batch
         if message["role"] in {"assistant", "tool"} and isinstance(message.get("content"), str)),
        default=0,
    )
    while max_length > 80:
        max_length = max(80, max_length // 2)
        head = max_length // 2
        clipped = []
        for message in batch:
            content = message.get("content")
            if (message["role"] == "tool" or message["role"] == "assistant" and not message.get("tool_calls")
                and not message.get("reasoning_content")) and isinstance(content, str) and len(content) > max_length:
                label = "工具输出" if message["role"] == "tool" else "回复内容"
                content = content[:head] + f"\n[{label}已压缩]\n" + content[-head:]
                message = {**message, "content": content}
            clipped.append(message)
        if estimate_tokens(clipped, model) <= available:
            return clipped
    return None


def _keep_suffix(blocks: list[list[dict]], available: int, model: str) -> tuple[list[list[dict]], int]:
    first = len(blocks)
    while first > 0:
        cost = estimate_tokens(blocks[first - 1], model)
        if cost > available:
            break
        available -= cost
        first -= 1
    return blocks[first:], available


def fit_request(
    messages: list[dict], tools: list[dict], *, limit: int, max_output: int,
    emit: Callable[[dict], None], safety: int = 0, model: str = "",
) -> tuple[list[dict], int]:
    breakdown = context_breakdown(messages, tools, limit=limit, max_output=max_output, safety=safety, model=model)
    output, budget, before = breakdown.reserved_output, breakdown.message_budget, breakdown.messages
    if breakdown.remaining >= 0:
        return _public(messages), output

    reduced = [m for m in messages if not m.get("_context_optional")]
    if len(reduced) < len(messages) and estimate_tokens(reduced, model) <= budget:
        fitted = reduced
        return _report_pruning(emit, messages, fitted, before, budget, output, model)
    instructions, turns = _split_turns(reduced)
    current = turns[-1] if turns else []
    user = current[:1] if current and current[0]["role"] == "user" else []
    blocks = _tool_batches(current[len(user):])
    available = budget - estimate_tokens(instructions + user, model)
    latest = _fit_latest_batch(blocks.pop(), available, model) if available >= 0 and blocks else []
    if available < 0 or latest is None:
        raise ContextBudgetError(
            f"必要上下文超出请求预算（消息可用约 {max(0, budget)} tokens）。"
            "请缩短当前输入或项目说明，或在模型支持的范围内提高 CONTEXT_LIMIT_TOKENS。"
        )
    available -= estimate_tokens(latest, model)
    kept_blocks, available = _keep_suffix(blocks, available, model)
    kept_turns, _ = _keep_suffix(turns[:-1], available, model) if len(kept_blocks) == len(blocks) else ([], 0)
    fitted = [
        *instructions, *chain.from_iterable(kept_turns), *user,
        *chain.from_iterable(kept_blocks), *latest,
    ]
    return _report_pruning(emit, messages, fitted, before, budget, output, model)


def _report_pruning(emit, messages, fitted, before, budget, output, model):
    after = estimate_tokens(fitted, model)
    emit({
        "type": "notice", "reason": "context_compacted", "operation": "pruning",
        "estimated_before": before, "estimated_after": after, "message_budget": budget,
        "reserved_output": output, "dropped_messages": len(messages) - len(fitted),
        "text": f"已裁剪本次请求上下文（约 {before} → {after} tokens）。",
    })
    return _public(fitted), output
