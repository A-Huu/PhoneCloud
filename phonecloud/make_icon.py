"""把页面上的蓝色云画成 Windows 图标。打包时生成，不增加运行时依赖。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_BLUE = (37, 99, 235, 255)
_WHITE = (255, 255, 255, 255)


def _draw(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    radius = 14 * scale
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=_BLUE)

    def blob(cx: float, cy: float, rx: float, ry: float) -> None:
        draw.ellipse(
            ((cx - rx) * scale, (cy - ry) * scale, (cx + rx) * scale, (cy + ry) * scale),
            fill=_WHITE,
        )

    blob(28, 35, 9, 7.5)
    blob(40, 30, 12, 10)
    blob(50, 35, 8, 6.5)
    draw.rectangle((21 * scale, 35 * scale, 50 * scale, 44 * scale), fill=_WHITE)

    width = max(2, round(3 * scale))
    draw.line((32 * scale, 40 * scale, 32 * scale, 30 * scale), fill=_BLUE, width=width)
    draw.line(
        (27.5 * scale, 34 * scale, 32 * scale, 29.5 * scale, 36.5 * scale, 34 * scale),
        fill=_BLUE,
        width=width,
        joint="curve",
    )
    return image


def write_icon(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = _draw(256)
    image.save(
        path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
    )
