"""Resolve reasoning effort from the selected connection and model."""

import re
from urllib.parse import urlsplit

from .provider_profiles import PROVIDERS, REASONING_LEVELS

CATALOG_EFFORT_PROVIDERS = {"anthropic", "openrouter"}
_OFFICIAL_ENDPOINTS = {
    (entry.adapter, urlsplit(entry.base_url).hostname): name
    for name, entry in PROVIDERS.items() if entry.base_url
}
_LOW_HIGH_MAX = ("low", "high", "max")
_LOW_TO_HIGH = ("low", "medium", "high")
_FAMILY_LEVELS = {
    "deepseek": ((r"deepseek-(?:flash|v4(?:\.\d+)?-(?:flash|pro))(?:-\d+)?", _LOW_HIGH_MAX),),
    "kimi": ((r"kimi-k3(?:\.\d+)?(?:-[a-z0-9]+)*", _LOW_HIGH_MAX),),
    "gemini": (
        (r"gemini-4-(?:neon|hydrogen)(?:-[a-z0-9]+)*", (*_LOW_TO_HIGH, "xhigh")),
        (r"gemini-(?:2\.5|4(?:\.\d+)?)-[a-z0-9-]+", _LOW_TO_HIGH),
        (r"gemini-3(?:\.\d+)?-(?:pro|flash)(?:-[a-z0-9]+)*", ("low", "high")),
    ),
    "groq": ((r"openai/gpt-oss-\d+b", _LOW_TO_HIGH),),
    "minimax": ((r"MiniMax-M3\.1-Flash(?:-\d+)?", (*_LOW_TO_HIGH, "xhigh", "max")),),
    # Newer GPT models require the Responses API to combine tools with reasoning.
    "openai": ((r"gpt-5(?:\.[01])?(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?", _LOW_TO_HIGH),),
}


def provider_kind(data, login=None):
    if login is not None:
        return login.provider
    endpoint = urlsplit(data["base_url"])
    if endpoint.scheme == "https":
        return _OFFICIAL_ENDPOINTS.get((data["provider"], endpoint.hostname))
    return None


def effort_levels(data, login=None):
    model = data["model"]
    if login is not None and model in login.reasoning:
        return login.reasoning[model]
    provider = provider_kind(data, login)
    for pattern, levels in _FAMILY_LEVELS.get(provider, ()):
        if re.fullmatch(pattern, model):
            return levels
    return ()


def catalog_effort_levels(provider, record):
    """Read only provider-documented effort metadata, never infer it from an ID."""
    if provider == "openrouter":
        reasoning = record.get("reasoning")
        if not isinstance(reasoning, dict) or "supported_efforts" not in reasoning:
            return None
        levels = reasoning["supported_efforts"]
        if levels is None:
            levels = REASONING_LEVELS
        elif not isinstance(levels, list):
            return None
        return tuple(level for level in REASONING_LEVELS
                     if level in levels and (level != "none" or reasoning.get("mandatory") is not True))
    if provider == "anthropic":
        capabilities = record.get("capabilities")
        effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
        if isinstance(effort, dict):
            return tuple(level for level in REASONING_LEVELS
                         if effort.get("supported") is True and isinstance(effort.get(level), dict)
                         and effort[level].get("supported") is True)
    return None
