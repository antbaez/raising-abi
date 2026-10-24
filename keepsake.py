"""The end-of-experience keepsake: one tall image with the two segmented faces,
the baby portrait, and every age's image and caption, sized for a phone.
Drawn with Pillow; qrcode draws the QR code for the phone link.
"""

import io

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1080
MARGIN = 56
GAP = 28
COLUMNS = 2
RADIUS = 22
BG = "#08080d"
SURFACE = "#1a1a25"
TEXT = "#f1f5f9"
MUTED = "#a3abbd"
GOOD = "#4ade80"
BAD = "#f87171"
ACCENT = "#ec4899"

# macOS system fonts, close to the app's Lora (captions) and Inter (labels).
FONT_FILES = {
    "serif": "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "serif-bold": "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "serif-italic": "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
    "sans": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "sans-bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_FILES[kind], size)
    except OSError:
        return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def _rounded(image: Image.Image, size: tuple[int, int], contain: bool = False) -> Image.Image:
    """`image` cropped (or, with `contain`, letterboxed) to `size`, with rounded corners."""
    if contain:
        # Segmented faces sit on black; trim it so the face fills the tile.
        image = image.convert("RGB")
        image = image.crop(image.convert("L").point(lambda v: 255 if v > 8 else 0).getbbox() or None)
        tile = Image.new("RGB", size, "black")
        fitted = ImageOps.contain(image, (size[0] * 9 // 10, size[1] * 9 // 10), Image.LANCZOS)
        tile.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    else:
        tile = ImageOps.fit(image.convert("RGB"), size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), RADIUS, fill=255)
    tile.putalpha(mask)
    return tile


def build(
    faces: list[Image.Image],
    baby: Image.Image,
    ages: list[dict],
    parent_names: tuple[str, str] = ("", ""),
    child_name: str = "",
) -> Image.Image:
    """`ages` is a list of {"age", "choice", "caption", "image"} in order."""
    child = child_name or "your child"
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_font, names_font = _font("serif-bold", 68), _font("sans", 30)
    label_font, small_font = _font("sans-bold", 24), _font("sans", 22)
    caption_font = _font("serif", 24)

    # Top row: parent, parent, baby.
    top_w = (WIDTH - 2 * MARGIN - 2 * GAP) // 3
    top_h = top_w * 4 // 3
    # Age grid: 3:4 tiles like the generated images, captions below.
    tile_w = (WIDTH - 2 * MARGIN - (COLUMNS - 1) * GAP) // COLUMNS
    tile_h = tile_w * 4 // 3
    pad = 22
    line_h = 34
    wrapped = [_wrap(measure, a["caption"], caption_font, tile_w - 2 * pad) for a in ages]
    rows = [range(i, min(i + COLUMNS, len(ages))) for i in range(0, len(ages), COLUMNS)]
    row_heights = [tile_h + pad + 30 + 10 + max(len(wrapped[i]) for i in row) * line_h + pad for row in rows]

    height = MARGIN + 80 + 46 + 40 + top_h + 14 + 30 + 56 + sum(row_heights) + GAP * (len(rows) - 1) + 40 + 30 + MARGIN
    out = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(out)

    y = MARGIN
    draw.text((WIDTH / 2, y), f"Raising {child}", font=title_font, fill=TEXT, anchor="mt")
    y += 80
    names = " & ".join(n for n in parent_names if n)
    if names:
        draw.text((WIDTH / 2, y), names, font=names_font, fill=MUTED, anchor="mt")
    y += 46 + 40

    labels = [parent_names[0] or "Parent A", parent_names[1] or "Parent B", f"Baby {child_name}".strip()]
    for i, (image, label) in enumerate(zip([*faces, baby], labels)):
        x = MARGIN + i * (top_w + GAP)
        tile = _rounded(image, (top_w, top_h), contain=i < 2)
        out.paste(tile, (x, y), tile)
        draw.text((x + top_w / 2, y + top_h + 14), label, font=small_font, fill=MUTED, anchor="mt")
    y += top_h + 14 + 30 + 56

    for row, row_h in zip(rows, row_heights):
        for col, i in enumerate(row):
            a = ages[i]
            x = MARGIN + col * (tile_w + GAP)
            draw.rounded_rectangle((x, y, x + tile_w, y + row_h), RADIUS, fill=SURFACE)
            tile = _rounded(a["image"], (tile_w, tile_h))
            out.paste(tile, (x, y), tile)
            ty = y + tile_h + pad
            draw.text((x + pad, ty), f"AGE {a['age']}", font=label_font, fill=TEXT)
            if a.get("choice") in ("good", "bad"):
                good = a["choice"] == "good"
                draw.text((x + tile_w - pad, ty), "GOOD PATH" if good else "BAD PATH",
                          font=label_font, fill=GOOD if good else BAD, anchor="ra")
            ty += 30 + 10
            for line in wrapped[i]:
                draw.text((x + pad, ty), line, font=caption_font, fill=TEXT)
                ty += line_h
        y += row_h + GAP
    y += 40 - GAP

    draw.text((WIDTH / 2, y), "Raising ABI", font=_font("serif-italic", 26), fill=ACCENT, anchor="mt")
    return out


def qr_png(url: str) -> bytes:
    """A PNG QR code for `url` (black on white scans most reliably)."""
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()

