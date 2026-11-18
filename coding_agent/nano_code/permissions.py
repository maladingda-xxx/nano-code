"""Deterministic permissions, deliberately separate from process isolation."""

import fnmatch
import json

READ_TOOLS = {"read_file", "list_directory", "search_workspace", "search_in_file", "glob_files", "git_status", "git_diff", "load_skill", "task_list"}
WRITE_TOOLS = {"edit_file", "write_file"}


class Permissions:
    def __init__(self, mode="default", rules=None, ask=None):
        self.mode = mode
        self.rules = rules or {"allow": [], "deny": []}
        self.ask = ask
        self.grants = set()

    @staticmethod
    def signature(name, arguments):
        return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def matches(rule, name, arguments):
        if isinstance(rule, str):
            # Tool names, not shell-like string patterns over a serialized command.
            return fnmatch.fnmatchcase(name, rule)
        if not isinstance(rule, dict) or not fnmatch.fnmatchcase(name, rule.get("tool", "")):
            return False
        if "path" in rule:
            path = arguments.get("file_path")
            if not isinstance(path, str) or not isinstance(rule["path"], str) or not fnmatch.fnmatchcase(path, rule["path"]):
                return False
        if "argv" in rule and arguments.get("argv") != rule["argv"]:
            return False
        if "cwd" in rule and arguments.get("cwd", ".") != rule["cwd"]:
            return False
        return True

    def authorize(self, name, arguments):
        if any(self.matches(r, name, arguments) for r in self.rules.get("deny", [])):
            return False
        if self.mode == "plan" and name not in READ_TOOLS | {"ask_followup_question", "delegate"}:
            return False
        if name in READ_TOOLS or name in {"task_update", "ask_followup_question", "delegate"}:
            return True
        if self.mode in {"acceptEdits", "auto"} and name in WRITE_TOOLS:
            return True
        if self.mode == "auto" and name == "execute_command" and any(
            self.matches(rule, name, arguments) for rule in self.rules.get("autoAllow", [])
        ):
            return True
        signature = self.signature(name, arguments)
        if signature in self.grants or any(self.matches(r, name, arguments) for r in self.rules.get("allow", [])):
            return True
        if self.mode == "dontAsk" or self.ask is None:
            return False
        decision = self.ask(name, arguments)
        if decision == "session":
            self.grants.add(signature)
        return decision in {True, "yes", "session"}
