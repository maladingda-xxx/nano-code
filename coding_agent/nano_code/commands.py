"""User-facing commands; mutations are explicit and never inferred from model text."""

import json
import re
import shutil
import sys
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import tools
from changes import list_checkpoints, preview_checkpoint, resolve_turn_id, restore_checkpoint, undo_last_edit
from context_budget import estimate_tokens
from execution_store import execution_session, pending_executions, recover_execution
from workspace import workspace_path

from . import __version__, sessions
from .config import MODES
from .extensions import expand_skill


def expand_references(session, text):
    references = re.findall(r'(?<!\w)@(?:"([^"]+)"|([^\s]+))', text)
    additions = []
    for quoted, plain in references[:10]:
        name = quoted or plain
        path = workspace_path(session.root, name, "read_file")
        if not path.is_file():
            raise ValueError(f"引用的文件不存在: {name}")
        if not session.permissions.authorize("read_file", {"file_path": name}):
            raise ValueError(f"无权引用文件: {name}")
        session._activate()
        additions.append(f"\n\n<file path={json.dumps(name)}>\n{tools.read_file(name, line_end=300)}\n</file>")
    return text + "".join(additions)


def diagnostics(session):
    return {
        "version": __version__, "python": sys.version.split()[0], "executable": sys.executable,
        "workspace": str(session.root), "git": shutil.which("git"), "node": shutil.which("node"),
        "provider": session.config.data["provider"], "model": session.config.data["model"],
        "api_key_configured": bool(session.config.api_key), "project_extensions_trusted": session.config.trusted,
        "skills": len(session.skills), "mcp_configured": list(session.config.data["mcpServers"]),
        "extension_errors": session.extension_errors,
        "checks": {"python_supported": sys.version_info >= (3, 11), "workspace_exists": session.root.is_dir()},
    }


