"""Nano Code terminal and non-interactive entry points."""

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from functools import partial
from datetime import datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from agent_core import EventHandler, ToolResult, run_turn
from changes import undo_last_edit
from context_budget import estimate_tokens
from exceptions import (
    ToolError, ToolInputError, ToolSecurityError, ToolTimeoutError, TurnPersistenceError,
)
from execution_store import pending_executions, record_execution, recover_execution
from model_api import ChatModel
from session_store import auto_save, list_saved_sessions, load_session, save_session
from tools import AVAILABLE_TOOLS, TOOLS, run_command, set_workspace_root
from workspace import find_root, read_project_instructions


console = Console()
error_console = Console(stderr=True)


def print_banner(model: ChatModel, root: Path, max_rounds: int, context_limit: int):
    """打印 Agent 启动时的欢迎面板。"""
    info_table = Table(show_header=False, box=None, padding=(0, 2))
    info_table.add_column("key", style="bold cyan", width=14)
    info_table.add_column("value", style="white")
    info_table.add_row("模型", model.name)
    info_table.add_row("地址", str(model.client.base_url))
    info_table.add_row("工作目录", str(root))
    info_table.add_row("工具数量", str(len(TOOLS)))
    info_table.add_row("最大工具轮数", str(max_rounds))
    info_table.add_row("上下文上限", f"{context_limit:,} tokens")
    info_table.add_row("流式输出", "开启" if model.streaming else "关闭")
    info_table.add_row("Temperature", str(model.temperature))
    info_table.add_row("Top-P", str(model.top_p))

    console.print()
    console.print(Panel(
        info_table,
        title="[bold white]🤖 Coding Agent[/bold white]",
        subtitle="[dim]输入 /exit 退出 | /clear 清空上下文 | /help 查看帮助[/dim]",
        border_style="cyan",
        padding=(1, 2),
    ))


def print_goodbye():
    """打印退出时的告别面板。"""
    console.print()
    console.print(Panel(
        "[dim]感谢使用，再见！[/dim]",
        title="[bold]👋 再见[/bold]",
        border_style="cyan",
    ))


def print_help():
    """打印帮助面板。"""
    help_text = """
[bold]可用命令:[/bold]

  /exit      退出程序（自动保存会话）
  /clear     清空对话上下文
  /help      显示此帮助信息
  /stats     查看当前会话统计
  /save      将当前会话保存为一个命名存档
  /load      加载一个命名存档（替换当前会话）
  /sessions  列出所有已保存的会话
  /undo      撤销最近一次内置文件修改
  /recover   查看未结束的轮次，或用 /recover <轮次 ID> 恢复

[bold]可用工具（由模型自动调用）:[/bold]

  read_file             读取文件内容，带行号
  list_directory        列出目录内容
  search_workspace      在仓库中搜索文本
  search_in_file        在文件中搜索关键词
  edit_file             局部修改现有文件并显示差异
  execute_command       执行获授权的命令并报告退出码
  write_file            创建、覆盖或追加文件
  ask_followup_question 向用户澄清不确定的问题

[bold]配置:[/bold]
  编辑 .env 文件来修改模型、API 地址等参数。
  详细说明见 README.md
"""
    console.print(Panel(help_text, title="[bold white]📖 帮助[/bold white]", border_style="cyan"))


def print_stats(messages: list[dict], start_time: float) -> None:
    """打印当前会话统计。"""
    elapsed = time.time() - start_time
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    est_tokens = estimate_tokens(messages)

    stats_table = Table(show_header=False, box=None, padding=(0, 2))
    stats_table.add_column("key", style="bold cyan", width=16)
    stats_table.add_column("value", style="white")
    stats_table.add_row("运行时长", f"{elapsed:.0f} 秒")
    stats_table.add_row("用户消息", str(len(user_msgs)))
    stats_table.add_row("助手消息", str(len(assistant_msgs)))
    stats_table.add_row("工具调用", str(len(tool_msgs)))
    stats_table.add_row("总消息数", str(len(messages)))
    stats_table.add_row("估算 token", f"~{est_tokens:,}")

    console.print()
    console.print(Panel(stats_table, title="[bold white]📊 会话统计[/bold white]", border_style="cyan"))


