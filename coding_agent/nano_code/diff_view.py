"""Parse unified diffs and render them as full-width colored rows with syntax colors."""

import re
from dataclasses import dataclass, field
from functools import lru_cache

from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from rich.cells import cell_len
from rich.text import Text

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_STYLES = {
    False: {
        "add": "on #0f2a1c",
        "remove": "on #321a1f",
        "add_gutter": "on #0a2014",
        "remove_gutter": "on #281317",
        "hunk": "#8aa4ff on #0b1224 bold",
        "context": "",
        "note": "#8494b4",
        "header": "#8aa4ff bold",
        "number": "#5b6b85",
        "add_sign": "#7ee2a8 bold",
        "remove_sign": "#ff9aa6 bold",
        "context_sign": "#5b6b85",
        "syntax": "one-dark",
    },
    True: {
        "add": "on #e6ffec",
        "remove": "on #ffebe9",
        "add_gutter": "on #d3f2de",
        "remove_gutter": "on #fbdce0",
        "hunk": "#1e3a8a on #eef2ff bold",
        "context": "",
        "note": "#5b6b85",
        "header": "#1e3a8a bold",
        "number": "#6b7a99",
        "add_sign": "#146c43 bold",
        "remove_sign": "#b02a37 bold",
        "context_sign": "#6b7a99",
        "syntax": "friendly",
    },
}


@dataclass
class DiffLine:
    kind: str  # add / remove / context / hunk / note
    number: int | None
    text: str


@dataclass
class DiffFile:
    path: str = ""
    created: bool = False
    deleted: bool = False
    added: int = 0
    removed: int = 0
    lines: list[DiffLine] = field(default_factory=list)


