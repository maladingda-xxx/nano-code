"""Interactive provider login using the Nano Code terminal theme."""

import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app_session
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, TextArea

from .model_picker import picker_style
from .provider_login import openrouter_login, prepare_login
from .provider_profiles import PROVIDERS, azure_endpoint, valid_model_id, valid_secret


def cancel_bindings(result=None):
    bindings = KeyBindings()

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=result)

    return bindings


async def choose(title, choices, *, light, rows, searchable=False):
    search = TextArea(height=1, multiline=False, prompt="搜索  ", style="class:picker.panel")
    selected = 0

    def matches():
        query = search.text.strip().casefold()
        return [item for item in choices if query in f"{item[0]} {item[1]}".casefold()]

    def reset(_):
        nonlocal selected
        selected = 0

    search.buffer.on_text_changed += reset
    bindings = cancel_bindings()

    @bindings.add("up")
    def previous(event):
        nonlocal selected
        selected = max(0, selected - 1)

    @bindings.add("down")
    def following(event):
        nonlocal selected
        selected = min(selected + 1, max(0, len(matches()) - 1))

    @bindings.add("enter")
    def apply(event):
        visible = matches()
        if visible:
            event.app.exit(result=visible[selected][0])

    count = max(1, min(len(choices), 9, rows - 5))

    def fragments():
        matching = matches()
        if not matching:
            return [("class:picker.hint", "没有匹配的供应商")]
        start = max(0, min(selected - count // 2, len(matching) - count))
        result = []
        for index, (_, label, status) in enumerate(matching[start:start + count], start):
            focused = index == selected
            result.append(("class:picker.selected" if focused else "class:picker.item",
                           ("❯ " if focused else "  ") + label))
            if status:
                result.append(("class:picker.hint", " · " + status))
            result.append(("", "\n"))
        return result

    options = FormattedTextControl(fragments, focusable=True, show_cursor=False)
    body = [Window(options, height=count, style="class:picker.panel"),
            Window(FormattedTextControl([("class:picker.hint", "↑/↓ 选择 · Enter 确认 · Esc 取消")]),
                   height=1, style="class:picker.panel")]
    if searchable:
        body.insert(0, search)
    app = Application(
        layout=Layout(Frame(HSplit(body), title=title), focused_element=search if searchable else options),
        key_bindings=bindings, style=picker_style(light), erase_when_done=True,
    )
    try:
        return await app.run_async()
    except (EOFError, KeyboardInterrupt):
        return None


async def _prompt(label, *, light, secret=False):
    bindings = cancel_bindings()
    prompt = PromptSession(
        is_password=secret, history=DummyHistory(), key_bindings=bindings, style=picker_style(light),
        # PromptSession's fallback for TERM=dumb echoes keystrokes. An explicit
        # output keeps password processing enabled across terminal types.
        output=get_app_session().output, erase_when_done=secret,
    )
    try:
        value = await prompt.prompt_async([("class:picker.title", label + "  ")])
        return value.strip() if value is not None else None
    except (EOFError, KeyboardInterrupt):
        return None


async def _wait_for_provider(title, message, operation, *, light):
    app = None

    def update(text):
        nonlocal message
        message = text
        app.invalidate()

    async def run():
        try:
            result = await operation(update)
            if not app.is_done:
                app.exit(result=result)
        except ValueError as error:
            if not app.is_done:
                app.exit(exception=error)

    body = Window(FormattedTextControl(lambda: message, focusable=True, show_cursor=False),
                  wrap_lines=True, height=Dimension(min=2, preferred=8), style="class:picker.panel")
    app = Application(
        layout=Layout(Frame(body, title=title)),
        key_bindings=cancel_bindings(), style=picker_style(light), erase_when_done=True,
    )
    try:
        return await app.run_async(pre_run=lambda: app.create_background_task(run()))
    except (EOFError, KeyboardInterrupt):
        return None


async def _browser_key(light):
    async def authorize(update):
        return await openrouter_login(lambda url: update(
            f"请在浏览器授权。如果浏览器未打开，请手动访问\n{url}\nEsc / Ctrl+C 取消"
        ))

    return await _wait_for_provider("登录 OpenRouter", "正在打开 OpenRouter 授权页面…", authorize, light=light)


async def configure_provider(session, *, rows):
    light = session.config.data["theme"] == "light"
    method = await choose("选择登录方式", [
        ("browser", "浏览器账号登录", ""),
        ("api", "API Key / Token", ""),
    ], light=light, rows=rows)
    if method is None:
        return None
    configured = {login.provider for login in session.config.logins.configured()}
    provider = await choose("选择模型供应商", [
        (key, entry.label, "已配置" if key in configured else "未配置")
        for key, entry in PROVIDERS.items() if method == "api" or entry.browser_login
    ], light=light, rows=rows, searchable=True)
    if provider is None:
        return None
    label = PROVIDERS[provider].label
    if method == "browser":
        credential = await _browser_key(light)
    else:
        credential = await _prompt(f"{label} API Key / Token（输入隐藏）", light=light, secret=True)
    if credential is None:
        return None
    if not valid_secret(credential):
        raise ValueError("请输入有效的 API Key 或 Token。")
    fields = {}
    if provider == "azure":
        endpoint = await _prompt("Azure OpenAI HTTPS 资源地址", light=light)
        if endpoint is None:
            return None
        fields["endpoint"] = azure_endpoint(endpoint)
        deployment = await _prompt("Azure OpenAI 部署名称", light=light)
        if deployment is None:
            return None
        if not valid_model_id(deployment):
            raise ValueError("请输入无空白的 Azure OpenAI 部署名称。")
        fields["deployment"] = deployment
    result = await _wait_for_provider(f"连接 {label}", "正在检查凭据…\nEsc / Ctrl+C 取消",
                                      lambda _: prepare_login(provider, credential, **fields), light=light)
    if result is None:
        return None
    login, verified = result
    session.save_login(login)
    return label, verified
