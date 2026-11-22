"""Bound model-visible tool text while preserving the canonical session."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import file_state

ERROR_LINE = re.compile(r"traceback|\b(?:fail(?:ed|ure)?|error|exception|fatal|denied|timeout)\b|错误|失败|退出码", re.I)
CREDENTIAL = re.compile(
    r"\bBearer\s+[\w.~-]{8,}|\b(?:sk-|gh[opsu]_|github_pat_)[\w-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|(?:access[_-]?)?token)\s*[=:]\s*[^\s,;]+", re.I,
)
FOLDABLE = {
    "read_file": "file_path",
    "search_in_file": "file_path",
    "search_workspace": "dir_path",
    "glob_files": "pattern",
}


@dataclass(frozen=True)
class PruningStats:
    shortened_results: int
    folded_results: int


def is_history_message(message: dict) -> bool:
    return message["role"] not in {"system", "developer"} and not message.get("_runtime_notice")


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
    view, stats = project_results(messages, cap, aggressive=aggressive, start_index=start_index)
    return view, stats.shortened_results


def project_results(
    messages: list[dict], cap: int, *, aggressive: bool = False, start_index: int = 0,
    root: Path | None = None, invalidate_reads: bool = True,
) -> tuple[list[dict], PruningStats]:
    """Use the zero-based index in canonical non-system history as the result reference."""
    result = []
    calls = {}
    index = start_index - 1
    clipped = folded = 0
    latest_batch = next((i for i in range(len(messages) - 1, -1, -1)
                         if messages[i].get("tool_calls")), len(messages))
    for position, message in enumerate(messages):
        role = message["role"]
        if is_history_message(message):
            index += 1
        if role == "assistant":
            calls = {call["id"]: call["function"] for call in message.get("tool_calls", ())}
        if role == "tool" and isinstance(content := message.get("content"), str):
            safe = mask_credentials(content)
            function = calls.get(message.get("tool_call_id"), {})
            name = function.get("name", "")
            size = min(cap, 1400) if name in {"edit_file", "write_file"} else cap
            old = aggressive and position < latest_batch
            if old:
                size = max(256, size // 3)
            may_fold = old and name in FOLDABLE and len(safe) > 256 and not safe.startswith((
                "目录约定：", "工具错误", "工具参数无效", "工具执行异常", "用户中断了",
                "执行记录保存失败", "文件未变化",
            ))
            if may_fold or len(safe) > size:
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (ValueError, TypeError):
                    arguments = {}
                arguments = arguments if isinstance(arguments, dict) else {}
                path = arguments.get("file_path")
                label = f"{name} {path[:160]}" if isinstance(path, str) else (name or "tool")
                reference = f"[read_tool_output reference {index}]"
                replacement = None
                if may_fold:
                    scope = arguments.get(FOLDABLE[name], "." if name == "search_workspace" else None)
                    if isinstance(scope, str) and scope:
                        label = f"{name} {mask_credentials(scope)[:160]}"
                        if isinstance(query := arguments.get("query"), str):
                            label += f" ({mask_credentials(query)[:80]})"
                        placeholder = f"{reference}\n[{label}: older output folded. Page the recorded text using the reference.]"
                        if len(safe) - len(placeholder) >= 256:
                            replacement = placeholder
                            folded += 1
                if replacement is None and len(safe) > size:
                    replacement = (
                        f"{reference}\n"
                        f"[{mask_credentials(label)}: output shortened. Page the recorded text using the reference.]\n"
                        + _tool_excerpt(safe, name, size)
                    )
                if replacement is not None:
                    if invalidate_reads and name == "read_file" and isinstance(path, str):
                        file_state.invalidate(path, tool_call_id=message.get("tool_call_id"), root=root)
                    safe = replacement
                    clipped += 1
            message = {**message, "content": safe} if safe != content else message
        result.append(message)
    return result, PruningStats(clipped, folded)


def read_output(messages: list[dict], reference: str, offset: int = 0, limit: int = 1600) -> str:
    """Read a bounded character range from an already recorded result."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000:
        raise ValueError("偏移须为非负整数，每页最多 4000 字符。")
    history = [m for m in messages if is_history_message(m)]
    if not re.fullmatch(r"[0-9]{1,9}", reference) or int(reference) >= len(history):
        raise ValueError("找不到当前会话中的工具输出引用。")
    message = history[int(reference)]
    if message["role"] != "tool" or not isinstance(message.get("content"), str):
        raise ValueError("引用未指向工具输出。")
    body = mask_credentials(message["content"])
    end = min(offset + limit, len(body))
    return f"工具输出 {reference}，字符 {min(offset, end)}–{end}/{len(body)}\n" + body[offset:end]
