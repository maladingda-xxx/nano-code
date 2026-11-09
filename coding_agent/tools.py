"""
tools.py —— Coding Agent 的工具系统
=====================================
这个文件定义了 Agent 可以调用的所有工具函数，以及发送给 API 的工具 JSON Schema。

自定义入口可使用 register_tool() 注册工具及其 JSON Schema。

"""

import difflib
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agent_core import ToolResult
from changes import read_bytes, record_edit
from command_runner import run_process
from session_store import set_session_root
from workspace import workspace_path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from exceptions import (
    ToolInputError,
    ToolExecutionError,
    ToolSecurityError,
    ToolTimeoutError,
)

# 全局 Rich Console 实例，供所有工具函数使用
console = Console()

# ============================================================
# 工作区根目录 —— 由 agent.py 在启动时设置
# ============================================================

_workspace_root = Path.cwd().resolve()


def set_workspace_root(path: str | Path) -> None:
    global _workspace_root
    _workspace_root = Path(path).resolve()
    set_session_root(_workspace_root)


def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ============================================================
# 1. read_file —— 读取文件内容（带行号，像 cat -n）
# ============================================================

def read_file(file_path: str, line_start: int | None = None, line_end: int | None = None) -> str:
    """
    读取指定文件的内容，返回带行号的文本。

    参数:
        file_path:   文件的绝对路径或相对路径
        line_start:  起始行号（从 1 开始），不传则从第 1 行开始
        line_end:    结束行号（包含），不传则读到文件末尾

    返回:
        带行号的文本内容，格式同 cat -n 命令

    异常:
        ToolInputError:     参数不合法
        ToolExecutionError: 文件不存在、无法读取等
    """
    if not file_path or not file_path.strip():
        raise ToolInputError("file_path 不能为空", tool_name="read_file")

    path = workspace_path(_workspace_root, file_path, "read_file")

    if not path.exists():
        raise ToolExecutionError(f"文件不存在 —— {file_path}", tool_name="read_file")
    if not path.is_file():
        raise ToolExecutionError(f"路径是一个目录，不是文件 —— {file_path}", tool_name="read_file")

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except PermissionError:
        raise ToolExecutionError(f"没有权限读取文件 —— {file_path}", tool_name="read_file")
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="latin-1") as f:
                lines = f.readlines()
        except Exception as e:
            raise ToolExecutionError(
                f"文件编码无法识别 —— {file_path}，详情: {e}", tool_name="read_file"
            )
    except Exception as e:
        raise ToolExecutionError(
            f"读取文件时发生异常 —— {file_path}，详情: {e}", tool_name="read_file"
        )

    if not lines:
        return "文件为空。"
    total_lines = len(lines)
    start = line_start if line_start is not None else 1
    end = line_end if line_end is not None else total_lines

    start = max(1, start)
    end = min(total_lines, end)

    if start > end:
        raise ToolInputError(
            f"line_start ({line_start}) 大于 line_end ({line_end})", tool_name="read_file"
        )

    result_parts = []
    for i in range(start - 1, end):
        line_num = i + 1
        line = lines[i].rstrip("\r\n")
        result_parts.append(f"{line_num:>6}\t{line}")

    return "\n".join(result_parts)


# ============================================================
# 2. list_directory —— 列出目录内容
# ============================================================