def print_sessions() -> None:
    """打印所有已保存的会话列表。"""
    sessions = list_saved_sessions()

    if not sessions:
        console.print("  [dim]没有已保存的会话[/dim]")
        return

    table = Table(title="已保存的会话", border_style="cyan")
    table.add_column("名称", style="bold cyan")
    table.add_column("消息数", justify="right")
    table.add_column("创建时间")
    table.add_column("更新时间")

    for s in sessions:
        table.add_row(
            s["name"].replace("__auto__", "(自动保存)"),
            str(s["message_count"]),
            s["created_at"],
            s["updated_at"],
        )

    console.print()
    console.print(table)


# ============================================================
# 3. 项目感知 —— 启动时自动检测项目信息
# ============================================================

def detect_project(root: Path) -> str:
    """
    自动检测当前项目的信息，返回一段描述字符串。

    检测内容：
      1. Git 仓库状态（分支、最近提交）
      2. 项目主要编程语言
      3. 项目根目录结构摘要
    """
    import subprocess as sp

    parts = []

    # --- 3.1 Git 仓库检测 ---
    try:
        git_dir = sp.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5, cwd=root
        )
        if git_dir.returncode == 0:
            branch = sp.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=5, cwd=root
            )
            branch_name = branch.stdout.strip() or "unknown"

            log = sp.run(
                ["git", "log", "--oneline", "-3", "--format=%s"],
                capture_output=True, text=True, timeout=5, cwd=root
            )
            commits = [c.strip() for c in log.stdout.strip().split("\n") if c.strip()]

            parts.append(f"Git 分支: {branch_name}")
            if commits:
                parts.append(f"最近提交: {' | '.join(commits[:3])}")
    except Exception:
        pass

    # --- 3.2 项目语言检测 ---
    try:
        ext_counter = Counter()
        flag_files = {
            "Cargo.toml": "Rust",
            "go.mod": "Go",
            "package.json": "JavaScript/TypeScript",
            "tsconfig.json": "TypeScript",
            "CMakeLists.txt": "C/C++ (CMake)",
            "Makefile": "C/C++ (Make)",
            "pom.xml": "Java (Maven)",
            "build.gradle": "Java (Gradle)",
            "Gemfile": "Ruby",
            "mix.exs": "Elixir",
            "Cargo.lock": "Rust",
        }

        detected_flags = []
        root_entries = list(root.iterdir())
        for entry in root_entries:
            if entry.name in flag_files:
                detected_flags.append(flag_files[entry.name])

            if entry.is_file():
                ext = entry.suffix.lower()
                if ext:
                    ext_counter[ext] += 1

        for entry in root_entries:
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in (
                "__pycache__", "node_modules", "target", "venv", ".venv", "dist", "build"
            ):
                try:
                    for sub_entry in entry.iterdir():
                        if sub_entry.is_file():
                            ext = sub_entry.suffix.lower()
                            if ext:
                                ext_counter[ext] += 1
                except PermissionError:
                    pass

        lang_map = {
            ".py": "Python",
            ".js": "JavaScript",
            ".ts": "TypeScript",
            ".tsx": "TypeScript (React)",
            ".jsx": "JavaScript (React)",
            ".rs": "Rust",
            ".go": "Go",
            ".java": "Java",
            ".c": "C",
            ".cpp": "C++",
            ".h": "C/C++ Header",
            ".rb": "Ruby",
            ".ex": "Elixir",
            ".exs": "Elixir",
            ".swift": "Swift",
            ".kt": "Kotlin",
            ".scala": "Scala",
            ".vue": "Vue.js",
            ".svelte": "Svelte",
            ".css": "CSS",
            ".html": "HTML",
            ".md": "Markdown",
            ".json": "JSON",
            ".yaml": "YAML",
            ".yml": "YAML",
            ".toml": "TOML",
        }

        if detected_flags:
            parts.append(f"项目类型: {', '.join(detected_flags)}")
        elif ext_counter:
            top_ext = ext_counter.most_common(3)
            top_langs = []
            for ext, count in top_ext:
                lang = lang_map.get(ext, ext)
                top_langs.append(f"{lang}({count})")
            parts.append(f"项目语言: {', '.join(top_langs)}")
    except Exception:
        pass

    # --- 3.3 项目结构摘要 ---
    try:
        noise_dirs = {
            ".git", "__pycache__", "node_modules", "target",
            "venv", ".venv", "dist", "build", ".idea", ".vscode",
            ".claude", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        }
        structure_lines = []
        root_entries = sorted(
            [e for e in root.iterdir() if e.name not in noise_dirs],
            key=lambda e: (not e.is_dir(), e.name.lower())
        )
        for entry in root_entries[:30]:
            if entry.is_dir():
                structure_lines.append(f"  📁 {entry.name}/")
            else:
                size = entry.stat().st_size
                structure_lines.append(f"  📄 {entry.name} ({_format_size_simple(size)})")

        if structure_lines:
            parts.append(f"项目结构:\n" + "\n".join(structure_lines))
    except Exception:
        pass

    if not parts:
        return "（未检测到特殊项目信息）"

    return "\n".join(parts)


