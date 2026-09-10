#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库，无需 Pillow）。

图案：深夜蓝渐变圆角方块 + 仪表盘（轨道弧 / 进度弧 / 指针），
与状态监视面板的「午夜蓝」主题呼应。输出：
  ICON.PNG / ICON_256.PNG（FPK 根目录）
  app/ui/images/icon-64.png / icon-256.png（桌面图标）
"""

import math
import os
import struct
import zlib

SS = 4            # 超采样倍率
BASE = 256 * SS   # 高分辨率画布（一次性渲染，双重降采样）

BG_TOP = (0x16, 0x2e, 0x5c)
BG_BOT = (0x0a, 0x17, 0x33)
GLOW = (0x2f, 0x7e, 0xf0)
ACCENT = (0x38, 0xbd, 0xf8)
WHITE = (0xf5, 0xf8, 0xff)


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path, size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    print("  写出 %s (%dx%d)" % (path, size, size))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def add(base, other, alpha):
    """src-over 叠加，返回新元组。"""
    if alpha <= 0:
        return base
    if alpha >= 1:
        return other
    return tuple(int(base[i] * (1 - alpha) + other[i] * alpha) for i in range(3))


def rounded_rect_mask(x, y, n, inset, radius):
    """圆角矩形 SDF：内部返回 1，外部 0（超采样下即抗锯齿）。

    距离 = |p - clamp(p, 内缩矩形)| - r，为圆角矩形的标准距离场。"""
    lo, hi = inset, n - inset
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    d = math.hypot(x - cx, y - cy) - radius
    return 1 if d <= 0 else 0


def render_base():
    n = BASE
    cx = cy = n / 2
    inset = n * 0.015
    radius = n * 0.24
    R = n * 0.40           # 表盘半径
    stroke = n * 0.058     # 弧线宽度
    hub = n * 0.062
    needle_len = R * 0.66
    needle_w = n * 0.030
    tick_w = n * 0.012

    a0, a1 = math.radians(135), math.radians(405)   # 240° 表盘
    needle_a = a0 + (a1 - a0) * 0.72
    glow_cx, glow_cy = cx, cy + R * 0.35
    glow_sigma = n * 0.42

    img = bytearray(n * n * 4)

    def put(i, color):
        img[i] = color[0]
        img[i + 1] = color[1]
        img[i + 2] = color[2]
        img[i + 3] = 255

    # --- 背板 + 辉光 ---
    for y in range(n):
        t = y / (n - 1)
        row = lerp(BG_TOP, BG_BOT, t)
        base_i = y * n * 4
        dyg = y - glow_cy
        for x in range(n):
            i = base_i + x * 4
            dxg = x - glow_cx
            g = math.exp(-(dxg * dxg + dyg * dyg) / (2 * glow_sigma * glow_sigma)) * 0.45
            put(i, add(row, GLOW, g) if g > 0.01 else row)
            # 圆角矩形以外的像素透明
            if not rounded_rect_mask(x + 0.5, y + 0.5, n, inset, radius):
                img[i + 3] = 0

    def stamp_dot(x, y, r, color, alpha):
        """在超采样画布上盖一个实心圆（用于描边）。"""
        x0, x1 = int(x - r), int(x + r) + 1
        y0, y1 = int(y - r), int(y + r) + 1
        r2 = r * r
        for yy in range(max(0, y0), min(n, y1)):
            ddy = yy + 0.5 - y
            span = math.sqrt(max(0.0, r2 - ddy * ddy))
            base_i = yy * n * 4
            for xx in range(max(0, x0), min(n, x1)):
                if abs(xx + 0.5 - x) <= span:
                    i = base_i + xx * 4
                    if img[i + 3]:
                        put(i, add((img[i], img[i + 1], img[i + 2]), color, alpha))

    def arc(a_from, a_to, radius, width, color, alpha):
        steps = max(8, int(abs(a_to - a_from) * radius / (width / 4)))
        r = width / 2
        for s in range(steps + 1):
            a = a_from + (a_to - a_from) * s / steps
            stamp_dot(cx + math.cos(a) * radius,
                      cy + math.sin(a) * radius, r, color, alpha)

    # --- 轨道弧 ---
    arc(a0, a1, R, stroke, WHITE, 0.28)
    # --- 进度弧（到指针处）---
    arc(a0, needle_a, R, stroke, ACCENT, 0.95)
    # --- 刻度 ---
    for k in range(5):
        a = a0 + (a1 - a0) * k / 4
        arc(a, a, R * 0.88, tick_w * 2, WHITE, 0.5)  # 点刻度
        arc(a, a, R * 1.10, tick_w * 2, WHITE, 0.35)
    # --- 指针 ---
    arc(needle_a, needle_a, needle_len / 2, needle_w * 2, WHITE, 0.98)
    stamp_dot(cx + math.cos(needle_a) * needle_len,
              cy + math.sin(needle_a) * needle_len, needle_w * 1.4, WHITE, 0.98)
    # --- 中轴 ---
    stamp_dot(cx, cy, hub, WHITE, 1.0)
    stamp_dot(cx, cy, hub * 0.45, ACCENT, 1.0)

    return img


def downsample(img, src, dst):
    f = src // dst
    out = bytearray(dst * dst * 4)
    for y in range(dst):
        for x in range(dst):
            r = g = b = a = 0
            for yy in range(f):
                base = ((y * f + yy) * src + x * f) * 4
                for xx in range(f):
                    i = base + xx * 4
                    r += img[i]
                    g += img[i + 1]
                    b += img[i + 2]
                    a += img[i + 3]
            cnt = f * f
            j = (y * dst + x) * 4
            out[j] = r // cnt
            out[j + 1] = g // cnt
            out[j + 2] = b // cnt
            out[j + 3] = a // cnt
    return out


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    print("渲染图标（%dx%d 超采样）…" % (BASE, BASE))
    img = render_base()
    p256 = downsample(img, BASE, 256)
    p64 = downsample(img, BASE, 64)
    write_png("ICON_256.PNG", 256, p256)
    write_png("ICON.PNG", 64, p64)
    os.makedirs("app/ui/images", exist_ok=True)
    write_png("app/ui/images/icon-256.png", 256, p256)
    write_png("app/ui/images/icon-64.png", 64, p64)
    print("图标生成完成。")


if __name__ == "__main__":
    main()