def list_directory(dir_path: str) -> str:
    """
    列出指定目录下的所有文件和子目录，标注类型和文件大小。

    异常:
        ToolInputError:     参数为空
        ToolExecutionError: 目录不存在或无法访问
    """
    if not dir_path or not dir_path.strip():
        raise ToolInputError("dir_path 不能为空", tool_name="list_directory")

    path = workspace_path(_workspace_root, dir_path, "list_directory")

    if not path.exists():
        raise ToolExecutionError(f"目录不存在 —— {dir_path}", tool_name="list_directory")
    if not path.is_dir():
        raise ToolExecutionError(f"路径不是目录 —— {dir_path}", tool_name="list_directory")

    try:
        entries = sorted(path.iterdir())
    except PermissionError:
        raise ToolExecutionError(
            f"没有权限访问目录 —— {dir_path}", tool_name="list_directory"
        )
    except Exception as e:
        raise ToolExecutionError(
            f"读取目录时发生异常 —— {dir_path}，详情: {e}", tool_name="list_directory"
        )

    if not entries:
        return f"目录为空 —— {dir_path}"

    result_parts = [f"目录 {dir_path} 的内容（共 {len(entries)} 项）:"]
    for entry in entries:
        try:
            if entry.is_dir():
                result_parts.append(f"  [目录]  {entry.name}/")
            elif entry.is_file():
                size = entry.stat().st_size
                result_parts.append(f"  [文件]  {entry.name}  ({_format_size(size)})")
            else:
                result_parts.append(f"  [其他]  {entry.name}")
        except OSError:
            result_parts.append(f"  [未知]  {entry.name}")

    return "\n".join(result_parts)


# ============================================================
# 3. search_in_file —— 在文件中搜索关键词
# ============================================================

def search_in_file(file_path: str, query: str) -> str:
    """
    在文件中搜索包含 query 的行，返回匹配行及其上下文（前后各 2 行）。

    异常:
        ToolInputError:     参数为空
        ToolExecutionError: 文件不存在或无法读取
    """
    if not file_path or not file_path.strip():
        raise ToolInputError("file_path 不能为空", tool_name="search_in_file")
    if not query or not query.strip():
        raise ToolInputError("query 不能为空", tool_name="search_in_file")

    path = workspace_path(_workspace_root, file_path, "search_in_file")

    if not path.exists():
        raise ToolExecutionError(f"文件不存在 —— {file_path}", tool_name="search_in_file")
    if not path.is_file():
        raise ToolExecutionError(f"路径不是文件 —— {file_path}", tool_name="search_in_file")

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except PermissionError:
        raise ToolExecutionError(f"没有权限读取文件 —— {file_path}", tool_name="search_in_file")
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="latin-1") as f:
                lines = f.readlines()
        except Exception as e:
            raise ToolExecutionError(
                f"文件编码无法识别 —— {file_path}，详情: {e}", tool_name="search_in_file"
            )
    except Exception as e:
        raise ToolExecutionError(
            f"读取文件时发生异常 —— {file_path}，详情: {e}", tool_name="search_in_file"
        )

    total_lines = len(lines)
    matched_indices = []

    for i, line in enumerate(lines):
        if query in line:
            matched_indices.append(i)

    if not matched_indices:
        return f"未在 {file_path} 中找到 \"{query}\""

    lines_to_show = set()
    context_radius = 2

    for idx in matched_indices:
        for offset in range(-context_radius, context_radius + 1):
            ctx_idx = idx + offset
            if 0 <= ctx_idx < total_lines:
                lines_to_show.add(ctx_idx)

    result_parts = [f'在 {file_path} 中搜索 "{query}"，找到 {len(matched_indices)} 处匹配:']
    result_parts.append("")

    sorted_indices = sorted(lines_to_show)
    prev_idx = -2

    for idx in sorted_indices:
        if idx - prev_idx > 1:
            result_parts.append("  " + "-" * 50)
        marker = ">>>" if idx in matched_indices else "   "
        result_parts.append(f"{marker} {idx + 1:>6}\t{lines[idx].rstrip()}")
        prev_idx = idx

    return "\n".join(result_parts)


