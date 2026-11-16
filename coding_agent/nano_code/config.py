"""Layered configuration; project executables require explicit workspace trust."""

import hashlib
import json
import os
from pathlib import Path

from dotenv import dotenv_values

DEFAULTS = {
    "provider": "openai", "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
    "api_key_env": "API_KEY", "permission_mode": "default", "max_turns": 24,
    "context_limit": 80000, "command_timeout": 120, "request_timeout": 60,
    "streaming": True, "temperature": 0.3, "theme": "dark",
    "top_p": 1.0, "max_retries": 3, "retry_base_delay": 0.5,
    "permissions": {"allow": [], "deny": []}, "mcpServers": {}, "hooks": {},
}
MODES = ("default", "acceptEdits", "plan", "dontAsk")


def config_home():
    return Path(os.environ.get("NANO_CODE_HOME", str(Path.home() / ".nano-code")))


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取配置 {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象: {path}")
    return value


def merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Config:
    def __init__(self, root, *, overrides=None, env_file=None):
        self.root = Path(root).resolve()
        self.home = config_home()
        self.sources = []
        user = read_json(self.home / "settings.json")
        self.project = {}
        self.data = merge(DEFAULTS, user)
        for path in (self.root / ".nano/settings.json", self.root / ".nano/settings.local.json"):
            layer = read_json(path)
            if layer:
                self.sources.append(str(path))
                self.project = merge(self.project, layer)
        if self.trusted:
            self.data = merge(self.data, self.project)
        else:
            # Keep untrusted repositories from changing API endpoints, granting
            # permissions, or introducing values into executable configuration.
            project_rules = self.project.get("permissions", {})
            if isinstance(project_rules, dict) and isinstance(project_rules.get("deny"), list):
                self.data["permissions"] = merge(self.data["permissions"], {
                    "deny": [*self.data["permissions"].get("deny", []), *project_rules["deny"]],
                })
        # Compatibility with the original local .env without copying secrets to JSON.
        legacy = Path(__file__).resolve().parents[1] / ".env"
        values = {}
        for path in (legacy, self.home / ".env", env_file):
            if path is not None and Path(path).is_file():
                values.update(dotenv_values(path))
        values.update(os.environ)
        self.env = values
        mapping = {"MODEL_NAME": "model", "BASE_URL": "base_url", "MODEL_PROVIDER": "provider",
                   "MAX_TOOL_ROUNDS": "max_turns", "CONTEXT_LIMIT_TOKENS": "context_limit",
                   "COMMAND_TIMEOUT_SECONDS": "command_timeout", "MAX_MODEL_REQUESTS": "request_limit",
                   "TEMPERATURE": "temperature", "TOP_P": "top_p", "MAX_RETRIES": "max_retries",
                   "RETRY_BASE_DELAY": "retry_base_delay"}
        for env, key in mapping.items():
            if values.get(env):
                self.data[key] = values[env]
        if values.get("STREAMING_ENABLED"):
            self.data["streaming"] = values["STREAMING_ENABLED"].lower() in {"1", "true", "yes"}
        self.data.update({k: v for k, v in (overrides or {}).items() if v is not None})
        if self.data["provider"] == "anthropic" and self.data["base_url"] == DEFAULTS["base_url"]:
            self.data["base_url"] = "https://api.anthropic.com"
        self._validate()
        self.user_extensions = {k: user.get(k, {}) for k in ("hooks", "mcpServers")}

    def _validate(self):
        from math import isfinite
        if self.data["permission_mode"] not in MODES:
            raise ValueError(f"permission_mode 必须是 {', '.join(MODES)}")
        if self.data["provider"] not in {"openai", "anthropic"}:
            raise ValueError("provider 必须是 openai 或 anthropic")
        for key in ("max_turns", "context_limit", "command_timeout", "request_timeout", "max_retries"):
            self.data[key] = int(self.data[key])
            if self.data[key] <= 0:
                raise ValueError(f"{key} 必须大于 0")
        if self.data.get("request_limit") is not None:
            self.data["request_limit"] = int(self.data["request_limit"])
            if self.data["request_limit"] <= 0:
                raise ValueError("request_limit 必须大于 0")
        self.data["temperature"] = float(self.data["temperature"])
        if not isfinite(self.data["temperature"]) or not 0 <= self.data["temperature"] <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        for key, limit in (("top_p", 1), ("retry_base_delay", 60)):
            self.data[key] = float(self.data[key])
            if not 0 <= self.data[key] <= limit:
                raise ValueError(f"{key} 必须在 0 到 {limit} 之间")
        for key in ("permissions", "hooks", "mcpServers"):
            if not isinstance(self.data[key], dict):
                raise ValueError(f"{key} 必须是对象")
        for key in ("allow", "deny"):
            if not isinstance(self.data["permissions"].get(key, []), list):
                raise ValueError(f"permissions.{key} 必须是数组")
        from urllib.parse import urlsplit
        url = urlsplit(self.data["base_url"])
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            raise ValueError("base_url 必须是无内嵌凭据的 HTTP(S) 地址")

    @property
    def api_key(self):
        name = self.data["api_key_env"]
        return self.env.get(name) or self.env.get("ANTHROPIC_API_KEY" if self.data["provider"] == "anthropic" else "OPENAI_API_KEY")

    @property
    def fingerprint(self):
        payload = json.dumps(self.project, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def trusted(self):
        return read_json(self.home / "trust.json").get(str(self.root)) == self.fingerprint

    def trust(self):
        path = self.home / "trust.json"
        values = read_json(path)
        values[str(self.root)] = self.fingerprint
        write_json(path, values)

    def extensions(self, kind):
        return self.data[kind] if self.trusted else self.user_extensions[kind]

