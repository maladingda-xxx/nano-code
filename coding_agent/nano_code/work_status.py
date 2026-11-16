"""Fixed-width animations and per-turn terminal status."""

from collections.abc import Callable
from functools import cache
from time import monotonic
from typing import NamedTuple

from prompt_toolkit.utils import get_cwidth

from .mascot import pixel_cells

# One-cell-high thumbnail of Items_54.png. The mouthpiece sits above the bell.
_TRUMPET = ("OA.", ".AA")
_TRUMPET_TILT = ("OAA", ".AA")
_PALETTE = {"O": "#f87722", "A": "#ffad34"}
_FRAME_INTERVAL = 0.25
_NOTES = ("♪", "♫", "♪", "·", " ", " ")
_BUBBLES = (".", "∘", "°", "°", "·", " ")
_LIQUID_LEVELS = ("▃", "▄", "▅", "▄", "▃", "▂")

Frame = tuple[tuple[str, str], ...]


class _Animation(NamedTuple):
    label: str
    draw: Callable[[int, bool], Frame]
    narrow: tuple[str, ...]


@cache
def trumpet_frame(frame, color):
    """Return three half-block cells and one fixed note slot."""
    pixels = _TRUMPET_TILT if frame in (1, 2) else _TRUMPET
    fragments = []
    for block, fg, bg in next(pixel_cells(pixels, _PALETTE, color=color)):
        style = f"{fg} bg:{bg}" if bg else fg or ""
        fragments.append((style, block))
    fragments.append(("class:work.note", _NOTES[frame]))
    return tuple(fragments)


@cache
def potion_frame(frame, color):
    """Draw the Items_7.png bottle with a rippling purple surface and rising bubble."""
    # The narrower neck and pale glass shoulders preserve the silhouette in one row.
    return (
        ("#c0ccdc" if color else "", "▅"),
        ("#d482ff bg:#ffffff" if color else "", _LIQUID_LEVELS[frame] if color else "█"),
        ("#ffffff" if color else "", "▅"),
        ("#d482ff" if color else "", _BUBBLES[frame]),
    )


_ANIMATIONS = (
    _Animation("composing···", trumpet_frame, ("♪", "♪", "♪", "·", "·", "·")),
    _Animation("brewing···", potion_frame, _BUBBLES),
)


def _clip(text, width):
    """Keep the text in one row, including double-width terminal characters."""
    end = 0
    for char in text:
        width -= get_cwidth(char)
        if width < 0:
            break
        end += 1
    return text[:end]


def _duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _tokens(number):
    if number is None:
        return "—"
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)


class WorkStatus:
    def __init__(self):
        self.started = None
        self._animation_index = -1
        self.stage = "thinking"
        self.effort = None
        self.output_tokens = None
        self.cancelling = False

    @property
    def active(self):
        return self.started is not None

    def start(self, submitted):
        self.started = submitted
        self._animation_index = (self._animation_index + 1) % len(_ANIMATIONS)
        self.stage = "thinking"
        self.effort = None
        self.output_tokens = None
        self.cancelling = False

    def stop(self):
        self.started = None
        self.cancelling = False

    def cancel(self):
        self.cancelling = self.active

    def event(self, event):
        kind = event["type"]
        if kind == "turn_end":
            self.stop()
        elif not self.active:
            return
        elif kind == "model_start":
            self.stage, self.effort = "thinking", None
        elif kind == "model_request":
            self.stage, self.effort = "thinking", event.get("effort")
        elif kind == "text_delta":
            self.stage = "streaming"
        elif kind == "tool_request":
            self.stage, self.effort = event["call"]["function"]["name"], None
        elif kind == "tool_result":
            self.stage = "thinking"
        elif kind == "usage" or (kind == "subagent_event" and event.get("event_type") == "usage"):
            count = event.get("output_tokens")
            if count is not None:
                self.output_tokens = (self.output_tokens or 0) + count

    def fragments(self, width, *, color=True, waiting=None, now=None):
        if waiting:
            return [("class:work.label", _clip(waiting, width))]
        if self.cancelling:
            return [("class:work.label", _clip("正在取消…", width))]
        if not self.active:
            return []
        seconds = max(0, (monotonic() if now is None else now) - self.started)
        elapsed = _duration(seconds)
        animation = _ANIMATIONS[self._animation_index]
        label = animation.label
        frame = int(seconds / _FRAME_INTERVAL) % len(animation.narrow)
        icon = animation.draw(frame, color)
        minimum = get_cwidth(f"{label} {elapsed}")
        if minimum + len(icon) + 1 > width:
            icon = ((icon[-1][0], animation.narrow[frame]),)
        if minimum + len(icon) + 1 > width:
            icon = ()
        if icon:
            icon = (*icon, ("", " "))
        room = width - len(icon)
        parts = [elapsed, f"↓ {_tokens(self.output_tokens)} tokens", (f"{self.effort} effort" if self.effort else self.stage)]
        for length in (3, 2, 1):
            details = f" ({' · '.join(parts[:length])})"
            if get_cwidth(label + details) <= room:
                break
        else:
            details = f" {elapsed}"
            if get_cwidth(label + details) > room:
                details = ""
        return [*icon, ("class:work.label", _clip(label, room)), ("class:work.detail", details)]