def _search_paths(directory: Path):
    """Use Git ignore rules in repositories, with a plain-directory fallback."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
            cwd=directory, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        for name in dict.fromkeys(result.stdout.split(b"\0")):
            if name:
                yield directory / os.fsdecode(name)
        return

    ignored = {"node_modules", "venv", "__pycache__", "build", "dist", "target"}
    for base, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in ignored]
        for name in files:
            yield Path(base) / name


def search_workspace(query: str, dir_path: str = ".", limit: int = 30) -> str:
    """Find literal text across workspace files."""
    tool = "search_workspace"
    if not isinstance(query, str) or not query.strip() or "\n" in query or "\r" in query:
        raise ToolInputError("query 必须是一行非空文本。", tool_name=tool)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolInputError("limit 必须在 1 到 100 之间。", tool_name=tool)
    directory = workspace_path(_workspace_root, dir_path, tool)
    if not directory.is_dir():
        raise ToolInputError("搜索路径必须是目录。", tool_name=tool)

    matches = []
    for path in _search_paths(directory):
        try:
            resolved = workspace_path(_workspace_root, str(path), tool)
            if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size > 2_000_000:
                continue
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ToolSecurityError):
            continue
        if "\0" in content:
            continue
        name = resolved.relative_to(_workspace_root).as_posix()
        for number, line in enumerate(content.splitlines(), 1):
            offset = line.find(query)
            if offset == -1:
                continue
            if len(matches) == limit:
                return "\n".join(matches) + "\n（已达到匹配上限，可缩小搜索范围。）"
            start = max(0, offset - 60)
            excerpt = ("…" if start else "") + line[start:start + 400]
            matches.append(f"{name}:{number}: {excerpt}")
    return "\n".join(matches) if matches else f"未找到匹配: {query}"


def _edit_diff(path: Path, before: str, after: str) -> str:
    name = path.relative_to(_workspace_root).as_posix()
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{name}", f"b/{name}",
    )
    return "\n".join(
        line.rstrip("\r\n") if line.endswith(("\r", "\n")) else
        line + "\n\\ No newline at end of file"
        for line in lines
    )


def edit_file(file_path: str, old_text: str, new_text: str) -> str:
    """Replace one exact occurrence, preserving the file's existing line endings."""
    tool = "edit_file"
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        raise ToolInputError("old_text 必须非空，new_text 必须是字符串。", tool_name=tool)
    path = workspace_path(_workspace_root, file_path, tool)
    try:
        before = read_bytes(path)
        if before is None:
            raise FileNotFoundError(file_path)
        original = before.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ToolExecutionError(f"无法读取 UTF-8 文件: {error}", tool_name=tool) from error
    if "\0" in original or "\0" in new_text:
        raise ToolInputError("只能编辑 UTF-8 文本文件。", tool_name=tool)

    # read_file presents LF line breaks. Adapt multiline arguments to CRLF when needed.
    if "\r\n" in original and "\n" not in original.replace("\r\n", ""):
        old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
        new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    start = original.find(old_text)
    if start < 0:
        raise ToolInputError("原文没有匹配，请重新读取文件。", tool_name=tool)
    if original.find(old_text, start + 1) >= 0:
        raise ToolInputError("原文匹配多处，请提供更多上下文。", tool_name=tool)
    if old_text == new_text:
        return "内容相同，无需修改。"

    edited = original[:start] + new_text + original[start + len(old_text):]
    record_edit(_workspace_root, path, before, edited.encode("utf-8"), tool)
    diff = _edit_diff(path, original, edited)
    return f"已编辑 {path.relative_to(_workspace_root).as_posix()}\n{diff}".rstrip()


_BLOCKED_PROGRAMS = {
    "bash", "cmd", "command", "doas", "fish", "ksh", "powershell", "pwsh",
    "sh", "su", "sudo", "zsh", "rm", "rmdir", "del", "format", "shutdown",
}
_SHELL_OPERATORS = {"&", "&&", "|", "||", ";", "<", ">", ">>"}


def execute_command(
    command: str | None = None,
    *,
    argv: list[str] | None = None,
    cwd: str = ".",
    confirm: Callable[[list[str], Path], bool] | None = None,
) -> str:
    result = run_command(command, argv=argv, cwd=cwd, confirm=confirm)
    if result.status == "timeout":
        raise ToolTimeoutError(result.text, tool_name="execute_command")
    if result.status == "interrupted":
        raise KeyboardInterrupt
    return result.text


