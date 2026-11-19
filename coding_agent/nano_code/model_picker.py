"""Interactive model picker styled like the terminal UI."""

import shutil

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from .provider_profiles import PROVIDERS
from .providers import bundled_models

MAX_ROWS = 10


def model_note(session, model):
    provider, model_id = session.split_model_choice(model)
    if provider in PROVIDERS:
        return PROVIDERS[provider].label
    legacy = session.config.legacy_model_settings
    if model_id in bundled_models(legacy):
        return "DeepSeek · 现有配置"
    if model == session.current_model_choice:
        return "当前模型"
    return "现有配置"


def effort_choices(session, model):
    return session.model_efforts(model)


def normalize_effort(session, model, effort):
    choices = effort_choices(session, model)
    return effort if effort in choices else "auto"


def _model_window(models, selected, max_rows):
    if len(models) <= max_rows:
        return models, 0, models
    start = max(0, min(selected - max_rows // 2, len(models) - max_rows))
    return models, start, models[start:start + max_rows]


def model_picker_text(session, selected, max_rows=MAX_ROWS, *, choices=None):
    """Return formatted rows for tests and the picker control."""
    models, start, visible = _model_window(
        list(session.model_choices()) if choices is None else choices, selected, max_rows,
    )
    if not models:
        return [("class:picker.hint", "没有匹配的模型")]
    current = session.current_model_choice
    width = min(44, max(len(session.split_model_choice(model)[1]) for model in visible) + 2)
    fragments = []
    for offset, model in enumerate(visible):
        index = start + offset
        is_selected = index == selected
        is_current = model == current
        marker = "❯ " if is_selected else "  "
        style = "class:picker.selected" if is_selected else (
            "class:picker.current" if is_current else "class:picker.item"
        )
        desc_style = "class:picker.selected-desc" if is_selected else "class:picker.desc"
        _, name = session.split_model_choice(model)
        if len(name) > width:
            name = name[:width - 1] + "…"
        fragments.append((style, f"{marker}{index + 1:>2}. {name:<{width}}"))
        fragments.append((desc_style, f" {model_note(session, model)}"))
        if is_current:
            fragments.append(("class:picker.check", " ✓"))
        fragments.append(("", "\n"))
    return fragments


def _header_text():
    return [
        ("class:picker.subtitle", "选择仅影响当前会话；新会话使用启动时的模型。\n"),
        ("class:picker.panel", "\n"),
    ]


def _effort_text(session, model, effort):
    label = "auto（默认）" if effort == "auto" else effort
    adjustable = len(effort_choices(session, model)) > 1
    hint = "   ←/→ 调整\n" if adjustable else "   （当前连接无可用档位）\n"
    return [
        ("class:picker.panel", "\n"),
        ("class:picker.effort", "● "),
        ("class:picker.item", f"推理强度 {label}"),
        ("class:picker.hint", hint),
    ]


def _footer_text(has_more=False, can_adjust=True):
    parts = ["↑/↓ 滚动" if has_more else "↑/↓ 选择"]
    if can_adjust:
        parts.append("←/→ 调整强度")
    parts.extend(["/ 搜索", "1–9 快速选择", "Enter 应用", "Esc 取消"])
    return [("class:picker.hint", "\n" + " · ".join(parts) + "\n")]


def picker_style(light):
    if light:
        return Style.from_dict({
            "picker.panel": "bg:#f4f7ff #22304d",
            "picker.title": "bg:#f4f7ff #1e3a8a bold",
            "picker.subtitle": "bg:#f4f7ff #5b6b85",
            "picker.item": "bg:#f4f7ff #22304d",
            "picker.selected": "bg:#f4f7ff #0b255e bold",
            "picker.current": "bg:#f4f7ff #1e3a8a",
            "picker.desc": "bg:#f4f7ff #5b6b85",
            "picker.selected-desc": "bg:#f4f7ff #1e3a8a",
            "picker.check": "bg:#f4f7ff #15803d bold",
            "picker.effort": "bg:#f4f7ff #15803d",
            "picker.hint": "bg:#f4f7ff #5b6b85",
            "frame": "bg:#f4f7ff",
            "frame.border": "bg:#f4f7ff #b8c7e6",
            "frame.label": "bg:#f4f7ff #1e3a8a bold",
        })
    return Style.from_dict({
        "picker.panel": "bg:#0b1224 #b8c4e0",
        "picker.title": "bg:#0b1224 #c7d2fe bold",
        "picker.subtitle": "bg:#0b1224 #8494b4",
        "picker.item": "bg:#0b1224 #b8c4e0",
        "picker.selected": "bg:#0b1224 #e0e7ff bold",
        "picker.current": "bg:#0b1224 #8aa4ff",
        "picker.desc": "bg:#0b1224 #8494b4",
        "picker.selected-desc": "bg:#0b1224 #c7d2fe",
        "picker.check": "bg:#0b1224 #4ade80 bold",
        "picker.effort": "bg:#0b1224 #4ade80",
        "picker.hint": "bg:#0b1224 #8494b4",
        "frame": "bg:#0b1224",
        "frame.border": "bg:#0b1224 #2f4478",
        "frame.label": "bg:#0b1224 #c7d2fe bold",
    })


async def pick_model(session, *, width=None, height=None):
    """Show the model picker and return the chosen model or None."""
    all_models = list(session.model_choices())
    models = all_models
    current = session.current_model_choice
    selected = models.index(current) if current in models else 0
    effort = normalize_effort(session, models[selected], session.config.data.get("effort") or "auto")
    state = {"selected": selected, "effort": effort}
    size = shutil.get_terminal_size(fallback=(80, 24))
    columns = width or size.columns
    rows = height or size.lines
    max_rows = max(1, min(MAX_ROWS, rows - 10))
    _, _, visible = _model_window(models, selected, max_rows)
    list_lines = len(visible)
    dialog_height = list_lines + 9
    dialog_width = min(columns, max(40, min(88, columns - 12)))
    left_pad = max(0, (columns - dialog_width) // 2)
    bindings = KeyBindings()
    row_bindings = KeyBindings()
    search = TextArea(height=1, multiline=False, prompt="搜索  ", style="class:picker.panel")
    searching = False
    search_labels = [f"{model} {model_note(session, model)}".casefold() for model in all_models]

    def update_filter(_):
        nonlocal models
        query = search.text.strip().casefold()
        models = [model for model, label in zip(all_models, search_labels) if query in label]
        state.update(selected=0, effort="auto")

    search.buffer.on_text_changed += update_filter

    @row_bindings.add("/")
    def start_search(event):
        nonlocal searching
        searching = True
        event.app.layout.focus(search)

    def move(delta):
        if models:
            state["selected"] = max(0, min(len(models) - 1, state["selected"] + delta))
            state["effort"] = normalize_effort(session, models[state["selected"]], state["effort"])

    @bindings.add("up")
    @row_bindings.add("k")
    def up(event):
        move(-1)

    @bindings.add("down")
    @row_bindings.add("j")
    def down(event):
        move(1)

    for number in range(1, 10):
        @row_bindings.add(str(number))
        def choose(event, number=number):
            move(number - 1 - state["selected"])

    def shift_effort(delta):
        choices = effort_choices(session, models[state["selected"]])
        if len(choices) <= 1:
            return
        index = choices.index(state["effort"]) if state["effort"] in choices else 0
        state["effort"] = choices[(index + delta) % len(choices)]

    @row_bindings.add("left")
    def left(event):
        shift_effort(-1)
        event.app.invalidate()

    @row_bindings.add("right")
    def right(event):
        shift_effort(1)
        event.app.invalidate()

    @bindings.add("enter")
    def apply(event):
        if models:
            event.app.exit(result={"model": models[state["selected"]], "effort": state["effort"]})

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    control = FormattedTextControl(
        lambda: model_picker_text(session, state["selected"], max_rows, choices=models),
        key_bindings=row_bindings,
        focusable=True,
        show_cursor=False,
    )
    body = HSplit([
        Window(FormattedTextControl(_header_text), height=2, style="class:picker.panel"),
        ConditionalContainer(search, Condition(lambda: searching)),
        Window(control, height=list_lines, style="class:picker.panel"),
        Window(FormattedTextControl(lambda: _effort_text(session, models[state["selected"]], state["effort"]) if models else []),
               height=2, style="class:picker.panel"),
        Window(FormattedTextControl(lambda: _footer_text(
            len(models) > max_rows, bool(models) and len(effort_choices(session, models[state["selected"]])) > 1,
        )), height=2, style="class:picker.panel"),
    ])
    dialog = Frame(
        body=body, title="选择模型", style="class:picker.panel",
        width=Dimension.exact(dialog_width), height=Dimension.exact(dialog_height),
    )
    root = VSplit([Window(width=left_pad), dialog, Window()], height=Dimension.exact(dialog_height))
    app = Application(
        layout=Layout(container=root, focused_element=control),
        key_bindings=bindings,
        style=picker_style(session.config.data.get("theme") == "light"),
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    try:
        return await app.run_async()
    except (EOFError, KeyboardInterrupt):
        return None
