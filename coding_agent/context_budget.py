"""Build a bounded request view without rewriting the session history."""

import json
from collections.abc import Callable
from functools import cache
from itertools import chain

from exceptions import ContextBudgetError


@cache
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # The optional tokenizer can also be unavailable offline.
        return None


def _estimate(value) -> int:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    encoding = _encoding()
    if encoding is not None:
        return len(encoding.encode_ordinary(text))
    return (len(text.encode("utf-8", errors="replace")) + 1) // 2


def estimate_tokens(messages: list[dict]) -> int:
    return sum(4 + _estimate(message) for message in messages)


def _split_turns(messages: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    instructions = []
    turns = []
    for message in messages:
        role = message["role"]
        if role in {"system", "developer"}:
            instructions.append(message)
        else:
            if not turns or role == "user":
                turns.append([])
            turns[-1].append(message)
    return instructions, turns


def _tool_batches(messages: list[dict]) -> list[list[dict]]:
    blocks: list[list[dict]] = []
    for message in messages:
        if message["role"] == "tool" and blocks and blocks[-1][0].get("tool_calls"):
            blocks[-1].append(message)
        else:
            blocks.append([message])
    return blocks


def _fit_latest_batch(batch: list[dict], available: int) -> list[dict] | None:
    if estimate_tokens(batch) <= available:
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
            if message["role"] in {"assistant", "tool"} and isinstance(content, str) and len(content) > max_length:
                label = "工具输出" if message["role"] == "tool" else "回复内容"
                content = content[:head] + f"\n[{label}已压缩]\n" + content[-head:]
                message = {**message, "content": content}
            clipped.append(message)
        if estimate_tokens(clipped) <= available:
            return clipped
    return None


def _keep_suffix(blocks: list[list[dict]], available: int) -> tuple[list[list[dict]], int]:
    first = len(blocks)
    while first > 0:
        cost = estimate_tokens(blocks[first - 1])
        if cost > available:
            break
        available -= cost
        first -= 1
    return blocks[first:], available


def fit_request(
    messages: list[dict], tools: list[dict], *, limit: int, max_output: int,
    emit: Callable[[dict], None],
) -> tuple[list[dict], int]:
    # Small context windows leave up to a quarter of their space for generation.
    output = min(max_output, max(1, limit // 4))
    overhead = 8 + (_estimate(tools) if tools else 0)
    budget = limit - output - overhead
    before = estimate_tokens(messages)
    if before <= budget:
        return messages, output

    instructions, turns = _split_turns(messages)
    current = turns[-1] if turns else []
    user = current[:1] if current and current[0]["role"] == "user" else []
    blocks = _tool_batches(current[len(user):])
    available = budget - estimate_tokens(instructions + user)
    latest = _fit_latest_batch(blocks.pop(), available) if available >= 0 and blocks else []
    if available < 0 or latest is None:
        raise ContextBudgetError(
            f"必要上下文超出请求预算（消息可用约 {max(0, budget)} tokens）。"
            "请缩短当前输入或项目说明，或在模型支持的范围内提高 CONTEXT_LIMIT_TOKENS。"
        )
    available -= estimate_tokens(latest)
    kept_blocks, available = _keep_suffix(blocks, available)
    kept_turns, _ = _keep_suffix(turns[:-1], available) if len(kept_blocks) == len(blocks) else ([], 0)
    fitted = [
        *instructions, *chain.from_iterable(kept_turns), *user,
        *chain.from_iterable(kept_blocks), *latest,
    ]
    after = estimate_tokens(fitted)
    emit({
        "type": "notice", "reason": "context_compacted",
        "estimated_before": before, "estimated_after": after, "message_budget": budget,
        "reserved_output": output, "dropped_messages": len(messages) - len(fitted),
        "text": f"已压缩本次请求上下文（约 {before} → {after} tokens）。",
    })
    return fitted, output
