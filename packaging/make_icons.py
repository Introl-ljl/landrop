#!/usr/bin/env python3
"""生成应用图标（开发期工具，需要 Pillow；产物已入库，平时不用运行）。

图形与网页 favicon 相同：薄荷青圆角方块 + 深色水滴。

    python packaging/make_icons.py

产物::

    packaging/assets/landrop.png    512px，Linux 桌面图标
    packaging/assets/landrop.ico    Windows（16–256）
    packaging/assets/landrop.icns   macOS（16–1024）
    landrop/gui/icon.py             窗口图标（base64 PNG，打进 pyz 也能用）
"""
import base64
import io
import math
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "packaging", "assets")
MINT = (25, 207, 168, 255)
INK = (5, 32, 25, 255)


def cubic(p0, p1, p2, p3, n=48):
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        yield (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
               mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])


def drop_polygon():
    """favicon 的路径（40×40 视口）：M20 7.5 c… a9.3 9.3 … c… Z。"""
    pts = list(cubic((20, 7.5), (25.3, 13.7), (29.3, 18.7), (29.3, 23.5)))
    for i in range(1, 64):                         # 下半圆：右 → 底 → 左
        a = math.pi * i / 64
        pts.append((20 + 9.3 * math.cos(a), 23.5 + 9.3 * math.sin(a)))
    pts += list(cubic((10.7, 23.5), (10.7, 18.7), (14.7, 13.7), (20, 7.5)))
    return pts


def render(size: int) -> Image.Image:
    ss = 4
    big = size * ss
    k = big / 40
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=12 * k, fill=MINT)
    d.polygon([(x * k, y * k) for x, y in drop_polygon()], fill=INK)
    # 高光：以 (20,23.8) 附近为圆心的一小段弧
    w = 2.2 * k
    cx, cy, r = 20.2 * k, 23.8 * k, 5 * k
    ro = r + w / 2                                 # PIL 的 width 向内画：外框放大半个线宽
    d.arc([cx - ro, cy - ro, cx + ro, cy + ro], start=110, end=180, fill=MINT, width=round(w))
    for ang in (110, 180):                         # 圆头端点（stroke-linecap: round）
        ex, ey = cx + r * math.cos(math.radians(ang)), cy + r * math.sin(math.radians(ang))
        d.ellipse([ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2], fill=MINT)
    return img.resize((size, size), Image.LANCZOS)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main():
    os.makedirs(ASSETS, exist_ok=True)
    render(512).save(os.path.join(ASSETS, "landrop.png"), optimize=True)
    base = render(256)
    base.save(os.path.join(ASSETS, "landrop.ico"),
              sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    render(1024).save(os.path.join(ASSETS, "landrop.icns"))
    lines = ['"""窗口图标（由 packaging/make_icons.py 生成，勿手改）。"""', "", "PNG = {"]
    for s in (16, 32, 48, 64, 128):
        b64 = base64.b64encode(png_bytes(render(s))).decode()
        lines.append(f"    {s}: (")
        for i in range(0, len(b64), 96):
            lines.append(f'        "{b64[i:i + 96]}"')
        lines.append("    ),")
    lines.append("}")
    with open(os.path.join(ROOT, "landrop", "gui", "icon.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("ok")


if __name__ == "__main__":
    main()
