"""The startup mascot, transcribed from the supplied pixel sprite."""

from rich.text import Text

# One sprite pixel per half cell. A dot leaves the terminal background visible.
_SPRITE = (
    ".Y.....",
    "..A....",
    "..AY...",
    "..AAY..",
    ".AOAAYW",
    "OOOOYO.",
    ".SOECD.",
    ".SPOWW.",
    ".OOWWW.",
    ".OOWWW.",
    ".OOOAY.",
)
_COLORS = {
    "Y": "#ffb92f",  # golden highlights
    "A": "#f9a02a",  # hat and robe
    "O": "#f47b28",  # brim and robe shadows
    "S": "#ae4c25",  # deep orange shadow
    "W": "#fff8f1",  # hat edge and beard
    "E": "#443437",  # eye and hair
    "C": "#ffcfab",  # face
    "D": "#24293d",  # eye shadow
    "P": "#ed9869",  # cheek
}


def pixel_cells(pixels, palette, *, color=True):
    """Yield rows of half-block cells as (glyph, foreground, background)."""
    for index in range(0, len(pixels), 2):
        top = pixels[index]
        bottom = pixels[index + 1] if index + 1 < len(pixels) else "." * len(top)
        row = []
        for upper, lower in zip(top, bottom):
            foreground, background = palette.get(upper), palette.get(lower)
            if foreground and background and color:
                row.append(("▀", foreground, background))
            elif foreground and background:
                row.append(("█", None, None))
            elif foreground:
                row.append(("▀", foreground if color else None, None))
            elif background:
                row.append(("▄", background if color else None, None))
            else:
                row.append((" ", None, None))
        yield row


def mascot(*, color: bool = True) -> Text:
    """Draw the startup character without setting the terminal canvas color."""
    art = Text(no_wrap=True, overflow="crop")
    for index, row in enumerate(pixel_cells(_SPRITE, _COLORS, color=color)):
        if index:
            art.append("\n")
        for glyph, foreground, background in row:
            style = f"{foreground} on {background}" if background else foreground
            art.append(glyph, style)
    return art
