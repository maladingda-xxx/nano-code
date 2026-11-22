"""An inline terminal UI: editable prompt, queued turns and compact tool activity."""

import asyncio
import concurrent.futures
import json
import re
import sys
import time
from collections import deque

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, run_in_terminal, set_app
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.filters import Condition, is_done
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, FloatContainer, FormattedTextControl, HSplit, ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu, CompletionsMenuControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import MODES, MODE_CYCLE
from .diff_view import file_title, files_text, parse_diff, totals
from .effort_picker import pick_effort
from .login_picker import configure_provider
from .mascot import mascot
from .model_picker import effort_choices, pick_model
from .python_lsp import diagnostic_line
from .reasoning import CATALOG_EFFORT_PROVIDERS
from .work_status import WorkStatus

ACCENT = "#5b7cfa"
ACCENT_STRONG = "#3b5bdb"
ACCENT_LIGHT = "#8aa4ff"
ACCENT_DARK = "#1e3a8a"
ACCENT_SOFT = "#a5b4fc"
MUTED = "#8494b4"
SUCCESS = "#2f9e82"
WARNING = "#c98a2e"
ERROR = "#d05c68"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
INLINE_DIFF_LINES = 12
COMMANDS = {
    "/help": "命令与快捷键", "/clear": "保存当前会话并开始新会话", "/exit": "保存并退出",
    "/status": "项目、模型与运行状态", "/diff": "审阅 Git 差异（--staged 查看暂存）",
    "/output": "展开上一轮工具输出", "/sessions": "列出会话", "/resume": "按名称或 ID 恢复会话",
    "/rename": "重命名当前会话", "/fork": "分支当前对话", "/export": "导出 Markdown 或 JSON",
    "/undo": "撤销当前会话最近一次内置编辑", "/rewind": "列出文件检查点",
    "/checkpoints": "列出或预览检查点", "/restore": "恢复指定轮次的文件",
    "/recover": "查看或恢复异常退出的轮次", "/model": "查看或切换模型",
    "/login": "配置模型供应商的账号或 API Key",
    "/effort": "查看或切换受支持模型的推理强度",
    "/permissions": "查看或切换权限模式", "/plan": "切换只读计划模式", "/auto": "切换自动模式",
    "/context": "查看预算与占用，stats 查看详情", "/compact": "生成工作摘要并保留原始对话",
    "/cost": "查看实际 token 用量", "/tasks": "查看任务列表", "/skills": "查看可用 Skills",
    "/agents": "查看只读子代理", "/mcp": "查看、连接、断开 MCP", "/hooks": "查看已启用 hooks",
    "/memory": "查看或追加项目记忆", "/init": "为当前项目创建 AGENTS.md 模板",
    "/doctor": "检查配置和运行环境", "/cancel": "取消当前任务", "/queue": "查看待执行消息",
    "/clear-queue": "清空待执行消息",
    "/theme": "切换 midnight blue dark / light 主题", "/trust": "审阅并信任项目扩展配置",
}


def safe_text(value):
    value = ANSI.sub("", str(value))
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def is_login_command(value):
    return value.split(maxsplit=1)[:1] == ["/login"]


class PromptHistory(FileHistory):
    def append_string(self, string):
        if not is_login_command(string) or string.strip() == "/login":
            super().append_string(string)


class InputCompleter(Completer):
    def __init__(self, session):
        self.session = session
        self.paths = PathCompleter(get_paths=lambda: [str(session.root)], expanduser=False)

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        if before.startswith("/") and " " not in before:
            for name, detail in {**COMMANDS, **{"/" + s.name: s.description for s in self.session.skills.values()}}.items():
                if name.startswith(before):
                    yield Completion(name, start_position=-len(before), display=f" {name}", display_meta=f"  {detail}")
        else:
            command, _, argument = before.partition(" ")
            if command in {"/model", "/effort", "/permissions"}:
                prefix = argument.lstrip()
                choices = {
                    "/model": self.session.model_choices(),
                    "/effort": self.session.model_efforts(self.session.current_model_choice),
                    "/permissions": MODES,
                }[command]
                for choice in choices:
                    if choice.startswith(prefix):
                        yield Completion(choice, start_position=-len(prefix), display=f" {choice}")
                return
            word = document.get_word_before_cursor(WORD=True)
            if word.startswith("@"):
                from prompt_toolkit.document import Document
                for item in self.paths.get_completions(Document(word[1:]), complete_event):
                    yield item


