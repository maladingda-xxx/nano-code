"""Built-in provider definitions and user-local login records."""

import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    models: tuple[str, ...]
    adapter: str = "openai"
    catalog: str | None = "/models"
    browser_login: bool = False


PROVIDERS = {
    "deepseek": Provider("DeepSeek", "https://api.deepseek.com", ("deepseek-flash", "deepseek-v4-pro")),
    "openai": Provider("OpenAI", "https://api.openai.com/v1", ("gpt-5.4-mini", "gpt-5.4")),
    "anthropic": Provider("Anthropic / Claude", "https://api.anthropic.com/v1",
                          ("claude-sonnet-5", "claude-opus-5"), adapter="anthropic"),
    "gemini": Provider("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
                       ("gemini-4-hydrogen", "gemini-2.5-flash")),
    "openrouter": Provider("OpenRouter", "https://openrouter.ai/api/v1", ("openrouter/auto",),
                           catalog="/models/user", browser_login=True),
    "qwen": Provider("阿里云百炼 / Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1",
                     ("qwen-plus", "qwen3-coder-plus"), catalog=None),
    "kimi": Provider("月之暗面 / Kimi", "https://api.moonshot.cn/v1",
                     ("kimi-k3.1-pro", "kimi-k3.1-pro-highspeed")),
    "glm": Provider("智谱 / GLM", "https://open.bigmodel.cn/api/paas/v4", ("glm-5.3", "glm-5.2"), catalog=None),
    "minimax": Provider("MiniMax", "https://api.minimax.io/v1", ("MiniMax-M3.1-Flash", "MiniMax-M3"), catalog=None),
    "xai": Provider("xAI / Grok", "https://api.x.ai/v1", ("grok-4.3", "grok-4.7")),
    "groq": Provider("Groq", "https://api.groq.com/openai/v1", ("openai/gpt-oss-20b", "openai/gpt-oss-120b")),
    "mistral": Provider("Mistral", "https://api.mistral.ai/v1", ("mistral-medium-latest", "devstral-latest")),
    "azure": Provider("Microsoft Azure OpenAI", "", ()),
}


def valid_model_id(value):
    return isinstance(value, str) and bool(value) and not any(
        c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in value
    )


def valid_secret(value):
    return isinstance(value, str) and bool(value) and all("!" <= char <= "~" for char in value)


def azure_endpoint(value):
    url = urlsplit(value.strip())
    if (
        url.scheme != "https"
        or not re.fullmatch(r"[a-z0-9-]+\.(?:openai\.azure\.com|services\.ai\.azure\.com)", url.netloc)
        or url.path.rstrip("/") not in {"", "/openai/v1"}
        or url.query or url.fragment
    ):
        raise ValueError("请输入 Azure OpenAI 的 HTTPS 资源地址或 /openai/v1 地址。")
    return f"https://{url.netloc}/openai/v1"


@dataclass(frozen=True)
class Login:
    provider: str
    credential: str = field(repr=False)
    models: tuple[str, ...]
    endpoint: str = ""
    reasoning: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if self.provider not in PROVIDERS or not valid_secret(self.credential):
            raise ValueError("请输入有效的 API Key 或 Token。")
        if not isinstance(self.models, tuple) or not self.models or not all(map(valid_model_id, self.models)):
            raise ValueError("请输入有效的模型 ID 或部署名称。")
        if not isinstance(self.reasoning, dict) or any(
            model not in self.models or not isinstance(levels, tuple)
            or any(level not in REASONING_LEVELS for level in levels)
            for model, levels in self.reasoning.items()
        ):
            raise ValueError("模型推理档位无效。")
        if self.provider == "azure":
            object.__setattr__(self, "endpoint", azure_endpoint(self.endpoint))
        elif self.endpoint:
            raise ValueError("该供应商使用内置服务地址。")

    @property
    def base_url(self):
        return self.endpoint or PROVIDERS[self.provider].base_url


class LoginStore:
    def __init__(self, home):
        self.directory = home / "nano-provider-credentials"

    def read(self, provider):
        if provider not in PROVIDERS:
            return None
        try:
            raw = json.loads((self.directory / f"{provider}.json").read_text(encoding="utf-8"))
            if not isinstance(raw["models"], list):
                raise ValueError
            reasoning = raw.get("reasoning", {})
            if not isinstance(reasoning, dict) or any(not isinstance(levels, list) for levels in reasoning.values()):
                raise ValueError
            return Login(provider, raw["credential"], tuple(raw["models"]), raw.get("endpoint", ""),
                         {model: tuple(levels) for model, levels in reasoning.items()})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError(f"无法读取 {PROVIDERS[provider].label} 登录记录，请重新运行 /login。") from None

    def configured(self):
        for provider in PROVIDERS:
            try:
                login = self.read(provider)
            except ValueError:
                continue
            if login is not None:
                yield login

    def save(self, login):
        target = self.directory / f"{login.provider}.json"
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            # NamedTemporaryFile uses owner-only access on POSIX. On Windows the
            # file inherits permissions from the user configuration directory.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".login-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"credential": login.credential, "models": login.models,
                           "endpoint": login.endpoint, "reasoning": login.reasoning}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            temporary = None
        except OSError:
            raise ValueError("无法保存供应商登录。请检查用户配置目录的写入权限。") from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()