def execute_command(session, line, renderer):
    from .ui import COMMANDS, ACCENT, safe_text
    command, _, argument = line.partition(" ")
    argument = argument.strip()
    command = {"/load": "/resume", "/save": "/rename", "/stats": "/cost", "/rewind": "/checkpoints"}.get(command, command)
    readonly = {"/help", "/status", "/diff", "/output", "/context", "/cost", "/tasks", "/skills", "/agents", "/hooks", "/doctor", "/sessions"}
    if session.busy and command not in readonly:
        raise ValueError("当前任务运行中；请等待结束或 /cancel 后操作。")
    out = renderer.console
    session._activate()
    if command == "/help":
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        for name, detail in COMMANDS.items():
            table.add_row(Text(name, style=ACCENT), Text(detail))
        out.print(table)
    elif command == "/clear":
        session.new()
        out.print("已保存旧会话并开始新会话。")
    elif command == "/status":
        out.print(Text(session.git(["status", "--short", "--branch"])))
        out.print(Text(f"{session.uid} · {session.config.data['provider']} / {session.config.data['model']} · {session.permissions.mode}"))
    elif command == "/diff":
        out.print(Syntax(safe_text(session.git_diff(argument == "--staged")), "diff", theme="ansi_dark", word_wrap=True))
    elif command == "/output":
        renderer.show_output()
    elif command == "/sessions":
        table = Table("ID", "名称", "消息", "更新时间", box=box.SIMPLE)
        for item in sessions.list_sessions():
            table.add_row(item["uid"][:8], Text(item["name"]), str(item["message_count"]), item["updated_at"])
        out.print(table)
    elif command == "/resume":
        session.resume(argument or None)
        out.print(Text(f"已恢复 {session.uid[:8]} · {session.label}"))
    elif command == "/rename":
        if not argument:
            raise ValueError("用法：/rename <名称>")
        with sessions.lease(session.uid):
            if not sessions.load(session.uid):
                session.persist()
            sessions.rename(session.uid, argument)
        session.label = argument
        out.print("会话已重命名。")
    elif command == "/fork":
        from .runtime import history_digest
        session.uid = sessions.fork(session.uid, session.messages, argument or None)
        session._saved_digest = history_digest(session.messages)
        session.label = argument or None
        out.print(Text(f"已进入分支会话 {session.uid[:8]}。文件工作区仍共享。"))
    elif command == "/export":
        name = argument or f"nano-session-{session.uid[:8]}.md"
        target = workspace_path(session.root, name, "export")
        out.print(Text(sessions.export_session(session.uid, session.messages, target)))
    elif command == "/undo":
        out.print(Text(undo_last_edit(session.root, session.uid)))
    elif command in {"/checkpoints", "/restore"}:
        if not argument:
            for checkpoint in list_checkpoints(100):
                if checkpoint.get("session_id") == session.uid:
                    out.print(Text(f"{checkpoint['turn_id'][:8]}  {', '.join(checkpoint['paths'])}"))
            out.print("用 /checkpoints <ID> 预览，/restore <ID> 恢复文件。")
        else:
            turn_id = resolve_turn_id(argument)
            entries = preview_checkpoint(session.root, turn_id)
            out.print_json(json.dumps(entries, ensure_ascii=False, default=str))
            if command == "/restore":
                answer = session.question("恢复这个轮次的内置文件修改？后续手工修改会保留。输入 yes 确认。", None) if session.question else "no"
                if answer.lower() in {"yes", "y"}:
                    out.print_json(json.dumps(restore_checkpoint(session.root, turn_id), ensure_ascii=False))
    elif command == "/recover":
        if not argument:
            for item in pending_executions():
                out.print(Text(f"{item['turn_id']} · {'进程仍在运行' if item['active'] else '可恢复'}"))
        else:
            matches = [x for x in pending_executions() if x["turn_id"].startswith(argument)]
            if len(matches) != 1:
                raise ValueError("请使用唯一的待恢复轮次 ID。")
            turn_id = matches[0]["turn_id"]
            recovered = recover_execution(turn_id)
            if recovered:
                session.uid = execution_session(turn_id) or session.uid
                session.messages = [{"role": "system", "content": session.system_prompt()}, *[m for m in recovered if m["role"] != "system"]]
                with sessions.lease(session.uid):
                    session.persist()
                out.print("已恢复。结果不确定的操作需先核查；不会自动重放命令。")
    elif command == "/model":
        if argument:
            session.config.data["model"] = argument
            if session.model is not None:
                client = getattr(session.model, "client", None)
                if client:
                    client.close()
                session.model = None
        out.print(Text(f"{session.config.data['provider']} / {session.config.data['model']}"))
    elif command in {"/permissions", "/plan"}:
        if command == "/plan":
            argument = "default" if session.permissions.mode == "plan" else "plan"
        if argument:
            if argument not in MODES:
                raise ValueError("可用模式：" + ", ".join(MODES))
            session.permissions.mode = argument
        out.print(Text(f"模式：{session.permissions.mode}。这是工具授权控制，不是操作系统沙箱。"))
        out.print_json(json.dumps(session.permissions.rules, ensure_ascii=False))
    elif command == "/context":
        out.print(Text(f"约 {estimate_tokens(session.messages):,} / {session.config.data['context_limit']:,} tokens · {len(session.messages)} 条消息"))
    elif command == "/compact":
        out.print(Text(session.compact(argument)))
    elif command == "/cost":
        out.print_json(json.dumps(session.usage))
        out.print("用量为本进程收到的服务端 token 统计；未返回 usage 的请求不计入。费用请查看模型服务商账单。")
    elif command == "/tasks":
        out.print(Text(session.tasks()))
    elif command in {"/skills", "/agents"}:
        entries = session.skills if command == "/skills" else session.agents
        if command == "/agents":
            out.print("explore · review  (内置，只读、前台运行)")
        for item in entries.values():
            out.print(Text(f"{item.name}  {item.description}\n  {item.path}"))
        if not entries:
            out.print("没有自定义条目。")
    elif command == "/hooks":
        out.print(Text("已启用事件：" + ", ".join(session.hooks.settings)))
    elif command == "/mcp":
        action, _, name = argument.partition(" ")
        if action == "connect" and name:
            count = session.connect_mcp(name)
            out.print(Text(f"{name} 已连接，{count} 个工具。"))
        elif action == "disconnect" and name in session.mcp:
            client = session.mcp.pop(name)
            client.close()
            removed = {n for n, (c, _) in session.mcp_tools.items() if c is client}
            session.mcp_tools = {n: v for n, v in session.mcp_tools.items() if n not in removed}
            session.tool_schemas[:] = [t for t in session.tool_schemas if t["function"]["name"] not in removed]
            out.print("已断开。")
        else:
            for name in session.config.data["mcpServers"]:
                out.print(Text(f"{name} · {'connected' if name in session.mcp else 'disconnected'}"))
            out.print("/mcp connect <name> · /mcp disconnect <name>")
    elif command == "/memory":
        name = ".nano/MEMORY.md"
        path = workspace_path(session.root, name, "memory")
        if argument:
            out.print(Text(tools.write_file(name, argument + "\n", mode="append")))
        else:
            out.print(Text(path.read_text(encoding="utf-8") if path.is_file() else "尚无项目记忆。/memory <内容> 追加。"))
    elif command == "/init":
        if (session.root / "AGENTS.md").exists():
            out.print("AGENTS.md 已存在，未覆盖。")
        else:
            template = "# Project instructions\n\n## Build and test\n\nDescribe the actual build, test and lint commands here.\n\n## Conventions\n\n- Read related code before editing.\n- Keep changes focused and verify affected behavior.\n- Report tests actually run and unresolved failures.\n"
            out.print(Text(tools.write_file("AGENTS.md", template)))
    elif command == "/doctor":
        out.print_json(json.dumps(diagnostics(session), ensure_ascii=False))
    elif command == "/theme":
        if argument not in {"dark", "light"}:
            raise ValueError("用法：/theme dark 或 /theme light")
        session.config.data["theme"] = argument
        out.print("主题已切换。")
    elif command == "/trust":
        out.print(Text("项目配置来源：\n" + "\n".join(session.config.sources)))
        if not session.config.sources:
            out.print("当前项目没有扩展配置。")
            return None
        answer = session.question("确认已审阅这些配置，并允许项目扩展和授权规则生效？输入 yes 确认。", None) if session.question else "no"
        if answer.lower() == "yes":
            session.config.trust()
            out.print("已信任当前配置。重新启动 Nano Code 后生效。配置变化后需要重新信任。")
    elif command[1:] in session.skills:
        return expand_skill(session.skills[command[1:]], argument)
    else:
        raise ValueError(f"未知命令：{command}。输入 /help 查看命令。")