def _format_size_simple(size_bytes: int) -> str:
    """简易版文件大小格式化（用于项目结构展示）。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes}{unit}" if unit == "B" else f"{size_bytes:.0f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}GB"


# ============================================================
# 4. 系统提示词 —— 动态拼接项目信息
# ============================================================

def build_system_prompt(root: Path, directory: Path) -> str:
    instructions = read_project_instructions(root, directory)
    prompt = f"""你是 Nano Code，一个在终端中协助开发的编程助手。

- 仓库根目录: {root}
- 启动目录: {directory}
- 操作系统: {sys.platform}
- 日期: {datetime.now().date().isoformat()}

{detect_project(root)}

先收集上下文，再修改，再验证。使用 search_workspace 定位代码，使用 read_file 检查原文。
修改已有代码优先使用 edit_file，创建文件可用 write_file。文件工具的相对路径以仓库根目录为基准。
需要运行测试或构建来验证结果时，使用 execute_command 的 argv 数组并设置 verification=true，一次提交一条命令。
命令被拒绝时不要通过其他命令绕过。依据实际退出码和输出汇报验证结果。
若会话记录标记了上轮结果待核查的操作，先核查工作区和命令效果，再决定是否需要重试。
需求不明确时可使用 ask_followup_question。优先给出简洁、可核查的结论。"""
    if instructions:
        prompt += (
            "\n\n## 项目工作约定\n以下是仓库中的说明。按用户任务采用相关约定，权限由工具层控制。\n\n"
            + instructions
        )
    return prompt


def execute_tool_call(
    call: dict, emit: EventHandler | None = None, *,
    confirm=None, interactive: bool = False, command_timeout: float = 60,
) -> ToolResult:
    name = call["function"]["name"]
    if name not in AVAILABLE_TOOLS:
        return ToolResult(f"错误：未知工具 {name}。", status="error")
    if name == "ask_followup_question" and not interactive:
        return ToolResult(
            "当前为非交互模式，请根据已有信息作答，或在回复中提出需要澄清的问题。",
            status="denied",
        )
    is_verification = False
    try:
        arguments = json.loads(call["function"]["arguments"])
        if not isinstance(arguments, dict):
            raise ToolInputError("工具参数必须是 JSON 对象。")
        if name == "execute_command":
            flag = arguments.pop("verification", False)
            if not isinstance(flag, bool):
                raise ToolInputError("verification 必须为布尔值。")
            is_verification = flag
            def on_output(stream: str, text: str) -> None:
                emit({"type": "tool_output", "stream": stream, "text": text})

            result = run_command(
                **arguments, confirm=confirm, timeout_limit=command_timeout,
                on_output=on_output if emit else None,
            )
        else:
            result = ToolResult(
                AVAILABLE_TOOLS[name](**arguments),
                invalidates_verification=name in {"edit_file", "write_file"},
            )
    except KeyboardInterrupt:
        result = ToolResult("用户中断了工具调用，请先检查工作区再决定是否重试。", status="interrupted")
    except (ToolInputError, ValueError, TypeError) as error:
        result = ToolResult(f"工具错误：{error}", status="invalid_arguments")
    except ToolSecurityError as error:
        result = ToolResult(f"工具错误：{error}", status="blocked")
    except ToolTimeoutError as error:
        result = ToolResult(f"工具错误：{error}", status="timeout")
    except ToolError as error:
        result = ToolResult(f"工具错误：{error}", status="error")
    except Exception as error:
        result = ToolResult(f"工具执行异常：{error}", status="error")
    if is_verification:
        if result.exit_code is not None:
            verification = "passed" if result.exit_code == 0 else "failed"
        elif result.status in {"denied", "blocked", "invalid_arguments"}:
            verification = "not_run"
        else:
            verification = "inconclusive"
        result = replace(result, verification=verification)
    if len(result.text) > 8000:
        result = replace(result, text=result.text[:8000] + "\n（输出已截断。）", truncated=True)
    return result


def _terminal_events():
    streaming_text = False
    streamed_command = False

    def emit(event: dict) -> None:
        nonlocal streaming_text, streamed_command
        kind = event["type"]
        if kind == "text_delta":
            console.print(Text(event["text"]), end="")
            sys.stdout.flush()
            streaming_text = True
            return
        if kind == "tool_output":
            text = event["text"]
            style = "yellow" if event["stream"] == "stderr" else ""
            console.print(Text(text, style), end="", soft_wrap=True)
            sys.stdout.flush()
            streaming_text = not text.endswith(("\n", "\r"))
            streamed_command = True
            return
        if streaming_text:
            console.print()
            streaming_text = False

        if kind == "model_start":
            console.print(f"  [dim]思考中（第 {event['round']} 轮）[/dim]")
        elif kind == "tool_request":
            streamed_command = False
            function = event["call"]["function"]
            console.print(Text(f"  🔧 {function['name']} {function['arguments'][:160]}", "cyan"))
        elif kind == "tool_result":
            function = event["call"]["function"]
            result = event["result"]
            if function["name"] in {"edit_file", "execute_command"} or event["outcome"]["status"] != "ok":
                console.print(Text(result.split("\n", 1)[0] if streamed_command else result))
            else:
                console.print(f"  [dim]返回 {len(result)} 字符[/dim]")
        elif kind == "usage":
            console.print(f"  [dim]用量 {event['total_tokens']} tokens[/dim]")
        elif kind in {"error", "notice", "recovery_error"}:
            console.print(Text(event["text"], "yellow" if kind == "notice" else "red"))
        elif kind == "recovery_pending":
            _print_pending_execution(event, console)
        elif kind == "turn_end":
            if event["status"] == "interrupted":
                console.print("  [yellow]本轮已中断，可以继续输入。[/yellow]")
            verification = event["verification"]
            missing = "最近一次验证未运行" if verification["tool_call_id"] else "本轮未运行验证"
            label, style = {
                "passed": ("最近一次验证通过", "green"),
                "failed": ("最近一次验证失败", "red"),
                "not_run": (missing, "dim"),
                "inconclusive": ("最近一次验证没有得到结果", "yellow"),
                "stale": ("验证后又调用了文件编辑，请重新验证", "yellow"),
            }[verification["status"]]
            console.print(Text(f"  {label}", style))

    return emit


def _print_pending_execution(event: dict, output: Console) -> None:
    state = "所属进程可能仍在运行" if event["active"] else "可在交互模式用 /recover 恢复"
    output.print(Text(f"未结束轮次 {event['turn_id']}（{state}）", "yellow"))
    for step in event["tools"]:
        label = {
            "unknown": "结果待核查", "not_started": "未开始",
            "ok": "已有结果", "failed": "执行失败",
        }.get(step["status"], step["status"])
        detail = step["name"]
        if "arguments" in step:
            detail += " " + step["arguments"][:240]
        if step["exit_code"] is not None:
            detail += f"（退出码 {step['exit_code']}）"
        output.print(Text(f"  {label} · {detail}"))


def _report_pending_executions(emit: EventHandler, *, show_arguments: bool = False) -> bool:
    try:
        pending = pending_executions()
    except TurnPersistenceError as error:
        emit({"type": "recovery_error", "text": str(error)})
        return True
    for execution in pending:
        steps = []
        for step in execution["tools"]:
            call = step["call"]
            summary = {
                "name": call["function"]["name"], "tool_call_id": call["id"],
                "status": step["status"], "exit_code": step["exit_code"],
            }
            if show_arguments:
                summary["arguments"] = call["function"]["arguments"]
            steps.append(summary)
        emit({"type": "recovery_pending", **execution, "tools": steps})
    return bool(pending)


def _interactive_dispatch(command_timeout: float):
    approvals = {}

    def confirm(argv: list[str], cwd: Path) -> bool:
        key = cwd, tuple(argv)
        if key not in approvals:
            console.print(Text(f"工作目录 {cwd}\n命令 {json.dumps(argv, ensure_ascii=False)}", "yellow"))
            answer = Prompt.ask("执行此命令？", choices=["y", "n"], default="n")
            approvals[key] = answer == "y"
        return approvals[key]

    return partial(execute_tool_call, confirm=confirm, interactive=True, command_timeout=command_timeout)


def _run_interactive(
    messages: list[dict], model: ChatModel, root: Path, max_rounds: int, context_limit: int,
    command_timeout: float,
) -> int:
    print_banner(model, root, max_rounds, context_limit)
    system = messages[0]
    _report_pending_executions(_terminal_events(), show_arguments=True)
    saved = load_session("__auto__")
    if saved and Prompt.ask("恢复上次会话？", choices=["y", "n"], default="y") == "y":
        messages.extend(saved[1:] if saved[0]["role"] == "system" else saved)

    session_start = time.time()
    session_id = uuid4().hex
    while True:
        try:
            user_input = Prompt.ask("\n[bold green]▸[/bold green] 你").strip()
        except (EOFError, KeyboardInterrupt):
            user_input = "/exit"
        if not user_input:
            continue
        command, _, name = user_input.partition(" ")
        command = command.lower()
        if command == "/exit":
            auto_save(messages)
            print_goodbye()
            return 0
        if command == "/clear":
            messages[:] = [system]
            session_id = uuid4().hex
            auto_save(messages)
            console.print("上下文已清空。")
        elif command == "/help":
            print_help()
        elif command == "/stats":
            print_stats(messages, session_start)
        elif command == "/sessions":
            print_sessions()
        elif command == "/recover":
            turn_id = name.strip()
            if not turn_id:
                if not _report_pending_executions(_terminal_events(), show_arguments=True):
                    console.print("没有待恢复的轮次。")
                continue
            try:
                recovered = recover_execution(turn_id)
            except TurnPersistenceError as error:
                console.print(Text(str(error), "red"))
                continue
            if recovered is None:
                console.print("找不到待恢复的轮次。")
                continue
            messages[:] = [system, *(recovered[1:] if recovered[0]["role"] == "system" else recovered)]
            session_id = uuid4().hex
            console.print("已恢复对话。请先核查结果不确定的操作，再输入下一条指令。")
        elif command == "/undo":
            try:
                console.print(Text(undo_last_edit(root)))
            except ToolError as error:
                console.print(Text(str(error), "red"))
        elif command == "/save":
            name = name.strip()
            if not name or name == "__auto__":
                console.print("用法：/save <会话名称>")
            elif save_session(name, messages):
                console.print(Text(f"已保存会话 {name}。"))
        elif command == "/load":
            name = name.strip()
            loaded = load_session(name)
            if loaded:
                messages[:] = [system, *(loaded[1:] if loaded[0]["role"] == "system" else loaded)]
                session_id = uuid4().hex
                auto_save(messages)
                console.print(Text(f"已加载会话 {name}。"))
            else:
                console.print(Text(f"找不到会话 {name}。"))
        else:
            emit = _terminal_events()
            run_turn(
                messages, user_input, request_model=model,
                dispatch_with_events=_interactive_dispatch(command_timeout),
                emit=emit, max_tool_rounds=max_rounds, save=auto_save,
                session_id=session_id, checkpoint=record_execution,
            )


def _parse_allowed_command(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("命令必须是 JSON 字符串数组。") from error
    if not isinstance(value, list) or not value or not all(isinstance(arg, str) for arg in value):
        raise argparse.ArgumentTypeError("命令必须是非空的 JSON 字符串数组。")
    return tuple(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nano Code 终端编程助手")
    parser.add_argument("-p", "--print", dest="prompt", help="执行一次任务并退出")
    parser.add_argument("--json", action="store_true", help="将进度作为 JSON Lines 写入标准输出")
    parser.add_argument("--env-file", type=Path, help="显式指定 .env 配置文件")
    parser.add_argument("--allow-command", type=_parse_allowed_command, action="append", default=[],
                        help="非交互模式下授权仓库根目录中的完整 argv，可重复指定")
    args = parser.parse_args(argv)
    if args.prompt is None and (args.json or args.allow_command):
        parser.error("--json 和 --allow-command 需要与 --print 一起使用。")
    if args.prompt is not None and not args.prompt.strip():
        parser.error("--print 需要非空任务。")

    if args.env_file and not args.env_file.is_file():
        parser.error("--env-file 指定的文件不存在。")
    load_dotenv(args.env_file or Path(__file__).with_name(".env"), override=False)
    key = os.getenv("API_KEY")
    if not key:
        error_console.print("请先设置 API_KEY，或用 --env-file 指定配置。", style="red")
        return 2

    try:
        max_rounds = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
        context_limit = int(os.getenv("CONTEXT_LIMIT_TOKENS", "80000"))
        attempts = int(os.getenv("MAX_RETRIES", "3"))
        retry_delay = float(os.getenv("RETRY_BASE_DELAY", "1"))
        command_timeout = float(os.getenv("COMMAND_TIMEOUT_SECONDS", "60"))
        configured_requests = os.getenv("MAX_MODEL_REQUESTS")
        request_limit = int(configured_requests) if configured_requests else None
        if max_rounds < 0 or context_limit <= 0 or attempts <= 0 or retry_delay < 0:
            raise ValueError("轮数、上下文上限和重试配置超出范围。")
        if not isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("COMMAND_TIMEOUT_SECONDS 必须为大于 0 的有限数值。")
        if request_limit is not None and request_limit <= 0:
            raise ValueError("MAX_MODEL_REQUESTS 必须为正整数。")
        base_url = os.getenv("BASE_URL", "https://api.deepseek.com")
        official_deepseek = urlsplit(base_url).hostname == "api.deepseek.com"
        model = ChatModel(
            OpenAI(
                api_key=key, base_url=base_url,
                max_retries=0, timeout=60,
            ),
            name=os.getenv("MODEL_NAME", "deepseek-flash" if official_deepseek else "deepseek-chat"),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
            top_p=float(os.getenv("TOP_P", "1.0")),
            streaming=os.getenv("STREAMING_ENABLED", "true").lower() in ("true", "1", "yes"),
            max_attempts=attempts, retry_delay=retry_delay, tools=TOOLS, context_limit=context_limit,
            request_limit=request_limit, disable_thinking=official_deepseek,
        )
    except ValueError as error:
        error_console.print(Text(f"配置错误：{error}", "red"))
        return 2

    directory = Path.cwd().resolve()
    root = find_root(directory)
    set_workspace_root(root)
    messages = [{"role": "system", "content": build_system_prompt(root, directory)}]
    if args.prompt is None:
        return _run_interactive(messages, model, root, max_rounds, context_limit, command_timeout)

    def emit(event: dict) -> None:
        if args.json:
            print(json.dumps(event), flush=True)
        elif event["type"] == "reply":
            console.print(Text(event["text"]))
        elif event["type"] == "tool_output":
            error_console.print(Text(event["text"]), end="", soft_wrap=True)
            sys.stderr.flush()
        elif event["type"] in {"error", "recovery_error"}:
            error_console.print(Text(event["text"], "red"))
        elif event["type"] == "recovery_pending":
            _print_pending_execution(event, error_console)

    allowed = set(args.allow_command)

    def confirm(command: list[str], cwd: Path) -> bool:
        return cwd == root and tuple(command) in allowed

    _report_pending_executions(emit)
    status = run_turn(
        messages, args.prompt, request_model=model,
        dispatch_with_events=partial(execute_tool_call, confirm=confirm, command_timeout=command_timeout),
        emit=emit, max_tool_rounds=max_rounds,
        checkpoint=record_execution,
    )
    return {"completed": 0, "error": 1, "interrupted": 130}[status]


def run_agent() -> int:
    return main([])


if __name__ == "__main__":
    raise SystemExit(main())