def run_command(
    command: str | None = None,
    *,
    argv: list[str] | None = None,
    cwd: str = ".",
    confirm: Callable[[list[str], Path], bool] | None = None,
    timeout_seconds: float | None = None,
    timeout_limit: float = 60,
    on_output: Callable[[str, str], None] | None = None,
) -> ToolResult:
    """Run one explicitly approved executable and report its status and output."""
    tool = "execute_command"
    if argv is None and isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError as error:
            raise ToolInputError(f"命令解析失败: {error}", tool_name=tool) from error
    if (
        not isinstance(argv, list) or not argv or
        any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0].strip()
    ):
        raise ToolInputError("argv 必须是非空的程序及参数数组。", tool_name=tool)
    timeout = timeout_limit if timeout_seconds is None else timeout_seconds
    if type(timeout) not in (int, float) or not 0 < timeout <= timeout_limit:
        raise ToolInputError(f"命令超时须大于 0 且不超过 {timeout_limit:g} 秒。", tool_name=tool)
    directory = workspace_path(_workspace_root, cwd, tool)
    if not directory.is_dir():
        raise ToolInputError("cwd 必须是工作区内的目录。", tool_name=tool)
    program = Path(argv[0]).name.casefold().removesuffix(".exe")
    if program in _BLOCKED_PROGRAMS or any(arg in _SHELL_OPERATORS for arg in argv[1:]):
        raise ToolSecurityError("请使用单条命令，禁止启动 Shell 或使用管道和重定向。", tool_name=tool)
    executable = argv[0]
    if Path(executable).parent != Path(".") or "/" in executable or chr(92) in executable:
        executable = str(workspace_path(_workspace_root, str(directory / executable), tool))
    elif program in {"python", "python3"} and "." not in executable:
        executable = sys.executable
    else:
        executable = shutil.which(executable) or executable
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise ToolSecurityError("Windows 下请直接调用可执行程序，例如 python -m pytest。", tool_name=tool)
    if confirm is None or not confirm(argv, directory):
        return ToolResult("命令未获授权，未执行。", status="denied")

    try:
        return run_process([executable, *argv[1:]], directory, timeout, on_output)
    except OSError as error:
        raise ToolExecutionError(f"无法执行命令: {error}", tool_name=tool) from error


def write_file(file_path: str, content: str, mode: str = "overwrite") -> str:
    """Create, overwrite or append to a workspace file."""
    tool = "write_file"
    if not isinstance(content, str) or mode not in ("overwrite", "append"):
        raise ToolInputError("content 必须是字符串，mode 必须为 overwrite 或 append。", tool_name=tool)
    path = workspace_path(_workspace_root, file_path, tool)
    try:
        before = read_bytes(path)
        data = content.encode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ToolExecutionError(f"无法准备写入: {error}", tool_name=tool) from error
    if mode == "append" and before is not None:
        data = before + data
    record_edit(_workspace_root, path, before, data, tool)
    return f"已写入 {path.relative_to(_workspace_root).as_posix()}（{len(data)} 字节）"


# ============================================================
# 6. ask_followup_question —— 向用户澄清问题
# ============================================================

def ask_followup_question(question: str, options: list[str] | None = None) -> str:
    """
    当模型不确定用户的意图时，主动向用户提问澄清。

    参数:
        question: 要问用户的问题
        options:  可选答案列表（最多 5 个），不传则让用户自由输入

    返回:
        用户的回答字符串

    异常:
        ToolInputError: question 为空
    """
    if not question or not question.strip():
        raise ToolInputError("question 不能为空", tool_name="ask_followup_question")

    question_text = Text()
    question_text.append("❓ ", style="bold cyan")
    question_text.append(question, style="white")

    console.print()
    console.print(Panel(question_text, border_style="cyan", title="Agent 需要确认"))

    if options and len(options) > 0:
        display_options = options[:5]

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("key", style="bold cyan", width=4)
        table.add_column("value", style="white")
        for i, opt in enumerate(display_options, 1):
            table.add_row(f"{i}.", opt)
        table.add_row("0.", "其他（自定义输入）")

        console.print(table)

        valid_choices = [str(i) for i in range(len(display_options) + 1)]
        choice = Prompt.ask(
            "  [bold cyan]请选择[/bold cyan]",
            choices=valid_choices,
            default="1",
        )

        idx = int(choice)
        if idx == 0:
            answer = Prompt.ask("  [bold cyan]请输入[/bold cyan]")
        else:
            answer = display_options[idx - 1]
    else:
        answer = Prompt.ask(f"  [bold cyan]请回答[/bold cyan]")

    console.print(f"  [dim]用户回答: {answer}[/dim]")
    return answer


