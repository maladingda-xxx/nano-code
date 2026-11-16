"""Discover declarative skills/agents; execute only trusted, explicit hooks."""

import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Instruction:
    name: str
    description: str
    body: str
    path: Path
    manual_only: bool = False


def parse_instruction(path):
    text = path.read_text(encoding="utf-8-sig")
    if len(text) > 100_000:
        raise ValueError(f"说明文件过大: {path}")
    metadata = {}
    if text.startswith("---\n") or text.startswith("---\r\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            text = parts[2].strip()
    if not isinstance(metadata, dict):
        raise ValueError(f"说明文件 frontmatter 必须为对象: {path}")
    name = str(metadata.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem))
    return Instruction(name, str(metadata.get("description", "")), text, path,
                       bool(metadata.get("disable-model-invocation", False)))


def discover(root, home, kind="skills"):
    result, errors = {}, []
    for directory in (home / kind, root / ".claude" / kind, root / ".nano" / kind):
        if not directory.is_dir():
            continue
        pattern = "*/SKILL.md" if kind == "skills" else "*.md"
        for path in sorted(directory.glob(pattern)):
            try:
                if not path.resolve().is_relative_to(directory.resolve()):
                    continue
                item = parse_instruction(path)
                if not item.name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in item.name):
                    raise ValueError(f"无效扩展名称: {item.name}")
                result[item.name] = item
            except (OSError, ValueError, yaml.YAMLError) as error:
                errors.append(str(error))
    return result, errors


def expand_skill(skill, arguments):
    return f"Skill: {skill.name}\nSource: {skill.path}\n\n" + skill.body.replace("$ARGUMENTS", arguments)


class Hooks:
    def __init__(self, settings, root, emit):
        self.settings, self.root, self.emit = settings, root, emit

    def run(self, event, payload):
        for hook in self.settings.get(event, []):
            if not isinstance(hook, dict):
                raise ValueError("hook 必须为对象")
            if not fnmatch.fnmatchcase(payload.get("tool", ""), hook.get("matcher", "*")):
                continue
            argv = hook.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise ValueError("hook.argv 必须为非空字符串数组")
            try:
                result = subprocess.run(
                    argv, cwd=self.root, input=json.dumps({"event": event, **payload}, ensure_ascii=False),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=min(60, max(1, float(hook.get("timeout", 10)))),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = (result.stdout + result.stderr)[-4000:]
                self.emit({"type": "hook", "event": event, "exit_code": result.returncode, "text": output})
                if event == "PreToolUse" and result.returncode != 0:
                    return False
            except (OSError, subprocess.TimeoutExpired) as error:
                self.emit({"type": "notice", "text": f"Hook {event}: {error}"})
                if event == "PreToolUse":
                    return False
        return True