class BorderedCompletionsMenu(ConditionalContainer):
    """Native completion menu with thin rules above and below the list."""

    def __init__(self, *, filter, max_height=16, scroll_offset=1):
        body = Window(
            content=CompletionsMenuControl(),
            width=Dimension(min=8),
            height=Dimension(min=1, max=max_height),
            scroll_offsets=ScrollOffsets(top=scroll_offset, bottom=scroll_offset),
            right_margins=[ScrollbarMargin(display_arrows=False)],
            dont_extend_width=True,
            style="class:completion-menu",
        )
        top = Window(height=1, char="─", style="class:completion-border")
        bottom = Window(height=1, char="─", style="class:completion-border")
        super().__init__(content=HSplit([top, body, bottom]), filter=filter)


def install_bordered_completion_menu(prompt):
    """Swap prompt_toolkit's plain completion menu for a bordered one."""
    for container in prompt.app.layout.walk():
        if not isinstance(container, FloatContainer):
            continue
        for item in container.floats:
            if isinstance(item.content, CompletionsMenu):
                item.content = BorderedCompletionsMenu(filter=item.content.filter)


class Renderer:
    def __init__(self, console=None):
        self.console = console or Console(highlight=False)
        self.expanded = False
        self.streaming = False
        self.stream_tail = ""
        self.output = []
        self.started = time.monotonic()
        self.light = False

    def welcome(self, session):
        self.light = session.config.data.get("theme") == "light"
        title = Text.assemble(("Nano Code", f"bold {ACCENT}"), (f" v{__version__}", "dim"))
        details = Group(
            title,
            Text(safe_text(session.config.data["model"]), style="dim", overflow="fold"),
            Text(safe_text(str(session.root)), style="dim", overflow="fold"),
        )
        art = mascot(color=self.console.color_system is not None and not self.console.no_color)
        self.console.print()
        if self.console.width < 30:
            self.console.print(Group(art, details))
        else:
            banner = Table.grid(padding=(0, 2), expand=True)
            banner.add_column(no_wrap=True)
            banner.add_column(ratio=1, vertical="middle")
            banner.add_row(art, details)
            self.console.print(banner)
        self.console.print(Text("/help 命令 · Alt+Enter 换行 · Ctrl+C 取消", style="dim"))
        self.console.print()

    def event(self, event):
        kind = event["type"]
        if kind in {"text_delta", "tool_output"}:
            if kind == "tool_output" and not self.expanded:
                return
            if not self.streaming:
                self.stream_tail = "● " if kind == "text_delta" else ""
            complete, newline, self.stream_tail = (self.stream_tail + safe_text(event["text"])).rpartition("\n")
            if newline:
                self.console.print(Text(complete), soft_wrap=True)
            self.streaming = True
            return
        if kind in {"model_request", "usage", "subagent_event"}:
            return
        if kind == "reply" and not self.streaming:
            self.console.print(Text("● " + safe_text(event["text"])))
        self.finish_stream()
        if kind == "turn_start":
            self.output = []
        elif kind == "tool_request":
            f = event["call"]["function"]
            try:
                arguments = json.loads(f["arguments"])
                detail = arguments.get("file_path") or arguments.get("query") or arguments.get("argv") or arguments.get("name") or ""
            except ValueError:
                detail = ""
            self.console.print(Text(f"● {f['name']}  {safe_text(detail)[:180]}", style=ACCENT))
        elif kind == "tool_result":
            self.output.append(event)
            content = safe_text(event["result"])
            status = event["outcome"]["status"]
            ok = status in {"ok", "unchanged"}
            name = event["call"]["function"]["name"]
            files = parse_diff(content) if name in {"edit_file", "write_file"} else []
            if files:
                added, removed = totals(files)
                head = Text("  ⎿  " + " · ".join(file_title(item) for item in files), style=ACCENT if status == "ok" else WARNING)
                head.append(f"  +{added} -{removed}", style=SUCCESS if status == "ok" else WARNING)
                self.console.print(head)
                body, shown, total = files_text(
                    files, light=self.light, indent="     ", width=self.console.width,
                    max_lines=None if self.expanded else INLINE_DIFF_LINES,
                )
                self.console.print(body)
                if total > shown:
                    self.console.print(Text(f"     … 还有 {total - shown} 行 · /output 展开", style="dim"))
            elif self.expanded:
                self.console.print(Text(content, style="dim" if ok else WARNING))
            else:
                lines = content.splitlines()
                self.console.print(Text("  ⎿  " + (lines[0][:160] if lines else status), style="dim" if ok else WARNING))
                if len(lines) > 1:
                    self.console.print(Text(f"     +{len(lines)-1} lines · /output 展开", style="dim"))
        elif kind == "python_diagnostics":
            path = event["path"]
            items = event["diagnostics"]
            if items:
                self.console.print(Text("  Python 静态诊断", style=WARNING))
                for item in items:
                    self.console.print(Text("    " + safe_text(diagnostic_line(path, item)), style=WARNING))
            else:
                self.console.print(Text(f"  Python 静态诊断已清除 · {safe_text(path)}", style="dim"))
        elif kind in {"error", "notice", "recovery_error"}:
            self.console.print(Text("  " + safe_text(event["text"]), style=WARNING if kind == "notice" else ERROR))
        elif kind == "context_compaction" and event["trigger"] == "automatic":
            text = (f"自动压缩约 {event['before']:,} → {event['after']:,} tokens"
                    if event["status"] == "completed" else f"自动压缩跳过 · {event['reason']}")
            self.console.print(Text("  " + text, style="dim"))
        elif kind == "subagent_start":
            self.console.print(Text(f"● {event['agent']}  ·  read-only subagent", style=ACCENT))
        elif kind == "turn_end":
            state = event["verification"]["status"]
            label = {"passed": "验证通过", "failed": "验证失败", "not_run": "未运行验证", "stale": "修改后需要重新验证", "inconclusive": "验证结果不确定"}.get(state, state)
            files = len(event.get("changes", {}).get("files", []))
            self.console.print(Text(f"  {event['status']} · {time.monotonic()-self.started:.1f}s · {files} 个文件 · {label}", style=SUCCESS if state == "passed" else "dim"))
            self.console.print()

    def finish_stream(self):
        if self.stream_tail:
            self.console.print(Text(self.stream_tail), soft_wrap=True)
        self.stream_tail = ""
        self.streaming = False

    def show_output(self, console=None):
        console = self.console if console is None else console
        for event in self.output:
            name = event["call"]["function"]["name"]
            text = safe_text(event["result"])
            files = parse_diff(text) if name in {"edit_file", "write_file"} else []
            if files:
                added, removed = totals(files)
                title = file_title(files[0]) if len(files) == 1 else f"{len(files)} 个文件"
                console.print(Rule(f"{title}  +{added} -{removed}", style=ACCENT))
                body, _, _ = files_text(files, light=self.light, indent="", width=console.width)
                console.print(body)
            else:
                console.print(Rule(name, style=ACCENT))
                console.print(Syntax(text, "diff", theme="ansi_dark", word_wrap=True) if name == "edit_file" else Text(text))
        if not self.output:
            console.print("暂无工具输出。")