# ============================================================
# 工具注册表 —— 把函数名映射到函数对象
# ============================================================

AVAILABLE_TOOLS: dict[str, Callable[..., str]] = {
    "read_file":              read_file,
    "list_directory":         list_directory,
    "search_in_file":         search_in_file,
    "search_workspace":       search_workspace,
    "edit_file":              edit_file,
    "execute_command":        execute_command,
    "write_file":             write_file,
    "ask_followup_question":  ask_followup_question,
}


# ============================================================
# 工具 JSON Schema 定义 —— 发送给 API 的工具描述
# ============================================================

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容，返回带行号的文本（类似 cat -n）。可以指定行号范围来只读取文件的一部分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要读取的文件路径（绝对路径或相对路径）",
                    },
                    "line_start": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始），留空则从第 1 行开始读取",
                    },
                    "line_end": {
                        "type": "integer",
                        "description": "结束行号（包含），留空则读到文件末尾",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出指定目录下的所有文件和子目录，标注是文件还是目录，并显示文件大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir_path": {
                        "type": "string",
                        "description": "要列出内容的目录路径（绝对路径或相对路径）",
                    },
                },
                "required": ["dir_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_file",
            "description": "在文件中搜索包含指定关键词的行，返回匹配行及其上下文（前后各 2 行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要搜索的文件路径",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（区分大小写）",
                    },
                },
                "required": ["file_path", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_workspace",
            "description": "在工作区目录递归搜索字面文本，返回路径、行号和匹配内容。Git 仓库中遵循忽略规则。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "dir_path": {"type": "string", "description": "搜索范围，默认仓库根目录"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "在现有 UTF-8 文件中替换唯一匹配的原文并返回 diff。修改已有代码时优先使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_text": {"type": "string", "description": "原文，保留缩进，必须恰好匹配一处"},
                    "new_text": {"type": "string", "description": "替换文本，可为空"},
                },
                "required": ["file_path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "执行一条获授权的命令并返回退出码、stdout 和 stderr。用于运行测试等验证操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "程序名与参数，例如 [\"python\", \"-m\", \"pytest\"]",
                    },
                    "cwd": {"type": "string", "description": "工作区内的目录，默认仓库根目录"},
                    "timeout_seconds": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": "可指定更短的超时，默认使用用户设置的命令超时上限。",
                    },
                    "verification": {
                        "type": "boolean",
                        "description": "用于验证任务结果的命令填 true，例如运行测试或构建。",
                    },
                },
                "required": ["argv"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "将内容写入文件。支持两种模式：'overwrite'（覆盖整个文件）和 'append'（追加到文件末尾）。"
                "写入前会检查路径并记录快照。修改已有代码时优先使用 edit_file。"
                "只能写入工作区内的文件，不能使用 ../ 跳出项目根目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要写入的文件路径（必须在当前工作区内）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件内容",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "写入模式：overwrite 覆盖整个文件，append 追加到末尾。默认为 overwrite。",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_followup_question",
            "description": (
                "当你对用户的需求不确定时，使用此工具向用户提问澄清。"
                "可以提供最多 5 个预设选项让用户选择，也可以让用户自由输入。"
                "重要：遇到模糊需求时优先使用此工具，不要自己猜测用户意图。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要问用户的问题",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选的答案列表（最多 5 个），不传则让用户自由输入",
                    },
                },
                "required": ["question"],
            },
        },
    },
]


def register_tool(
    name: str, handler: Callable[..., str], description: str, parameters: dict,
) -> None:
    """Expose a custom tool through both the dispatcher and the model schema."""
    if name in AVAILABLE_TOOLS:
        raise ValueError(f"工具已注册: {name}")
    AVAILABLE_TOOLS[name] = handler
    TOOLS.append({
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    })
