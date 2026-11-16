"""Installed CLI and human-readable or structured headless modes."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from workspace import find_root
from . import __version__, sessions
from .config import Config, MODES, config_home, read_json, write_json
from .runtime import Session


def parse_argv(raw):
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a JSON array of command arguments") from error
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise argparse.ArgumentTypeError("Use a nonempty JSON string array")
    return value


def _program_name():
    name = Path(sys.argv[0]).stem
    return name if name in {"nano", "nano-code", "coding-agent"} else "nano-code"


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog=_program_name(), description="Nano Code · terminal coding assistant")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("-p", "--print", dest="print_prompt", metavar="PROMPT")
    parser.add_argument("--json", action="store_true", help="alias for --output-format stream-json")
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    parser.add_argument("--rpc", action="store_true", help="serve JSON-RPC over stdin/stdout")
    parser.add_argument("-r", "--resume", nargs="?", const="", metavar="SESSION")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true")
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--provider", choices=("openai", "anthropic"))
    parser.add_argument("--base-url")
    parser.add_argument("--permission-mode", choices=MODES)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--allow-command", action="append", type=parse_argv, default=[])
    parser.add_argument("--allow-edits", action="store_true", help="permit workspace edits in headless mode")
    parser.add_argument("--mcp", action="append", default=[], metavar="SERVER", help="explicitly authorize connection to a configured server")
    parser.add_argument("--version", action="version", version=f"Nano Code {__version__}")
    args = parser.parse_args(argv)
    errors = Console(stderr=True)
    if args.print_prompt is not None and args.prompt is not None:
        parser.error("Specify one prompt, either positional or with --print")
    if args.fork_session and args.resume is None and not args.continue_session:
        parser.error("--fork-session requires --resume or --continue")
    if not args.cwd.is_dir() or args.env_file and not args.env_file.is_file():
        parser.error("Workspace directory or env file does not exist")
    text = args.print_prompt if args.print_prompt is not None else args.prompt
    output = "stream-json" if args.json else args.output_format
    headless = args.print_prompt is not None or output != "text" or not sys.stdin.isatty()
    if args.rpc and text is not None:
        parser.error("--rpc receives prompts from stdin")

    try:
        root = find_root(args.cwd.resolve())
        override = {"model": args.model, "provider": args.provider, "base_url": args.base_url,
                    "max_turns": args.max_turns, "permission_mode": args.permission_mode}
        if args.allow_edits and not args.permission_mode:
            override["permission_mode"] = "acceptEdits"
        config = Config(root, overrides=override, env_file=args.env_file)
        if text == "setup" and args.print_prompt is None:
            return setup(config)
        resume = args.resume if args.resume is not None else "" if args.continue_session else None
        with Session(args.cwd, config=config, resume=resume, fork_session=args.fork_session) as session:
            if text == "doctor" and args.print_prompt is None:
                from .commands import diagnostics
                print(json.dumps(diagnostics(session), ensure_ascii=True, indent=2))
                return 0
            if text == "sessions" and args.print_prompt is None:
                print(json.dumps(sessions.list_sessions(), ensure_ascii=True, indent=2))
                return 0
            session.permissions.rules = {**session.permissions.rules, "allow": [
                *session.permissions.rules.get("allow", []),
                *({"tool": "execute_command", "argv": cmd, "cwd": "."} for cmd in args.allow_command),
            ]}
            for server in args.mcp:
                if server not in config.extensions("mcpServers"):
                    raise ValueError(f"MCP 服务不可用: {server}")
                settings = config.extensions("mcpServers")[server]
                session.permissions.grants.add(session.permissions.signature("mcp_connect", {"server": server, "configuration": settings}))
                session.connect_mcp(server)
            if args.rpc:
                from .rpc import serve
                return serve(session)
            if not headless:
                from .ui import interactive
                return asyncio.run(interactive(session, text))
            if text is None or not text.strip():
                parser.error("Non-interactive mode requires -p PROMPT or --rpc")
            from .commands import expand_references
            return print_turn(session, expand_references(session, text), output, errors)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        if output != "text" and not args.rpc:
            print(json.dumps({"type": "error", "text": str(error)}, ensure_ascii=True))
        else:
            errors.print(Text(str(error), style="red"))
        return 2


def print_turn(session, prompt, output, errors):
    reply = None

    def emit(event):
        nonlocal reply
        if event["type"] == "reply":
            reply = event["text"]
        if output == "stream-json":
            print(json.dumps(event, ensure_ascii=True), flush=True)
        elif event["type"] == "error" and output == "text":
            errors.print(Text(event["text"], style="red"))

    session.events = emit
    result = session.run(prompt)
    if output == "json":
        print(json.dumps({**result, "result": reply, "usage": session.usage}, ensure_ascii=True))
    elif output == "text" and reply:
        print(reply)
    return {"completed": 0, "error": 1, "interrupted": 130}[result["status"]]


def setup(config):
    if not sys.stdin.isatty():
        raise ValueError("setup 需要交互终端。可通过环境变量配置模型。")
    provider = input(f"Provider [openai/anthropic] ({config.data['provider']}): ").strip() or config.data["provider"]
    default_url = "https://api.anthropic.com" if provider == "anthropic" else config.data["base_url"]
    base_url = input(f"API base URL ({default_url}): ").strip() or default_url
    default_model = config.data["model"] if provider == config.data["provider"] else ""
    model = input(f"Model ID ({default_model}): ").strip() or default_model
    if not model:
        raise ValueError("请提供模型服务商支持的模型 ID。")
    values = {"provider": provider, "base_url": base_url, "model": model}
    Config(config.root, overrides=values)
    path = config_home() / "settings.json"
    write_json(path, {**read_json(path), **values})
    print(f"已保存 {path}。请设置 API_KEY，或为此项目提供 --env-file。")
    return 0
