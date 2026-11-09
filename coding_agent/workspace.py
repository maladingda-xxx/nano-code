"""Workspace discovery and file boundaries shared by the built-in tools."""

import os
import subprocess
from pathlib import Path

from exceptions import ToolInputError, ToolSecurityError


def find_root(directory: Path) -> Path:
    directory = directory.resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=directory,
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return directory
    if result.returncode == 0:
        root = Path(result.stdout.strip()).resolve()
        if directory.is_relative_to(root):
            return root
    return directory


def workspace_path(root: Path, value: str, tool: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ToolInputError("路径不能为空或包含空字符。", tool_name=tool)
    try:
        requested = Path(os.path.abspath(root / value))
        requested_relative = requested.relative_to(root)
        path = requested.resolve()
        relative = path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ToolSecurityError(f"路径不在工作区内: {value}", tool_name=tool) from error
    for part in (*requested_relative.parts, *relative.parts):
        name = part.lower().rstrip(" .")
        if name in {".git", ".env", ".coding_agent_sessions.db"} or name.startswith(
            ".coding_agent_sessions.db-"
        ) or (os.name == "nt" and (":" in part or Path(part).is_reserved())):
            raise ToolSecurityError(f"路径受保护: {value}", tool_name=tool)
    return path


def read_project_instructions(root: Path, directory: Path) -> str:
    """Load AGENTS.md from the repository root through the launch directory."""
    relative = directory.resolve().relative_to(root)
    instructions = []
    for depth in range(len(relative.parts) + 1):
        name = Path(*relative.parts[:depth]) / "AGENTS.md"
        try:
            path = workspace_path(root, str(name), "AGENTS.md")
            with path.open(encoding="utf-8-sig") as stream:
                content = stream.read(16001)
        except (OSError, UnicodeError, ToolSecurityError):
            continue
        if len(content) > 16000:
            content = content[:16000] + "\n（文件过长，已截断）"
        instructions.append(f"### {name.as_posix()}\n{content}")
    return "\n\n".join(instructions)
