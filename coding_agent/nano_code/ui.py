"""An inline terminal UI: editable prompt, queued turns and compact tool activity."""

import asyncio
import concurrent.futures
import json
import re
import sys
import time
from collections import deque

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, run_in_terminal
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from context_budget import estimate_tokens
from . import __version__

ACCENT = "#d68c6e"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
COMMANDS = {
    "/help": "命令与快捷键", "/clear": "保存当前会话并开始新会话", "/exit": "保存并退出",
    "/status": "项目、模型与运行状态", "/diff": "审阅 Git 差异（--staged 查看暂存）",
    "/output": "展开上一轮工具输出", "/sessions": "列出会话", "/resume": "按名称或 ID 恢复会话",
    "/rename": "重命名当前会话", "/fork": "分支当前对话", "/export": "导出 Markdown 或 JSON",
    "/undo": "撤销当前会话最近一次内置编辑", "/rewind": "列出文件检查点",
    "/checkpoints": "列出或预览检查点", "/restore": "恢复指定轮次的文件",
    "/recover": "查看或恢复异常退出的轮次", "/model": "查看或切换模型",
    "/permissions": "查看或切换权限模式", "/plan": "切换只读计划模式",
    "/context": "查看上下文占用", "/compact": "生成摘要并保留原始存档",
    "/cost": "查看实际 token 用量", "/tasks": "查看任务列表", "/skills": "查看可用 Skills",
    "/agents": "查看只读子代理", "/mcp": "查看、连接、断开 MCP", "/hooks": "查看已启用 hooks",
    "/memory": "查看或追加项目记忆", "/init": "为当前项目创建 AGENTS.md 模板",
    "/doctor": "检查配置和运行环境", "/cancel": "取消当前任务", "/queue": "查看待执行消息",
    "/clear-queue": "清空待执行消息",
    "/theme": "切换 dark / light 主题", "/trust": "审阅并信任项目扩展配置",
}


def safe_text(value):
    value = ANSI.sub("", str(value))
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


class InputCompleter(Completer):
    def __init__(self, root, skills):
        self.root, self.skills = root, skills
        self.paths = PathCompleter(get_paths=lambda: [str(root)], expanduser=False)

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        if before.startswith("/") and " " not in before:
            for name, detail in {**COMMANDS, **{"/" + s.name: s.description for s in self.skills.values()}}.items():
                if name.startswith(before):
                    yield Completion(name, start_position=-len(before), display_meta=detail)
        else:
            word = document.get_word_before_cursor(WORD=True)
            if word.startswith("@"):
                from prompt_toolkit.document import Document
                for item in self.paths.get_completions(Document(word[1:]), complete_event):
                    yield item


class Renderer:
    def __init__(self, console=None):
        self.console = console or Console(highlight=False)
        self.expanded = False
        self.streaming = False
        self.output = []
        self.state = "ready"
        self.started = time.monotonic()

    def welcome(self, session):
        left = Group(Text("◈  Nano Code", style=f"bold {ACCENT}"),
                     Text(f"v{__version__} · local coding workspace", style="dim"), Text(""),
                     Text("把想做的事交给终端。", style="bold"),
                     Text(safe_text(session.config.data["model"])),
                     Text(safe_text(str(session.root)), style="dim", overflow="fold"))
        right = Group(Text("开始工作", style=f"bold {ACCENT}"),
                      Text("描述任务，或输入 / 查看命令"), Text("@ 引用文件 · /diff 审阅修改"), Text(""),
                      Text("最近会话", style=f"bold {ACCENT}"),
                      Text("/resume 继续上次工作"), Text("/doctor 检查配置", style="dim"))
        grid = Table.grid(expand=True, padding=(0, 3))
        grid.add_column(ratio=3)
        if self.console.width >= 85:
            grid.add_column(ratio=2)
            grid.add_row(left, right)
        else:
            grid.add_row(left)
        self.console.print(Panel(grid, border_style=ACCENT, box=box.ROUNDED, padding=(1, 2), title="[dim]NANO / TERMINAL[/dim]", title_align="left"))
        self.console.print(Text("  Enter 发送 · Alt+Enter 换行 · Shift+Tab 切换模式 · Ctrl+C 取消 · Ctrl+O 展开", style="dim"))
        self.console.print()

    def event(self, event):
        kind = event["type"]
        if kind == "text_delta":
            if not self.streaming:
                self.console.print(Text("● ", style=ACCENT), end="")
            self.console.print(Text(safe_text(event["text"])), end="", soft_wrap=True)
            self.streaming = True
            self.state = "responding"
            return
        if self.streaming:
            self.console.print()
            self.streaming = False
        if kind == "turn_start":
            self.started = time.monotonic()
            self.output = []
        elif kind == "model_start":
            self.state = "thinking"
        elif kind == "tool_request":
            f = event["call"]["function"]
            try:
                arguments = json.loads(f["arguments"])
                detail = arguments.get("file_path") or arguments.get("query") or arguments.get("argv") or arguments.get("name") or ""
            except ValueError:
                detail = ""
            self.console.print(Text(f"● {f['name']}  {safe_text(detail)[:180]}", style=ACCENT))
            self.state = f["name"]
        elif kind == "tool_result":
            self.output.append(event)
            content = safe_text(event["result"])
            status = event["outcome"]["status"]
            if self.expanded:
                self.console.print(Text(content, style="dim" if status == "ok" else "yellow"))
            else:
                lines = content.splitlines()
                self.console.print(Text("  ⎿  " + (lines[0][:160] if lines else status), style="dim" if status == "ok" else "yellow"))
                if len(lines) > 1:
                    self.console.print(Text(f"     +{len(lines)-1} lines · /output 展开", style="dim"))
        elif kind == "tool_output" and self.expanded:
            self.console.print(Text(safe_text(event["text"])), end="", soft_wrap=True)
        elif kind in {"error", "notice", "recovery_error"}:
            self.console.print(Text("  " + safe_text(event["text"]), style="yellow" if kind == "notice" else "red"))
        elif kind == "subagent_start":
            self.console.print(Text(f"● {event['agent']}  ·  read-only subagent", style=ACCENT))
        elif kind == "turn_end":
            self.state = "ready"
            state = event["verification"]["status"]
            label = {"passed": "验证通过", "failed": "验证失败", "not_run": "未运行验证", "stale": "修改后需要重新验证", "inconclusive": "验证结果不确定"}.get(state, state)
            files = len(event.get("changes", {}).get("files", []))
            self.console.print(Text(f"  {event['status']} · {time.monotonic()-self.started:.1f}s · {files} 个文件 · {label}", style="green" if state == "passed" else "dim"))
            self.console.print()

    def show_output(self):
        for event in self.output:
            name = event["call"]["function"]["name"]
            self.console.print(Rule(name, style=ACCENT))
            text = safe_text(event["result"])
            self.console.print(Syntax(text, "diff", theme="ansi_dark", word_wrap=True) if name == "edit_file" else Text(text))
        if not self.output:
            self.console.print("暂无工具输出。")


