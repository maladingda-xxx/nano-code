"""Bound model-visible tool text while preserving the canonical session."""

import json
import re

ERROR_LINE = re.compile(r"traceback|\b(?:fail(?:ed|ure)?|error|exception|fatal|denied|timeout)\b|错误|失败|退出码", re.I)
CREDENTIAL = re.compile(
    r"\bBearer\s+[\w.~-]{8,}|\b(?:sk-|gh[opsu]_|github_pat_)[\w-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|(?:access[_-]?)?token)\s*[=:]\s*[^\s,;]+", re.I,
)


def mask_credentials(text: str) -> str:
    return CREDENTIAL.sub("[REDACTED]", text)


def _tool_excerpt(text: str, name: str, size: int) -> str:
    if name == "execute_command":
        lines = text.splitlines()
        evidence = []
        for index, line in enumerate(lines):
            if ERROR_LINE.search(line):
                evidence.append("\n".join(item[:250] for item in lines[max(0, index - 1):index + 2]))
        if evidence:
            unique = list(dict.fromkeys(evidence))
            evidence_text = "\n".join(unique[:2] + unique[-6:] if len(unique) > 8 else unique)
            error_room = size // 2
            excerpt = (evidence_text if len(evidence_text) <= error_room else
                       evidence_text[:error_room // 3] + "\n[… ]\n" + evidence_text[-(error_room * 2 // 3):])
            return text[:size // 5] + "\n[… ]\n" + excerpt + "\n[… ]\n" + text[-size // 4:]
    if name in {"search_workspace", "search_in_file"}:
        return text[:size * 3 // 4] + "\n[… ]\n" + text[-size // 5:]
    return text[:size // 2] + "\n[… ]\n" + text[-size // 2:]


def prune_results(
    messages: list[dict], cap: int, *, aggressive: bool = False, start_index: int = 0,
) -> tuple[list[dict], int]:
    """Use the zero-based index in canonical non-system history as the result reference."""
    result = []
    calls = {}
    index = start_index - 1
    clipped = 0
    latest_batch = next((i for i in range(len(messages) - 1, -1, -1)
                         if messages[i].get("tool_calls")), len(messages))
    for position, message in enumerate(messages):
        role = message["role"]
        if role not in {"system", "developer"} and not message.get("_runtime_notice"):
            index += 1
        if role == "assistant":
            calls = {call["id"]: call["function"] for call in message.get("tool_calls", ())}
        if role == "tool" and isinstance(content := message.get("content"), str):
            safe = mask_credentials(content)
            function = calls.get(message.get("tool_call_id"), {})
            name = function.get("name", "")
            size = min(cap, 1400) if name in {"edit_file", "write_file"} else cap
            if aggressive and position < latest_batch:
                size = max(256, size // 3)
            if len(safe) > size:
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (ValueError, TypeError):
                    arguments = {}
                path = arguments.get("file_path") if isinstance(arguments, dict) else None
                label = f"{name} {path[:160]}" if isinstance(path, str) else (name or "tool")
                safe = (
                    f"[read_tool_output reference {index}]\n"
                    f"[{mask_credentials(label)}: output shortened. Page the recorded text using the reference.]\n"
                    + _tool_excerpt(safe, name, size)
                )
                clipped += 1
            message = {**message, "content": safe} if safe != content else message
        result.append(message)
    return result, clipped


def read_output(messages: list[dict], reference: str, offset: int = 0, limit: int = 1600) -> str:
    """Read a bounded character range from an already recorded result."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000:
        raise ValueError("偏移须为非负整数，每页最多 4000 字符。")
    history = [m for m in messages if m["role"] not in {"system", "developer"}]
    if not re.fullmatch(r"[0-9]{1,9}", reference) or int(reference) >= len(history):
        raise ValueError("找不到当前会话中的工具输出引用。")
    message = history[int(reference)]
    if message["role"] != "tool" or not isinstance(message.get("content"), str):
        raise ValueError("引用未指向工具输出。")
    body = mask_credentials(message["content"])
    end = min(offset + limit, len(body))
    return f"工具输出 {reference}，字符 {min(offset, end)}–{end}/{len(body)}\n" + body[offset:end]