def _clean_name(raw):
    name = raw.strip().split("\t", 1)[0]
    if len(name) > 1 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    for prefix in ("a/", "b/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def parse_diff(text):
    """Return one DiffFile per file in a unified diff; empty for other text."""
    files = []
    current = None
    old_left = new_left = 0
    old_number = new_number = 0
    rows = text.splitlines()

    for index, raw in enumerate(rows):
        line = raw.rstrip("\r")

        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                if current is not None:
                    current.lines.append(DiffLine("note", None, line))
                continue
            sign, body = (line[:1], line[1:]) if line else (" ", "")
            if sign == "-":
                if current is not None:
                    current.lines.append(DiffLine("remove", old_number or None, body))
                    current.removed += 1
                old_number += 1
                old_left = max(0, old_left - 1)
            elif sign == "+":
                if current is not None:
                    current.lines.append(DiffLine("add", new_number or None, body))
                    current.added += 1
                new_number += 1
                new_left = max(0, new_left - 1)
            else:
                if current is not None:
                    current.lines.append(DiffLine("context", new_number or None, body))
                old_number += 1
                new_number += 1
                old_left = max(0, old_left - 1)
                new_left = max(0, new_left - 1)
            continue

        if line.startswith("diff --git "):
            current = None
            continue

        if line.startswith("--- ") and index + 1 < len(rows) and rows[index + 1].startswith("+++ "):
            name = line[4:].strip()
            current = DiffFile(path="", created=name == "/dev/null")
            files.append(current)
            continue

        if line.startswith("+++ ") and current is not None and not current.path:
            name = line[4:].strip()
            if name == "/dev/null":
                current.deleted = True
            else:
                current.path = _clean_name(name)
            continue

        if line.startswith("@@ "):
            match = _HUNK.match(line)
            if current is None:
                current = DiffFile(path="?")
                files.append(current)
            current.lines.append(DiffLine("hunk", None, line))
            if match:
                old_number = int(match.group(1))
                new_number = int(match.group(3))
                old_left = int(match.group(2) or 1)
                new_left = int(match.group(4) or 1)
            continue

    return [item for item in files if item.path or item.lines]


def totals(files):
    return sum(item.added for item in files), sum(item.removed for item in files)


def file_title(item):
    verb = "Create" if item.created else "Delete" if item.deleted else "Update"
    return f"{verb}({item.path or '?'})"


@lru_cache(maxsize=4)
def _syntax_style(light):
    try:
        return get_style_by_name(_STYLES[bool(light)]["syntax"])
    except ClassNotFound:
        return get_style_by_name("default")


@lru_cache(maxsize=32)
def _lexer(path):
    try:
        return get_lexer_for_filename(path or "?")
    except ClassNotFound:
        return TextLexer()


def _token_style(style, token):
    info = style.style_for_token(token)
    parts = []
    if info["color"]:
        parts.append("#" + info["color"].lower())
    for attr in ("bold", "italic", "underline"):
        if info[attr]:
            parts.append(attr)
    return " ".join(parts)


def _token_lines(text, lexer, style):
    lines = [[]]
    for token, value in lexer.get_tokens(text):
        token_style = _token_style(style, token)
        for index, part in enumerate(value.split("\n")):
            if index:
                lines.append([])
            if part:
                lines[-1].append((part, token_style))
    return lines


def _side_tokens(item, light):
    """Lex the old and new sides so token colors keep their multi-line context."""
    text_style = _syntax_style(light)
    lexer = _lexer(item.path)
    old_lines, new_lines = [], []
    for line in item.lines:
        body = line.text.expandtabs(4)
        if line.kind == "context":
            old_lines.append(body)
            new_lines.append(body)
        elif line.kind == "add":
            new_lines.append(body)
        elif line.kind == "remove":
            old_lines.append(body)
    old_text = "\n".join(old_lines)
    new_text = "\n".join(new_lines)
    return (
        _token_lines(old_text, lexer, text_style),
        _token_lines(new_text, lexer, text_style),
    )


def files_text(files, *, light=False, max_lines=None, indent="", show_headers=False, width=None):
    """Render parsed files; returns (Text, shown lines, total lines)."""
    styles = _STYLES[bool(light)]
    total = sum(len(item.lines) for item in files)
    shown = 0
    out = Text()
    started = False

    def merge(base, extra):
        return f"{base} {extra}".strip() if base else extra

    def start_row():
        nonlocal started
        if started:
            out.append("\n")
        started = True

    def write_row(fragments, pad_style=""):
        start_row()
        used = 0
        for content, style in fragments:
            out.append(content, style=style)
            used += cell_len(content)
        if pad_style and width is not None and used < width:
            out.append(" " * (width - used), style=pad_style)

    for item in files:
        if max_lines is not None and shown >= max_lines:
            break
        if show_headers:
            start_row()
            out.append(f"{indent}{file_title(item)}  +{item.added} -{item.removed}", style=styles["header"])

        old_tokens, new_tokens = _side_tokens(item, light)
        old_index = new_index = 0
        for line in item.lines:
            if max_lines is not None and shown >= max_lines:
                break
            if line.kind == "hunk":
                write_row([(f"{indent}{line.text}", styles["hunk"])], styles["hunk"])
                shown += 1
                continue
            if line.kind == "note":
                write_row([(f"{indent}{line.text}", styles["note"])])
                shown += 1
                continue

            if line.kind == "context":
                tokens = new_tokens[new_index] if new_index < len(new_tokens) else []
                old_index += 1
                new_index += 1
            elif line.kind == "add":
                tokens = new_tokens[new_index] if new_index < len(new_tokens) else []
                new_index += 1
            else:
                tokens = old_tokens[old_index] if old_index < len(old_tokens) else []
                old_index += 1

            row_style = styles[line.kind]
            gutter_style = styles.get(f"{line.kind}_gutter", "")
            sign = "+" if line.kind == "add" else "-" if line.kind == "remove" else " "
            number = f"{line.number:>5}" if line.number is not None else "     "
            fragments = [
                (f"{indent}{number} ", merge(styles["number"], gutter_style)),
                (sign, merge(styles[f"{line.kind}_sign"], gutter_style)),
            ]
            for content, token_style in tokens:
                fragments.append((content, merge(token_style, row_style)))
            if not tokens:
                fragments.append((line.text.expandtabs(4), row_style))
            write_row(fragments, row_style)
            shown += 1

    return out, shown, total