async def interactive(session, initial=None, *, terminal_input=None, terminal_output=None):
    from .commands import execute_command

    renderer = Renderer(Console(file=sys.stdout, highlight=False))
    renderer.welcome(session)
    work = WorkStatus()
    events = asyncio.Queue()
    prompts = asyncio.Queue()
    pending = deque()
    loop = asyncio.get_running_loop()
    closing = False
    command_running = False

    def enqueue(text, *, authorization_prompt=None, echo=False):
        prompts.put_nowait((text, authorization_prompt, time.monotonic(), echo))

    def cancel_work():
        session.cancel()
        work.cancel()

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
        if any(not item["future"].done() for item in pending):
            return
        current = session.permissions.mode
        following = (MODE_CYCLE.index(current) + 1) % len(MODE_CYCLE) if current in MODE_CYCLE else 0
        session.set_permission_mode(MODE_CYCLE[following], allow_busy=True)
        event.app.invalidate()

    @bindings.add("c-o")
    def expand(event):
        renderer.expanded = not renderer.expanded

    @bindings.add("c-c")
    def interrupt(event):
        if work.active or session.busy or command_running or pending:
            cancel_work()
            event.app.invalidate()
            return
        event.current_buffer.reset()
        event.app.exit(exception=KeyboardInterrupt())

    def toolbar():
        mode = session.permissions.mode
        label = {
            "default": "default mode on",
            "acceptEdits": "accept edits mode on",
            "plan": "plan mode on",
            "auto": "auto mode on",
            "dontAsk": "don't ask mode on",
        }.get(mode, f"{mode} mode on")
        hint = " (shift+tab to cycle)" if mode in MODE_CYCLE else " (/permissions to change)"
        mode_style = {
            "default": "class:toolbar.mode.default",
            "acceptEdits": "class:toolbar.mode.accept",
            "plan": "class:toolbar.mode.plan",
            "auto": "class:toolbar.mode.auto",
            "dontAsk": "class:toolbar.mode.dontask",
        }.get(mode, "class:toolbar.mode")
        width = prompt.app.output.get_size().columns
        fragments = [(mode_style, f" ⏵⏵ {label}")]
        if width >= 58:
            fragments.append(("class:toolbar.hint", hint))
        if width >= 105:
            budget = session.last_context_budget
            usage = f"~{budget.input_tokens:,}/{budget.limit:,} tokens  │  " if budget else ""
            fragments.append(("class:toolbar", f"  │  {session.uid[:8]}  │  {usage}queue {prompts.qsize()} "))
        return fragments

    try:
        session.config.home.mkdir(parents=True, exist_ok=True)
        history = PromptHistory(str(session.config.home / "history"))
    except OSError:
        history = InMemoryHistory()
    def theme():
        if session.config.data["theme"] == "light":
            return Style.from_dict({
                "prompt": f"{ACCENT_DARK} bold",
                "bottom-toolbar": "bg:#e8edf9 #22304d noreverse",
                "toolbar": "bg:#e8edf9 #22304d noreverse",
                "toolbar.mode": "bg:#e8edf9 #1e3a8a bold noreverse",
                "toolbar.mode.default": "bg:#e8edf9 #1e3a8a bold noreverse",
                "toolbar.mode.accept": "bg:#e8edf9 #7e22ce bold noreverse",
                "toolbar.mode.plan": "bg:#e8edf9 #b45309 bold noreverse",
                "toolbar.mode.auto": "bg:#e8edf9 #15803d bold noreverse",
                "toolbar.mode.dontask": "bg:#e8edf9 #be123c bold noreverse",
                "toolbar.hint": "bg:#e8edf9 #5b6b85 noreverse",
                "work.label": f"{ACCENT_DARK} bold",
                "work.note": "#4c6ef5",
                "work.detail": "#5b6b85",
                "completion-menu": "bg:#f4f7ff #1e2a4a",
                "completion-border": "bg:#f4f7ff #b8c7e6",
                "completion-menu.completion": "bg:#f4f7ff #1e3a8a",
                "completion-menu.completion.current": "bg:#f4f7ff #0b255e bold noreverse",
                "completion-menu.meta.completion": "bg:#f4f7ff #5b6b85",
                "completion-menu.meta.completion.current": "bg:#f4f7ff #1e3a8a noreverse",
                "completion-menu.multi-column-meta": "bg:#f4f7ff #5b6b85",
                "scrollbar.background": "bg:#f4f7ff",
                "scrollbar.button": "bg:#8aa4ff",
            })
        return Style.from_dict({
            "prompt": f"{ACCENT_LIGHT} bold",
            "bottom-toolbar": "bg:#111a2e #b8c4e0 noreverse",
            "toolbar": "bg:#111a2e #b8c4e0 noreverse",
            "toolbar.mode": "bg:#111a2e #8aa4ff bold noreverse",
            "toolbar.mode.default": "bg:#111a2e #8aa4ff bold noreverse",
            "toolbar.mode.accept": "bg:#111a2e #c084fc bold noreverse",
            "toolbar.mode.plan": "bg:#111a2e #fbbf24 bold noreverse",
            "toolbar.mode.auto": "bg:#111a2e #4ade80 bold noreverse",
            "toolbar.mode.dontask": "bg:#111a2e #fb7185 bold noreverse",
            "toolbar.hint": "bg:#111a2e #8494b4 noreverse",
            "work.label": f"{ACCENT_LIGHT} bold",
            "work.note": ACCENT_SOFT,
            "work.detail": MUTED,
            "completion-menu": "bg:#0b1224 #c7d2fe",
            "completion-border": "bg:#0b1224 #2f4478",
            "completion-menu.completion": "bg:#0b1224 #8aa4ff",
            "completion-menu.completion.current": "bg:#0b1224 #e0e7ff bold noreverse",
            "completion-menu.meta.completion": "bg:#0b1224 #8494b4",
            "completion-menu.meta.completion.current": "bg:#0b1224 #c7d2fe noreverse",
            "completion-menu.multi-column-meta": "bg:#0b1224 #8494b4",
            "scrollbar.background": "bg:#0b1224",
            "scrollbar.button": "bg:#3b5bdb",
        })

    from prompt_toolkit.styles import DynamicStyle
    prompt = PromptSession(
        history=history, completer=InputCompleter(session),
        complete_while_typing=True, key_bindings=bindings, multiline=True,
        prompt_continuation="  ", bottom_toolbar=toolbar,
        style=DynamicStyle(theme),
        refresh_interval=0.25,
        reserve_space_for_menu=3,
        input=terminal_input, output=terminal_output,
    )

    install_bordered_completion_menu(prompt)

    def pending_request():
        return next((item for item in pending if not item["future"].done()), None)

    def status_fragments():
        waiting = pending_request()
        hint = None
        if waiting and not work.cancelling:
            hint = "等待授权 · y 允许 / s 本会话 / n 拒绝" if waiting["type"] == "approval" else "等待你的回答"
        return work.fragments(prompt.app.output.get_size().columns,
                              color=not renderer.console.no_color, waiting=hint)

    prompt.app.layout.container = HSplit([
        ConditionalContainer(
            Window(FormattedTextControl(lambda: renderer.stream_tail), wrap_lines=True,
                   dont_extend_height=True), filter=~is_done & Condition(lambda: bool(renderer.stream_tail)),
        ),
        ConditionalContainer(
            Window(FormattedTextControl(status_fragments), height=1, wrap_lines=False),
            filter=~is_done & Condition(lambda: work.active or pending_request() is not None),
        ),
        prompt.app.layout.container,
    ])

    async def print_above(callback):
        with set_app(prompt.app):
            await run_in_terminal(callback)

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
                    await print_above(display)
                else:
                    work.event(event)
                    kind = event["type"]
                    if kind in {"text_delta", "tool_output"} and "\n" not in event["text"]:
                        renderer.event(event)
                    elif kind not in {"model_request", "usage", "subagent_event"}:
                        await print_above(lambda: renderer.event(event))
            except Exception as error:
                if event.get("future") and not event["future"].done():
                    event["future"].set_result(False if event["type"] == "approval" else "无法展示问题。")
                renderer.console.print(Text(str(error), style=ERROR))
            finally:
                events.task_done()
                prompt.app.invalidate()

    async def run_prompts():
        while True:
            text, authorization_prompt, submitted, echo = await prompts.get()
            work.start(submitted)
            renderer.started = submitted
            prompt.app.invalidate()
            try:
                # PromptSession leaves typed submissions in the terminal history.
                if echo:
                    await print_above(lambda: renderer.console.print(Text.assemble(
                        ("❯ ", f"bold {ACCENT}"), (safe_text(text), "bold"))))
                if work.cancelling:
                    continue
                from .commands import expand_references
                await asyncio.to_thread(
                    lambda: session.run(expand_references(session, text),
                                        authorization_prompt=text if authorization_prompt is None else authorization_prompt)
                )
            except (Exception, KeyboardInterrupt) as error:
                await print_above(lambda error=error: renderer.console.print(Text(str(error) or "已取消。", style=ERROR)))
            finally:
                await events.join()
                work.stop()
                if renderer.streaming:
                    await print_above(renderer.finish_stream)
                prompt.app.invalidate()
                prompts.task_done()

    consumer = asyncio.create_task(consume_events())
    worker = asyncio.create_task(run_prompts())
    if initial and not is_login_command(initial):
        enqueue(initial, echo=True)
    try:
        with create_app_session(input=terminal_input, output=terminal_output), patch_stdout(raw=True):
            while not closing:
                try:
                    if initial and is_login_command(initial):
                        text, initial = initial.strip(), None
                    else:
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
                if text == "/cancel":
                    cancel_work()
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
                if text == "/queue":
                    renderer.console.print(f"待执行消息：{prompts.qsize()}。/clear-queue 清除待执行消息。")
                    continue
                if text == "/clear-queue":
                    while not prompts.empty():
                        prompts.get_nowait()
                        prompts.task_done()
                    continue
                if is_login_command(text):
                    if text != "/login":
                        renderer.console.print(Text("请单独输入 /login，然后在隐藏输入框中填写凭据。", style=WARNING))
                    elif session.busy or work.active or command_running or not prompts.empty():
                        renderer.console.print(Text("运行中或队列未清空时不能配置供应商。", style=WARNING))
                    else:
                        try:
                            result = await configure_provider(session, rows=prompt.app.output.get_size().rows)
                            if result is None:
                                renderer.console.print(Text("已取消登录。", style=WARNING))
                            else:
                                label, verified = result
                                status = "已配置" if verified else "已保存，凭据将在模型请求时验证"
                                renderer.console.print(Text(f"{label} · {status}。使用 /model 选择模型。", style=ACCENT))
                        except ValueError as error:
                            renderer.console.print(Text(str(error), style=ERROR))
                    continue
                command, _, argument = text.partition(" ")
                if command == "/model":
                    model_busy = session.busy or work.active or command_running or not prompts.empty()
                    if model_busy and not argument:
                        renderer.console.print(Text("运行中或队列未清空时不能切换模型。", style=WARNING))
                        continue
                    candidate, _ = session.split_model_choice(argument)
                    if not model_busy and (not argument or candidate in CATALOG_EFFORT_PROVIDERS):
                        await session.refresh_model_catalogs()
                if text == "/model":
                    size = prompt.app.output.get_size()
                    choice = await pick_model(session, width=size.columns, height=size.rows)
                    if choice:
                        try:
                            session.set_model(choice["model"])
                            session.set_effort(choice["effort"])
                            renderer.console.print(Text(
                                f"模型 {session.current_model_choice} · 推理强度 {choice['effort']}。", style=ACCENT))
                        except Exception as error:
                            renderer.console.print(Text(str(error), style=ERROR))
                    continue
                if text == "/effort":
                    if session.busy or work.active or command_running or not prompts.empty():
                        renderer.console.print(Text("运行中或队列未清空时不能切换推理强度。", style=WARNING))
                        continue
                    levels = effort_choices(session, session.current_model_choice)
                    if len(levels) <= 1:
                        renderer.console.print(Text(
                            f"{session.config.data['model']} 在当前 API 环境下没有可切换的推理强度。"))
                        continue
                    size = prompt.app.output.get_size()
                    effort = await pick_effort(session, width=size.columns)
                    if effort:
                        try:
                            session.set_effort(effort)
                            renderer.console.print(Text(f"推理强度：{effort}。", style=ACCENT))
                        except Exception as error:
                            renderer.console.print(Text(str(error), style=ERROR))
                    continue
                if text.startswith("/"):
                    try:
                        # Commands run in a thread so permission prompts remain responsive.
                        if command_running:
                            renderer.console.print("上一条命令仍在执行。")
                            continue
                        async def run_command(command, running):
                            nonlocal command_running
                            command_running = True
                            try:
                                reply = await asyncio.to_thread(execute_command, session, command, renderer,
                                                                console=Console(highlight=False), running=running)
                                if isinstance(reply, str) and reply:
                                    enqueue(reply, authorization_prompt=command, echo=True)
                            except (Exception, KeyboardInterrupt) as error:
                                renderer.console.print(Text(str(error) or "已取消。", style=ERROR))
                            finally:
                                command_running = False
                        command_task = asyncio.create_task(run_command(text, work.active or not prompts.empty()))
                        await asyncio.sleep(0)
                    except Exception as error:
                        renderer.console.print(Text(str(error), style=ERROR))
                else:
                    if command_running:
                        renderer.console.print("命令处理中，请稍候。")
                        continue
                    enqueue(text)
    finally:
        cancel_work()
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