async def interactive(session, initial=None, *, terminal_input=None, terminal_output=None):
    from .commands import execute_command

    renderer = Renderer()
    renderer.welcome(session)
    events = asyncio.Queue()
    prompts = asyncio.Queue()
    pending = deque()
    loop = asyncio.get_running_loop()
    closing = False
    command_running = False

    def emit(event):
        loop.call_soon_threadsafe(events.put_nowait, event)

    def ask(tool, arguments):
        future = concurrent.futures.Future()
        emit({"type": "approval", "tool": tool, "arguments": arguments, "future": future})
        while True:
            if session.cancel_event.is_set():
                future.cancel()
                return False
            try:
                return future.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                continue

    def question(text, options):
        future = concurrent.futures.Future()
        emit({"type": "question", "text": text, "options": options, "future": future})
        while not session.cancel_event.is_set():
            try:
                return future.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                pass
        future.cancel()
        return "用户取消了问题。"

    session.events, session.permissions.ask, session.question = emit, ask, question
    bindings = KeyBindings()

    @bindings.add("enter")
    def send(event):
        if event.current_buffer.text.endswith("\\"):
            event.current_buffer.delete_before_cursor(1)
            event.current_buffer.insert_text("\n")
        else:
            event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @bindings.add("s-tab")
    def change_mode(event):
        if session.busy:
            return
        choices = ["default", "acceptEdits", "plan"]
        current = session.permissions.mode
        session.permissions.mode = choices[(choices.index(current) + 1) % len(choices)] if current in choices else "default"

    @bindings.add("c-o")
    def expand(event):
        renderer.expanded = not renderer.expanded

    @bindings.add("c-c")
    def interrupt(event):
        if session.busy:
            session.cancel()
        event.current_buffer.reset()
        event.app.exit(exception=KeyboardInterrupt())

    def toolbar():
        mode = session.permissions.mode
        state = "等待授权 / 回答" if pending else renderer.state
        context = estimate_tokens(session.messages)
        return [("class:toolbar", f" {mode}  │  {state}  │  {session.uid[:8]}  │  ~{context:,}/{session.config.data['context_limit']:,} tokens  │  queue {prompts.qsize()} ")]

    try:
        session.config.home.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(session.config.home / "history"))
    except OSError:
        history = InMemoryHistory()
    def theme():
        light = session.config.data["theme"] == "light"
        return Style.from_dict({"prompt": "#a84d37 bold" if light else "#d68c6e bold",
            "toolbar": "bg:#f3e9e2 #403128" if light else "bg:#262626 #bdbdbd",
            "completion-menu.completion.current": "bg:#d68c6e #161616"})

    from prompt_toolkit.styles import DynamicStyle
    prompt = PromptSession(
        history=history, completer=InputCompleter(session.root, session.skills),
        complete_while_typing=True, key_bindings=bindings, multiline=True,
        prompt_continuation="  ", bottom_toolbar=toolbar,
        style=DynamicStyle(theme),
        refresh_interval=0.25,
        reserve_space_for_menu=3,
        input=terminal_input, output=terminal_output,
    )

    async def consume_events():
        while True:
            event = await events.get()
            try:
                if event["type"] in {"approval", "question"}:
                    if event["future"].cancelled():
                        continue
                    pending.append(event)
                    def display():
                        if event["type"] == "approval":
                            # Never print configured MCP credentials in a permission prompt.
                            arguments = dict(event["arguments"])
                            if "configuration" in arguments:
                                arguments["configuration"] = "见已信任的 MCP 配置"
                            text = safe_text(json.dumps(arguments, ensure_ascii=False, indent=2))[:1800]
                            renderer.console.print(Panel(Text(text), title=safe_text(event["tool"]), border_style=ACCENT))
                            renderer.console.print("  y 允许一次 · s 本会话允许相同操作 · n 拒绝")
                        else:
                            renderer.console.print(Text(safe_text(event["text"]), style="bold"))
                            for index, option in enumerate(event.get("options") or [], 1):
                                renderer.console.print(Text(f"  {index}. {safe_text(option)}"))
                    await run_in_terminal(display)
                else:
                    await run_in_terminal(lambda: renderer.event(event))
            except Exception as error:
                if event.get("future") and not event["future"].done():
                    event["future"].set_result(False if event["type"] == "approval" else "无法展示问题。")
                renderer.console.print(Text(str(error), style="red"))
            finally:
                events.task_done()

    async def run_prompts():
        while True:
            text = await prompts.get()
            try:
                await run_in_terminal(lambda: renderer.console.print(Text("❯ " + safe_text(text), style="bold")))
                from .commands import expand_references
                await asyncio.to_thread(lambda: session.run(expand_references(session, text)))
            except (Exception, KeyboardInterrupt) as error:
                await run_in_terminal(lambda error=error: renderer.console.print(Text(str(error) or "已取消。", style="red")))
            finally:
                renderer.state = "ready"
                prompts.task_done()

    consumer = asyncio.create_task(consume_events())
    worker = asyncio.create_task(run_prompts())
    if initial:
        prompts.put_nowait(initial)
    try:
        with create_app_session(input=terminal_input, output=terminal_output), patch_stdout(raw=True):
            while not closing:
                try:
                    text = (await prompt.prompt_async([("class:prompt", "❯ ")])).strip()
                except KeyboardInterrupt:
                    pending_copy = list(pending)
                    pending.clear()
                    for item in pending_copy:
                        if not item["future"].done():
                            item["future"].set_result(False if item["type"] == "approval" else "用户取消。")
                    continue
                except EOFError:
                    closing = True
                    break
                while pending and pending[0]["future"].done():
                    pending.popleft()
                if not text:
                    continue
                if pending:
                    item = pending[0]
                    if item["type"] == "approval":
                        if text.lower() not in {"y", "yes", "s", "n", "no"}:
                            renderer.console.print("请输入 y、s 或 n。")
                            continue
                        answer = {"y": "yes", "yes": "yes", "s": "session", "n": False, "no": False}[text.lower()]
                    else:
                        options = item.get("options") or []
                        answer = options[int(text)-1] if text.isdigit() and 1 <= int(text) <= len(options) else text
                    pending.popleft()["future"].set_result(answer)
                    continue
                if text in {"/exit", "/quit"}:
                    closing = True
                    break
                if text == "/cancel":
                    session.cancel()
                    continue
                if text == "/queue":
                    renderer.console.print(f"待执行消息：{prompts.qsize()}。/clear-queue 清除待执行消息。")
                    continue
                if text == "/clear-queue":
                    while not prompts.empty():
                        prompts.get_nowait()
                        prompts.task_done()
                    continue
                if text.startswith("/"):
                    try:
                        # Commands run in a thread so permission prompts remain responsive.
                        if command_running:
                            renderer.console.print("上一条命令仍在执行。")
                            continue
                        async def run_command(command):
                            nonlocal command_running
                            command_running = True
                            try:
                                reply = await asyncio.to_thread(execute_command, session, command, renderer)
                                if isinstance(reply, str) and reply:
                                    prompts.put_nowait(reply)
                            except (Exception, KeyboardInterrupt) as error:
                                renderer.console.print(Text(str(error) or "已取消。", style="red"))
                            finally:
                                command_running = False
                        command_task = asyncio.create_task(run_command(text))
                        await asyncio.sleep(0)
                    except Exception as error:
                        renderer.console.print(Text(str(error), style="red"))
                else:
                    if command_running:
                        renderer.console.print("命令处理中，请稍候。")
                        continue
                    prompts.put_nowait(text)
    finally:
        session.cancel()
        while not prompts.empty():
            prompts.get_nowait()
            prompts.task_done()
        for item in pending:
            if not item["future"].done():
                item["future"].set_result(False if item["type"] == "approval" else "用户退出。")
        if command_running:
            await command_task
        await prompts.join()
        await events.join()
        worker.cancel()
        consumer.cancel()
        await asyncio.gather(worker, consumer, return_exceptions=True)
        if len(session.messages) > 1:
            renderer.console.print(Text(f"会话 · nano-code --resume {session.uid}", style="dim"))
    return 0
