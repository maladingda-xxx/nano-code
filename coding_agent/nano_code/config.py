"""Layered configuration; project executables require explicit workspace trust."""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from context_budget import DEFAULT_MAX_OUTPUT, ModelBudget

from .provider_profiles import LoginStore, PROVIDERS, valid_model_id

DEFAULTS = {
    "provider": "openai", "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
    "models": [], "effort": "auto",
    "api_key_env": "API_KEY", "permission_mode": "default", "max_turns": 24,
    "context_limit": 80000, "command_timeout": 120, "request_timeout": 60,
    "context_models": {}, "context_safety_tokens": 512, "context_tool_result_chars": 3600,
    "context_prune_at": 0.60, "context_compact_at": 0.75, "context_auto_compact": True,
    "anthropic_prompt_cache": False,
    "streaming": True, "temperature": 0.3, "theme": "dark",
    "top_p": 1.0, "max_retries": 3, "retry_base_delay": 0.5,
    "permissions": {"allow": [], "deny": [], "autoAllow": []}, "mcpServers": {}, "hooks": {},
    "python_lsp": {},
}
MODE_CYCLE = ("default", "acceptEdits", "plan", "auto")
MODES = (*MODE_CYCLE, "dontAsk")
MODEL_SETTINGS = ("provider", "base_url", "model", "models", "effort", "stream_usage")


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
        self.logins = LoginStore(self.home)
        self.active_login = None
        self._legacy_model_settings = {}
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
                   "REASONING_EFFORT": "effort",
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
        if not valid_model_id(self.data["model"]):
            raise ValueError("model 必须是无空白的模型 ID。")
        models = self.data["models"]
        if not isinstance(models, list) or not all(valid_model_id(model) for model in models):
            raise ValueError("models 必须是模型 ID 列表。")
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
        lsp = self.data["python_lsp"]
        if not isinstance(lsp, dict):
            raise ValueError("python_lsp 必须是对象。")
        if lsp:
            argv, timeout = lsp.get("argv"), lsp.get("timeout", 3)
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
                    or not argv[0].strip() or Path(argv[0]).suffix.lower() in {".cmd", ".bat"}
                    or type(timeout) not in {int, float} or not 0.1 <= timeout <= 10):
                raise ValueError("python_lsp 需要可执行文件及参数数组 argv，timeout 须在 0.1 到 10 秒之间。")
        for key in ("allow", "deny", "autoAllow"):
            if not isinstance(self.data["permissions"].get(key, []), list):
                raise ValueError(f"permissions.{key} 必须是数组")
        for rule in self.data["permissions"].get("autoAllow", []):
            if not isinstance(rule, dict) or set(rule) != {"tool", "cwd", "argv"} or rule["tool"] != "execute_command":
                raise ValueError("permissions.autoAllow 需要精确的 execute_command、cwd 和 argv 规则。")
            argv, cwd = rule["argv"], rule["cwd"]
            if (not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv)
                    or not argv[0].strip() or not isinstance(cwd, str) or not cwd.strip()):
                raise ValueError("permissions.autoAllow 需要非空的 cwd 和命令参数数组。")
        url = urlsplit(self.data["base_url"])
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            raise ValueError("base_url 必须是无内嵌凭据的 HTTP(S) 地址")
        from .reasoning import effort_levels
        effort = self.data["effort"]
        if effort not in (None, "auto", *effort_levels(self.data)):
            raise ValueError("当前模型或 API 端点不支持所配置的推理强度。")
        d = self.data
        if (type(d["context_safety_tokens"]) is not int or d["context_safety_tokens"] < 0
                or type(d["context_tool_result_chars"]) is not int or d["context_tool_result_chars"] < 256):
            raise ValueError("context_safety_tokens 须为非负整数，context_tool_result_chars 须至少为 256。")
        for key in ("context_auto_compact", "anthropic_prompt_cache"):
            if type(d[key]) is not bool:
                raise ValueError(f"{key} 必须是布尔值。")
        prune, compact = d["context_prune_at"], d["context_compact_at"]
        if not all(type(n) in {int, float} for n in (prune, compact)) or not 0 < prune < compact < 1:
            raise ValueError("要求 0 < context_prune_at < context_compact_at < 1。")
        profiles = d["context_models"]
        if not isinstance(profiles, dict) or any(
            not isinstance(profile, dict) or any(
                key not in {"window", "max_output"} or type(n) is not int or n <= 0
                for key, n in profile.items()
            ) for profile in profiles.values()
        ):
            raise ValueError("context_models 的 window 和 max_output 必须是正整数。")

    def model_budget(self, target=DEFAULT_MAX_OUTPUT):
        from .reasoning import provider_kind

        d = self.data
        name = d["model"]
        vendor = provider_kind(d, self.active_login)
        declared = self.active_login.limits.get(name, {}) if self.active_login else {}
        source = "供应商目录" if declared.get("window") else "本地回退"
        if vendor == "deepseek" and name in {"deepseek-flash", "deepseek-v4-pro"}:
            declared = {"window": 1_000_000, "max_output": 384_000}
            source = "供应商文档"
        overrides = d["context_models"]
        profile = {**overrides.get(name, {}), **overrides.get(f"{vendor}:{name}", {})}
        if "window" in profile:
            source = "模型配置"
        effective = {**declared, **profile}
        window = effective.get("window")
        limit = min(d["context_limit"], window) if window is not None else d["context_limit"]
        return ModelBudget(limit, min(target, effective.get("max_output", target)),
                           min(d["context_safety_tokens"], limit // 8),
                           name if vendor == "openai" else "unknown", window, source)

    @property
    def api_key(self):
        if self.active_login is not None:
            return self.active_login.credential
        name = self.data["api_key_env"]
        return self.env.get(name) or self.env.get("ANTHROPIC_API_KEY" if self.data["provider"] == "anthropic" else "OPENAI_API_KEY")

    @property
    def provider_id(self):
        return self.active_login.provider if self.active_login is not None else self.data["provider"]

    @property
    def legacy_model_settings(self):
        return self.data if self.active_login is None else self._legacy_model_settings

    def select_connection(self, login, model):
        if self.active_login is None:
            self._legacy_model_settings = {key: self.data.get(key) for key in MODEL_SETTINGS}
        if login is None:
            self.data.update(self._legacy_model_settings)
            self.data["model"] = model
        else:
            self.data.update(provider=PROVIDERS[login.provider].adapter, base_url=login.base_url,
                             model=model, models=list(login.models), effort="auto", stream_usage=False)
        self.active_login = login

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

