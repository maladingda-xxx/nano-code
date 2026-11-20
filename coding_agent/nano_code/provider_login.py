"""Non-billing provider discovery and OpenRouter browser authorization."""

import asyncio
import base64
import hashlib
import re
import secrets
import threading
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .provider_profiles import PROVIDERS, Login, valid_model_id, valid_secret
from .reasoning import catalog_effort_levels


def _catalog_models(provider, payload):
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return (), {}
    models = []
    reasoning = {}
    for record in records:
        if not isinstance(record, dict) or not valid_model_id(model := record.get("id")):
            continue
        if provider == "openai" and not re.fullmatch(
            r"(?:gpt-(?:4o|4\.1|5(?:\.[0-5])?)(?:-[\w.]+)*|o[134](?:-[\w.]+)*)", model,
        ):
            continue
        if provider == "gemini" and not model.startswith("gemini-"):
            continue
        if any(part in model.lower() for part in (
            "embed", "whisper", "transcri", "rerank", "moderation", "-tts", "-audio", "-image",
            "-realtime", "-search-preview", "-codex", "-instruct", "-deep-research",
        )):
            continue
        capabilities = record.get("capabilities")
        if isinstance(capabilities, dict) and (
            capabilities.get("completion_chat") is False or capabilities.get("function_calling") is False
        ):
            continue
        supported = record.get("supported_parameters")
        if provider == "openrouter" and isinstance(supported, list) and "tools" not in supported:
            continue
        models.append(model)
        if (levels := catalog_effort_levels(provider, record)) is not None:
            reasoning[model] = levels
    return tuple(dict.fromkeys(models)), reasoning


def _catalog_limits(provider, records, models):
    limits = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(model := record.get("id"), str) or model not in models:
            continue
        if provider == "anthropic":
            values = {"window": record.get("max_input_tokens"), "max_output": record.get("max_tokens")}
        elif provider == "openrouter":
            top = record.get("top_provider")
            values = {"window": record.get("context_length"),
                      "max_output": top.get("max_completion_tokens") if isinstance(top, dict) else None}
        else:
            continue
        if profile := {key: n for key, n in values.items() if type(n) is int and n > 0}:
            limits[model] = profile
    return limits


def _catalog_headers(login):
    if PROVIDERS[login.provider].adapter == "anthropic":
        return {"x-api-key": login.credential, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {login.credential}"}


async def _fetch_catalog(login, client):
    response = await client.get(login.base_url + PROVIDERS[login.provider].catalog)
    response.raise_for_status()
    if login.provider != "azure":
        catalog = response.json()
        models, reasoning = _catalog_models(login.provider, catalog)
        if models:
            return replace(login, models=models, reasoning=reasoning,
                           limits=_catalog_limits(login.provider, catalog["data"], set(models)))
    return login


async def refresh_catalog(login):
    """Update a session's model list without rewriting its saved credential."""
    try:
        async with asyncio.timeout(2), httpx.AsyncClient(timeout=2, headers=_catalog_headers(login)) as client:
            updated = await _fetch_catalog(login, client)
            return None if updated is login else updated
    except (TimeoutError, httpx.HTTPError, ValueError):
        return None


async def prepare_login(provider, credential, *, endpoint="", deployment=None):
    """Return a validated login record and whether a service verified the key."""
    definition = PROVIDERS[provider]
    login = Login(provider, credential, (deployment,) if provider == "azure" else definition.models, endpoint)
    if definition.catalog is None:
        return login, False
    verified = False
    try:
        async with httpx.AsyncClient(timeout=8, headers=_catalog_headers(login)) as client:
            if provider == "openrouter":
                key_response = await client.get(login.base_url + "/key")
                key_response.raise_for_status()
                verified = True
            return await _fetch_catalog(login, client), True
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if verified or status in {404, 405, 501} and provider != "openrouter":
            return login, verified
        if status in {401, 403}:
            raise ValueError(f"{definition.label} 拒绝了该凭据（HTTP {status}）。请检查权限后重试。") from None
        raise ValueError(f"{definition.label} 登录验证失败（HTTP {status}）。请重试。") from None
    except (httpx.HTTPError, ValueError):
        if verified:
            return login, True
        raise ValueError(f"无法验证 {definition.label} 登录。请检查网络后重试。") from None


async def openrouter_login(show_url, *, timeout=180):
    """Receive the one-time browser code and exchange it using S256 PKCE."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    path = "/oauth/callback/" + secrets.token_urlsafe(18)
    loop = asyncio.get_running_loop()
    received = loop.create_future()

    def complete(code):
        if not received.done():
            received.set_result(code)

    class LoopbackServer(ThreadingHTTPServer):
        def server_bind(self):
            # The callback URL uses the loopback IP, so binding needs no reverse DNS lookup.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            codes = query.get("code", [])
            states = query.get("state", [])
            if parsed.path != path:
                self.send_error(404)
                return
            if query.get("error") == ["access_denied"]:
                code, message = None, b"Authorization cancelled. Return to Nano Code."
            elif (
                len(codes) == len(states) == 1 and valid_secret(codes[0])
                and states[0].isascii() and secrets.compare_digest(state, states[0])
            ):
                code, message = codes[0], b"Authorization received. Return to Nano Code."
            else:
                self.send_error(400, "Invalid authorization response")
                return
            loop.call_soon_threadsafe(complete, code)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message)

    try:
        server = LoopbackServer(("127.0.0.1", 0), Callback)
    except OSError:
        raise ValueError("无法启动本地浏览器回调。请重试或使用 API Key。") from None
    with server:
        url = "https://openrouter.ai/auth?" + urlencode({
            "callback_url": f"http://127.0.0.1:{server.server_port}{path}",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "key_label": "Nano Code",
        })
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            show_url(url)
            async with asyncio.timeout(timeout):
                try:
                    await asyncio.to_thread(webbrowser.open, url)
                except (OSError, webbrowser.Error):
                    pass  # The terminal also shows the manual URL.
                code = await received
        except TimeoutError:
            raise ValueError("浏览器授权超时。请重新运行 /login。") from None
        finally:
            received.cancel()
            server.shutdown()
    if code is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("https://openrouter.ai/api/v1/auth/keys", json={
                "code": code, "code_verifier": verifier, "code_challenge_method": "S256",
            })
            response.raise_for_status()
            credential = response.json()["key"]
            if valid_secret(credential):
                return credential
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass
    raise ValueError("OpenRouter 授权兑换失败。请重新运行 /login。")
