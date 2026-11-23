"""生成 Kwey 应用图标（多尺寸 .ico）。

设计：深色圆角底 + 青色 K 字骨架 + 三个节点连线（暗示知识图谱）。
用法：python tools/make_icon.py  → static/icons/kwey.ico
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (16, 20, 24, 255)
ACCENT = (76, 194, 176, 255)
DIM = (76, 194, 176, 90)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> Image.Image:
    scale = 4  # 超采样后缩小，边缘更干净
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BG)

    # 节点连线（图谱意象）：三个圆点 + 两条线
    dots = [(0.26, 0.28), (0.74, 0.30), (0.70, 0.74)]
    for a, b in ((0, 1), (1, 2)):
        d.line(
            [dots[a][0] * s, dots[a][1] * s, dots[b][0] * s, dots[b][1] * s],
            fill=DIM, width=max(1, int(s * 0.012)),
        )
    for x, y in dots:
        r = s * 0.035
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=DIM)

    # K：竖笔 + 两斜笔（用多边形描边，避免字体依赖）
    w = s * 0.115                    # 笔画宽度
    top, bottom = s * 0.24, s * 0.80
    left = s * 0.30
    d.rounded_rectangle([left - w / 2, top, left + w / 2, bottom], radius=w / 2, fill=ACCENT)
    # 上斜：从竖笔中部到右上
    d.line([left, s * 0.56, s * 0.70, s * 0.30], fill=ACCENT, width=int(w))
    # 下斜：从竖笔中部到右下
    d.line([left, s * 0.56, s * 0.72, s * 0.78], fill=ACCENT, width=int(w))
    # 转折点补圆，避免斜线与竖笔出现缺口
    r = w / 2
    d.ellipse([left - r, s * 0.56 - r, left + r, s * 0.56 + r], fill=ACCENT)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "static" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    ico = out / "kwey.ico"
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    frames[-1].save(out / "kwey.png", format="PNG")
    print(f"written {ico} ({ico.stat().st_size} bytes) + kwey.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
