# -*- coding: utf-8 -*-
"""生成 64x64 与 256x256 应用图标 (纯 Python, 无依赖)"""
import math
import struct
import sys
import zlib


def write_png(path, w, h, pixels):
    # pixels: list of rows, each row list of (r,g,b,a)
    raw = b""
    for row in pixels:
        raw += b"\x00" + b"".join(struct.pack("4B", *p) for p in row)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def draw(size):
    s = size
    px = [[(0, 0, 0, 0)] * s for _ in range(s)]
    # 背景圆角方块 渐变蓝
    c1, c2 = (47, 111, 237), (23, 62, 150)
    r = int(s * 0.22)
    m = int(s * 0.04)
    cx_bg, cy_bg = s / 2, s / 2

    def in_round_rect(x, y):
        x0, y0, x1, y1 = m, m, s - m - 1, s - m - 1
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        dx = max(x0 + r - x, x - (x1 - r), 0)
        dy = max(y0 + r - y, y - (y1 - r), 0)
        return dx * dx + dy * dy <= r * r

    for y in range(s):
        for x in range(s):
            if in_round_rect(x, y):
                t = (y - m) / max(1, (s - 2 * m))
                col = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,)
                px[y][x] = col

    # 磁盘盘片
    cx, cy = s / 2, s / 2
    R = s * 0.30
    hole = s * 0.075
    for y in range(s):
        for x in range(s):
            d = math.hypot(x - cx, y - cy)
            if hole <= d <= R:
                # 盘片渐变白
                t = (d - hole) / (R - hole)
                v = int(235 + 20 * t)
                px[y][x] = (v, v, v + 6 if v + 6 < 256 else 255, 255)
            elif d < hole:
                px[y][x] = (28, 62, 128, 255)
    # 数据弧线
    for ang in range(0, 360, 1):
        for rr, col in ((R * 0.82, (60, 120, 235)), (R * 0.66, (110, 165, 245))):
            for a0 in (30, 150, 270):
                a = math.radians(ang + a0)
                x = int(cx + rr * math.cos(a))
                y = int(cy + rr * math.sin(a))
                if 0 <= x < s and 0 <= y < s:
                    cpx = px[y][x]
                    if cpx[0] > 200:  # 只画在盘片上
                        px[y][x] = col + (255,)
    return px


for size, name in ((64, "ICON.PNG"), (256, "ICON_256.PNG")):
    write_png(name, size, size, draw(size))
    print("wrote", name)
for size, name in ((64, "app/ui/images/icon-64.png"), (256, "app/ui/images/icon-256.png")):
    write_png(name, size, size, draw(size))
    print("wrote", name)
sys.exit(0)
