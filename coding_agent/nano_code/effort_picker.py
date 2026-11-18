"""Interactive reasoning-effort picker with a slider."""

import asyncio
import colorsys
import shutil
import time

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from .model_picker import effort_choices, picker_style

EFFORT_COLORS = {
    "auto": "#fbbf24",
    "low": "#4ade80",
    "high": "#c084fc",
}


def effort_color(level, now=None):
    if level == "max":
        hue = ((time.monotonic() if now is None else now) * 0.35) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.75, 1.0)
        return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"
    return EFFORT_COLORS.get(level, "#b8c4e0")


def _slider_fragments(session, selected, width):
    choices = effort_choices(session, session.config.data["model"])
    inner = max(20, width)
    slot = max(1, inner // len(choices))
    positions = [min(inner - 1, i * slot + slot // 2) for i in range(len(choices))]
    left, right = "Faster", "Smarter"
    gap = max(1, inner - len(left) - len(right))
    fragments = [("class:picker.desc", left + " " * gap + right + "\n")]

    marker = positions[selected]
    color = effort_color(choices[selected])
    fragments.append(("class:picker.desc", "─" * marker))
    fragments.append((f"class:picker.item {color} bold", "▲"))
    fragments.append(("class:picker.desc", "─" * (inner - marker - 1) + "\n"))

    cursor = 0
    for index, label in enumerate(choices):
        start = max(cursor, positions[index] - len(label) // 2)
        if start > cursor:
            fragments.append(("class:picker.item", " " * (start - cursor)))
        style = f"class:picker.item {effort_color(label)} bold" if index == selected else "class:picker.desc"
        fragments.append((style, label))
        cursor = start + len(label)
    fragments.append(("class:picker.item", " " * max(0, inner - cursor) + "\n"))
    return fragments


def _footer_text():
    return [("class:picker.hint", "\n←/→ 调整 · Enter 应用 · Esc 取消\n")]


async def pick_effort(session, *, width=None):
    """Show the effort slider and return the chosen level or None."""
    choices = effort_choices(session, session.config.data["model"])
    if len(choices) <= 1:
        return None
    current = session.config.data.get("effort") or "auto"
    selected = choices.index(current) if current in choices else 0
    state = {"selected": selected}
    size = shutil.get_terminal_size(fallback=(80, 24))
    columns = width or size.columns
    dialog_width = min(columns, max(40, min(88, columns - 12)))
    left_pad = max(0, (columns - dialog_width) // 2)
    dialog_height = 8

    bindings = KeyBindings()

    @bindings.add("left")
    def previous(event):
        state["selected"] = (state["selected"] - 1) % len(choices)
        event.app.invalidate()

    @bindings.add("right")
    def following(event):
        state["selected"] = (state["selected"] + 1) % len(choices)
        event.app.invalidate()

    for number in range(1, 10):
        @bindings.add(str(number))
        def choose(event, number=number):
            if number <= len(choices):
                state["selected"] = number - 1
                event.app.invalidate()

    @bindings.add("enter")
    def apply(event):
        event.app.exit(result=choices[state["selected"]])

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    control = FormattedTextControl(
        lambda: _slider_fragments(session, state["selected"], dialog_width - 2),
        key_bindings=bindings,
        focusable=True,
        show_cursor=False,
    )
    body = HSplit([
        Window(control, height=3, style="class:picker.panel"),
        Window(FormattedTextControl(lambda: [("class:picker.panel", "\n")]), height=1, style="class:picker.panel"),
        Window(FormattedTextControl(_footer_text), height=2, style="class:picker.panel"),
    ])
    dialog = Frame(
        body=body, title="推理强度", style="class:picker.panel",
        width=Dimension.exact(dialog_width), height=Dimension.exact(dialog_height),
    )
    root = VSplit([Window(width=left_pad), dialog, Window()], height=Dimension.exact(dialog_height))
    app = Application(
        layout=Layout(container=root),
        style=picker_style(session.config.data.get("theme") == "light"),
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )

    async def refresh():
        while True:
            await asyncio.sleep(0.08)
            app.invalidate()

    task = asyncio.create_task(refresh())
    try:
        return await app.run_async()
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        task.cancel()
