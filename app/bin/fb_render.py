#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 显示器（framebuffer）渲染器。

参考 lcdsimple 的实现原理：从后端拉取 JSON 快照，直接绘制到 /dev/fb0。
- 布局按 fb 实际分辨率缩放；配置了屏幕尺寸(英寸)时按 PPI 估算缩放倍率
- 所有页面均针对单屏设计；启用模组后按「轮换间隔」自动翻页轮换
- 字体：优先 PIL + 系统 CJK 字体（DroidSansFallback），缺失时退回内置 5x7 ASCII 字体
"""

import argparse
import datetime
import json
import mmap
import os
import sys
import threading
import time
import urllib.request

API_STATUS = "http://127.0.0.1:8199/api/status"
API_MODULES = "http://127.0.0.1:8199/api/modules"

# 与 Web 端 style.css 保持一致的 6 套主题调色板
PALETTES = {
    "midnight": {"bg": "#0a1322", "text": "#e8eefb", "dim": "#93a5c4",
                 "accent": "#3b82f6", "warn": "#f59e0b", "danger": "#f87171",
                 "down": "#34d399", "up": "#fbbf24"},
    "graphite": {"bg": "#0e1013", "text": "#e7eaf0", "dim": "#9aa3b2",
                 "accent": "#2dd4bf", "warn": "#f59e0b", "danger": "#fb7185",
                 "down": "#4ade80", "up": "#fbbf24"},
    "emerald":  {"bg": "#04231a", "text": "#e4f7ee", "dim": "#8fc4ad",
                 "accent": "#34d399", "warn": "#fbbf24", "danger": "#fb7185",
                 "down": "#5eead4", "up": "#fcd34d"},
    "solar":    {"bg": "#1c1204", "text": "#fdf3e0", "dim": "#c9a878",
                 "accent": "#f59e0b", "warn": "#fbbf24", "danger": "#f87171",
                 "down": "#4ade80", "up": "#60a5fa"},
    "sakura":   {"bg": "#fdf1f6", "text": "#402a35", "dim": "#a3798c",
                 "accent": "#ec4899", "warn": "#d97706", "danger": "#e11d48",
                 "down": "#10b981", "up": "#8b5cf6"},
    "light":    {"bg": "#f1f4f9", "text": "#1e293b", "dim": "#64748b",
                 "accent": "#2563eb", "warn": "#d97706", "danger": "#dc2626",
                 "down": "#059669", "up": "#d97706"},
}

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAY_EN = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WEEKDAY_ZH_SHORT = ("一", "二", "三", "四", "五", "六", "日")
WMO_TEXT = {
    0: "晴", 1: "多云", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛雨", 53: "毛雨", 55: "大毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "霰",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}
WMO_EN = {0: "SUNNY", 1: "M.CLD", 2: "P.CLD", 3: "CLOUDY", 45: "FOG", 48: "FOG",
          51: "DRIZ", 53: "DRIZ", 55: "DRIZ", 61: "RAIN", 63: "RAIN", 65: "RAIN",
          66: "SLEET", 67: "SLEET", 71: "SNOW", 73: "SNOW", 75: "SNOW", 77: "SNOW",
          80: "SHWR", 81: "SHWR", 82: "SHWR", 85: "SNOW", 86: "SNOW",
          95: "STORM", 96: "STORM", 99: "STORM"}

# 内置 5x7 ASCII 字体（公有领域字形数据），用于无 PIL 环境的回退渲染
FONT5X7 = {
    ' ': (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
    '0': (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    '1': (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    '2': (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    '3': (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    '4': (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    '5': (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    '6': (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    '7': (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    '8': (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    '9': (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    'A': (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    'B': (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    'C': (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    'D': (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    'E': (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    'F': (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    'G': (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    'H': (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    'I': (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    'K': (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    'L': (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    'M': (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    'N': (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    'O': (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    'P': (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    'R': (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    'S': (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    'T': (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    'U': (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    'V': (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    'W': (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    'Y': (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    'Z': (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    '%': (0x19, 0x1A, 0x02, 0x04, 0x08, 0x0B, 0x13),
    '.': (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C),
    ',': (0x00, 0x00, 0x00, 0x00, 0x0C, 0x04, 0x08),
    ':': (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00),
    '-': (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    '+': (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00),
    '/': (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    '(': (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ')': (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    '^': (0x04, 0x0E, 0x15, 0x04, 0x04, 0x00, 0x00),
    'v': (0x00, 0x00, 0x04, 0x04, 0x15, 0x0E, 0x04),
    'G': (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    'J': (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    'Q': (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    'X': (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
}


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def blend(c1, c2, t):
    """在 RGB 画布上模拟半透明：c1 为主色，向 c2 混合 t。"""
    return tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))


# --------------------------------------------------------------------------
# 帧缓冲设备
# --------------------------------------------------------------------------
class FB:
    def __init__(self, path="/dev/fb0"):
        base = os.path.join("/sys/class/graphics",
                            os.path.basename(os.path.realpath(path)))
        vs = open(os.path.join(base, "virtual_size")).read().strip()
        self.w, self.h = (int(x) for x in vs.split(",")[:2])
        bpp = int(open(os.path.join(base, "bits_per_pixel")).read().strip())
        if bpp != 32:
            raise RuntimeError("仅支持 32bpp 帧缓冲，当前 %dbpp" % bpp)
        stride_file = os.path.join(base, "stride")
        self.stride = max(self.w * 4,
                          int(open(stride_file).read().strip())
                          if os.path.exists(stride_file) else self.w * 4)
        self.fd = os.open(path, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.stride * self.h)
        self._row = self.w * 4

    def blit(self, bgra):
        """bgra: W*H*4，按行写入（处理 stride 对齐）。"""
        if self.stride == self._row:
            self.mm[0:self._row * self.h] = bgra[:self._row * self.h]
            return
        for y in range(self.h):
            off = y * self.stride
            self.mm[off:off + self._row] = \
                bgra[y * self._row:(y + 1) * self._row]

    def close(self):
        try:
            self.mm.close()
            os.close(self.fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 绘图画布（PIL 后端：支持 CJK；ASCII 后端：内置 5x7 字体）
# --------------------------------------------------------------------------
FONT_LATIN_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
)
FONT_CJK_CANDIDATES = (
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


class PilCanvas:
    name = "pil"

    def __init__(self, w, h):
        from PIL import Image, ImageDraw, ImageFont
        self._Image, self._ImageFont, self._ImageDraw = Image, ImageFont, ImageDraw
        self._font_cache = {}
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w, h), (0, 0, 0))
        self.draw = ImageDraw.Draw(self.img)
        latin = next((p for p in FONT_LATIN_CANDIDATES if os.path.exists(p)), None)
        cjk = next((p for p in FONT_CJK_CANDIDATES if os.path.exists(p)), None)
        if not latin and not cjk:
            raise RuntimeError("未找到可用的系统字体")
        self.latin_path = latin or cjk
        self.cjk_path = cjk or latin

    def _font(self, size, cjk=False):
        size = max(8, int(size))
        key = (size, cjk)
        f = self._font_cache.get(key)
        if f is None:
            f = self._ImageFont.truetype(
                self.cjk_path if cjk else self.latin_path, size)
            self._font_cache[key] = f
        return f

    @staticmethod
    def _runs(s):
        """按 ASCII / 非 ASCII 分段，为各段选择合适字体。"""
        runs = []
        cur, cur_ascii = "", None
        for ch in str(s):
            a = ord(ch) < 128
            if cur_ascii is None or a == cur_ascii:
                cur += ch
                cur_ascii = a
            else:
                runs.append((cur, cur_ascii))
                cur, cur_ascii = ch, a
        if cur:
            runs.append((cur, cur_ascii))
        return runs

    def begin(self, bg):
        self.draw.rectangle([0, 0, self.w, self.h], fill=bg)

    def text(self, x, y, s, size, color, anchor="la"):
        runs = self._runs(s)
        widths = [self.draw.textlength(t, font=self._font(size, not a))
                  for t, a in runs]
        total = sum(widths)
        if anchor[0] == "r":
            x -= total
        elif anchor[0] in ("c", "m"):
            x -= total / 2
        va = anchor[1] if len(anchor) > 1 else "a"
        for (t, is_ascii), w in zip(runs, widths):
            self.draw.text((x, y), t, font=self._font(size, not is_ascii),
                           fill=color, anchor="l" + va)
            x += w

    def text_w(self, s, size):
        scale = max(1, int(round(size / 14.0)))
        return len(str(s)) * 6 * scale

    def text_centered(self, cx, cy, s, size, color):
        """按 ink 边界精确居中（圆内数字等对齐敏感场景）。

        textlength/textbbox 都是排版框，"1" 等字形的墨迹并不居中——
        渲染到临时图层量真实墨迹边界后再定位。"""
        s = str(s)
        f = self._font(size, cjk=any(ord(ch) > 127 for ch in s))
        pad = max(8, int(size))
        tmp = self._Image.new("RGBA", (int(size) * max(1, len(s)) + pad * 2,
                                       int(size) * 2 + pad * 2), (0, 0, 0, 0))
        self._ImageDraw.Draw(tmp).text((pad, pad), s, font=f, fill=color)
        bbox = tmp.getbbox()          # 真实墨迹边界
        if not bbox:
            return
        self.draw.text((cx - (bbox[0] + bbox[2]) / 2 + pad,
                        cy - (bbox[1] + bbox[3]) / 2 + pad),
                       s, font=f, fill=color)

    def ink_center_offset(self, s, size):
        """数字按字框(anchor mm)绘制时，墨迹质心相对字框中心的偏移 (dx, dy)。

        用于让圆圈/底色对齐数字的视觉中心，同时保持与邻列数字共轴。"""
        s = str(s)
        f = self._font(size, cjk=any(ord(ch) > 127 for ch in s))
        pad = max(8, int(size))
        tmp = self._Image.new("RGBA", (int(size) * max(1, len(s)) + pad * 2,
                                       int(size) * 2 + pad * 2), (0, 0, 0, 0))
        self._ImageDraw.Draw(tmp).text((pad, pad), s, font=f, fill=(255, 255, 255))
        bbox = tmp.getbbox()
        if not bbox:
            return 0.0, 0.0
        adv = self.draw.textlength(s, font=f)
        asc, desc = f.getmetrics()
        acx = pad + adv / 2                     # 字框(anchor mm)水平中心
        acy = pad + (asc + desc) / 2            # 垂直 "m" 中心相对 ascender 顶
        return (bbox[0] + bbox[2]) / 2 - acx, (bbox[1] + bbox[3]) / 2 - acy

    def rect(self, x, y, w, h, color, radius=0, outline=None):
        if radius > 0:
            try:
                self.draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                                            fill=color, outline=outline, width=2)
                return
            except AttributeError:
                pass
        self.draw.rectangle([x, y, x + w, y + h], fill=color, outline=outline)

    def line(self, x1, y1, x2, y2, color, width=2):
        self.draw.line([x1, y1, x2, y2], fill=color, width=width)

    def ellipse(self, x, y, w, h, color, outline=None):
        self.draw.ellipse([x, y, x + w, y + h], fill=color, outline=outline)

    def to_bgra(self, rotate=0):
        img = self.img
        if rotate:
            # Pillow ≥10 用 Transpose 枚举，旧版用顶层常量
            rot = getattr(self._Image, "Transpose", self._Image)
            angle = {90: rot.ROTATE_90, 180: rot.ROTATE_180, 270: rot.ROTATE_270}.get(rotate)
            if angle is not None:
                img = img.transpose(angle)
        return img.convert("RGBA").tobytes("raw", "BGRA")


class AsciiCanvas:
    """无 PIL 时的回退画布：仅 ASCII，整数倍放大 5x7 字形。"""
    name = "ascii"

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 4)
        self._bg = None

    @staticmethod
    def _clamp(s):
        out = []
        for ch in s:
            if ch in FONT5X7:
                out.append(ch)
            elif ch.upper() in FONT5X7:
                out.append(ch.upper())
            else:
                out.append('?')
        return "".join(out)

    def begin(self, bg):
        b, g, r = bg
        self._bg = bytes((b, g, r, 255)) * (self.w * self.h)
        self.buf[:] = self._bg

    def _px(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.buf[i:i + 4] = bytes((color[2], color[1], color[0], 255))

    def rect(self, x, y, w, h, color, radius=0, outline=None):
        for yy in range(max(0, int(y)), min(self.h, int(y + h))):
            row = yy * self.w
            for xx in range(max(0, int(x)), min(self.w, int(x + w))):
                i = (row + xx) * 4
                self.buf[i:i + 4] = bytes((color[2], color[1], color[0], 255))

    def line(self, x1, y1, x2, y2, color, width=2):
        steps = max(abs(x2 - x1), abs(y2 - y1)) + 1
        for s in range(steps):
            x = x1 + (x2 - x1) * s // steps
            y = y1 + (y2 - y1) * s // steps
            self.rect(x, y, width, width, color)

    def ellipse(self, x, y, w, h, color, outline=None):
        cx, cy = x + w / 2, y + h / 2
        for yy in range(int(y), int(y + h)):
            for xx in range(int(x), int(x + w)):
                if ((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2 <= 1:
                    self._px(xx, yy, color)

    def text(self, x, y, s, size, color, anchor="la"):
        s = self._clamp(str(s))
        scale = max(1, int(round(size / 14.0)))
        adv = 6 * scale
        total = len(s) * adv
        if anchor[0] == "r":
            x -= total
        elif anchor[0] == "c":
            x -= total // 2
        if anchor[1] == "m":
            y -= 4 * scale
        elif anchor[1] == "b":
            y -= 8 * scale
        for ch in s:
            rows = FONT5X7.get(ch)
            if rows:
                for ry, bits in enumerate(rows):
                    for rx in range(5):
                        if bits & (0x10 >> rx):
                            self.rect(x + rx * scale, y + ry * scale,
                                      scale, scale, color)
            x += adv

    def text_w(self, s, size):
        scale = max(1, int(round(size / 14.0)))
        return len(str(s)) * 6 * scale

    def to_bgra(self, rotate=0):
        if rotate:
            print("[fb] ASCII 回退模式不支持旋转，已忽略", flush=True)
        return bytes(self.buf)


# --------------------------------------------------------------------------
# 页面绘制（基准设计 1280x800，按 ui_scale 缩放）
# --------------------------------------------------------------------------
class Renderer:
    def __init__(self, canvas, W, H, scale):
        self.c = canvas
        self.W, self.H = W, H
        self.s = scale

    def header(self, pal, host, now, offline, page_name=""):
        s = self.s
        c = self.c
        c.text(24 * s, 22 * s, str(host)[:24], 30 * s, pal["dim"])
        clock = now.strftime("%H:%M:%S")
        c.text(self.W - 24 * s, 16 * s, clock, 40 * s, pal["text"], anchor="rt")
        if page_name:
            c.text(self.W / 2, 30 * s, page_name, 26 * s, pal["dim"], anchor="ma")
        c.line(24 * s, 76 * s, self.W - 24 * s, 76 * s, pal["accent"], 2)
        if offline:
            c.text(self.W - 24 * s, 88 * s, "OFFLINE", 20 * s,
                   pal["danger"], anchor="rt")

    def big_pct(self, x, y, val, label, pal, size=96):
        s = self.s
        c = self.c
        if val is None:
            col, txt = pal["dim"], "--"
        else:
            col = pal["accent"] if val < 60 else \
                (pal["warn"] if val < 85 else pal["danger"])
            txt = "%.1f" % val
        c.text(x, y, txt, size * s, col)
        w = c.text_w(txt, size * s)
        c.text(x + w + 6 * s, y + size * s * 0.72, "%", 30 * s, pal["dim"])
        c.text(x, y - 34 * s, label, 22 * s, pal["dim"])

    def bar(self, x, y, w, h, frac, color, track):
        s = self.s
        c = self.c
        c.rect(x, y, w, h, track, radius=h // 2)
        if frac > 0.005:
            c.rect(x, y, max(h, int(w * min(1.0, frac))), h, color, radius=h // 2)

    def page_system(self, st, pal):
        s = self.s
        c = self.c
        W, H = self.W, self.H
        track = blend(pal["bg"], pal["dim"], 0.35)

        cpu = st.get("cpu") or {}
        mem = st.get("memory") or {}
        half = (W - 72 * s) / 2
        lx, rx = 36 * s, 36 * s + half + 24 * s

        # --- CPU / 内存 ---
        self.big_pct(lx, 120 * s, cpu.get("usage"), "CPU", pal, size=88)
        cores = cpu.get("cores") or []
        if cores:
            cw = 26 * s
            gap = 6 * s
            bx = lx
            for u in cores[:12]:
                bh = int(52 * s * min(1.0, u / 100.0))
                c.rect(bx, 300 * s - bh, cw, bh, pal["accent"], radius=3 * s)
                bx += cw + gap
        load = cpu.get("load") or [0, 0, 0]
        c.text(lx, 322 * s, "LOAD %.2f %.2f %.2f" % tuple(load[:3]), 22 * s, pal["dim"])

        self.big_pct(rx, 120 * s,
                     (mem.get("usage") if mem.get("total") else None),
                     "MEMORY", pal, size=88)
        self.bar(rx, 280 * s, half - 40 * s, 16 * s,
                 (mem.get("used", 0) / mem["total"]) if mem.get("total") else 0,
                 pal["accent"], track)
        gb = 2 ** 30
        c.text(rx, 316 * s, "%.1fG / %.1fG" % (mem.get("used", 0) / gb,
                                               mem.get("total", 0) / gb),
               22 * s, pal["dim"])

        # --- 存储 ---
        y = 388 * s
        c.text(lx, y, "DISK", 24 * s, pal["dim"])
        y += 38 * s
        for d in (st.get("disks") or [])[:3]:
            frac = d.get("usage", 0) / 100.0
            col = pal["accent"] if d["usage"] < 85 else pal["danger"]
            c.text(lx, y + 2 * s, d["mount"], 24 * s, pal["text"])
            self.bar(lx + 140 * s, y, W - lx * 2 - 300 * s, 14 * s, frac, col, track)
            c.text(W - lx, y + 2 * s, "%d%%" % d.get("usage", 0), 24 * s,
                   pal["text"], anchor="rt")
            y += 44 * s

        # --- 网络 / 温度 ---
        y = max(y + 26 * s, 592 * s)
        net = (st.get("net") or {}).get("ifaces") or []
        down = sum(i.get("rx_rate", 0) for i in net[:1])
        up = sum(i.get("tx_rate", 0) for i in net[:1])
        iface = net[0]["name"] if net else "--"
        c.text(lx, y, "NET %s" % iface, 24 * s, pal["dim"])
        c.text(lx, y + 42 * s, "v %s/s" % human_bytes(down), 34 * s, pal["down"])
        c.text(lx, y + 94 * s, "^ %s/s" % human_bytes(up), 34 * s, pal["up"])

        temps = (st.get("temps") or {})
        cpu_t = temps.get("cpu_temp")
        sensors = temps.get("sensors") or []
        ty = y
        c.text(rx, ty, "TEMP", 24 * s, pal["dim"])
        tcol = pal["accent"] if (cpu_t or 0) < 60 else \
            (pal["warn"] if (cpu_t or 0) < 80 else pal["danger"])
        c.text(rx, ty + 40 * s, "%.1fC" % cpu_t if cpu_t is not None else "--",
               42 * s, tcol)
        chip = sensors[0]["label"] if sensors else ""
        c.text(rx + c.text_w("%.1fC" % cpu_t if cpu_t is not None else "--",
                             42 * s) + 14 * s,
               ty + 56 * s, chip[:12], 20 * s, pal["dim"])

    def page_calendar(self, cal, pal, ascii_mode=False):
        s = self.s
        c = self.c
        W = self.W
        lx = 36 * s
        c.text(lx, 120 * s, "%04d-%02d" % (cal["year"], cal["month"]), 64 * s,
               pal["text"])
        c.text(lx, 220 * s, "%02d" % cal["day"], 150 * s, pal["accent"])
        c.text(lx, 420 * s, cal.get("weekday", ""), 44 * s, pal["dim"])

        # 月历网格（右侧）
        gx = W * 0.44
        gw = W - gx - 36 * s
        cell = gw / 7.0
        gy = 130 * s
        header = WEEKDAY_EN if ascii_mode else WEEKDAY_ZH_SHORT
        for i, wd in enumerate(header):
            c.text(gx + cell * i + cell / 2, gy, wd, 24 * s, pal["dim"], anchor="ma")
        gy += 44 * s
        for row in cal.get("weeks") or []:
            for i, cellitem in enumerate(row):
                if not cellitem:
                    continue
                cx = gx + cell * i + cell / 2
                cy = gy + 40 * s
                if cellitem.get("today"):
                    # 数字与其他日期同样字框居中保持共轴，圆圈跟随数字墨迹中心
                    dx, dy = c.ink_center_offset(str(cellitem["d"]), 30 * s)
                    c.ellipse(cx - 26 * s + dx, cy - 26 * s + dy, 52 * s, 52 * s,
                              pal["accent"])
                    c.text(cx, cy, str(cellitem["d"]), 30 * s, pal["bg"],
                           anchor="mm")
                else:
                    c.text(cx, cy, str(cellitem["d"]), 30 * s, pal["text"],
                           anchor="mm")
            gy += 66 * s

    def page_weather(self, w, pal, ascii_mode):
        """和风天气布局：当前小时大字 + 逐小时列表 + 生活指数（过敏指数高亮）。"""
        s = self.s
        c = self.c
        W = self.W
        lx = 36 * s
        track = blend(pal["bg"], pal["dim"], 0.35)
        c.text(lx, 110 * s, str(w.get("city", ""))[:12], 44 * s, pal["text"])
        if w.get("error"):
            c.text(lx, 180 * s, "ERROR: %s" % w["error"][:30], 24 * s,
                   pal["danger"])
            return
        hourly = w.get("hourly") or []
        if hourly:
            cur = hourly[0]
            c.text(lx, 190 * s, "NOW %s" % cur.get("time", ""), 22 * s,
                   pal["dim"])
            c.text(lx, 230 * s, "%d" % cur.get("temp", 0), 150 * s,
                   pal["accent"])
            c.text(lx, 410 * s, str(cur.get("text", ""))[:8], 36 * s,
                   pal["text"])
            extra = []
            if cur.get("humidity") is not None:
                extra.append("湿度 %d%%" % cur["humidity"])
            if cur.get("pop"):
                extra.append("降水 %d%%" % cur["pop"])
            c.text(lx, 470 * s, "  ".join(extra)[:24], 22 * s, pal["dim"])
        # 右侧逐小时列表
        rx = W * 0.55
        c.text(rx, 190 * s, "逐小时预报" if not isinstance(self.c, AsciiCanvas)
               else "HOURLY", 24 * s, pal["dim"])
        y = 236 * s
        for h in hourly[1:6]:
            c.text(rx, y, h.get("time", ""), 24 * s, pal["dim"])
            c.text(rx + 110 * s, y, str(h.get("text", ""))[:4], 24 * s,
                   pal["text"])
            c.text(W - lx, y, "%d°" % h.get("temp", 0), 26 * s, pal["text"],
                   anchor="rt")
            y += 46 * s
        # 底部生活指数
        iy = max(520 * s, y + 20 * s)
        c.text(lx, iy, "生活指数", 24 * s, pal["dim"])
        iy += 40 * s
        for idx in (w.get("indices") or [])[:3]:
            allergy = str(idx.get("type")) == "7"
            c.text(lx, iy, str(idx.get("name", ""))[:10], 24 * s, pal["dim"])
            col = pal["accent"] if allergy else pal["text"]
            c.text(W - lx, iy, str(idx.get("category", ""))[:6], 30 * s, col,
                   anchor="rt")
            if allergy and idx.get("text"):
                c.text(lx, iy + 36 * s, idx["text"][:40], 19 * s, pal["dim"])
                iy += 30 * s
            iy += 52 * s

    def page_coding(self, m, pal):
        s = self.s
        c = self.c
        W = self.W
        lx = 36 * s
        track = blend(pal["bg"], pal["dim"], 0.35)
        c.text(lx, 120 * s, str(m.get("name", ""))[:24], 44 * s, pal["text"])
        if not m.get("ok"):
            c.text(lx, 210 * s, m.get("error") or "NOT CONFIGURED", 28 * s,
                   pal["danger"])
            return
        used = m.get("used")
        total = m.get("total")
        c.text(lx, 260 * s, human_num(used), 120 * s, pal["accent"])
        if total:
            c.text(lx, 420 * s, "/ %s" % human_num(total), 44 * s, pal["dim"])
            frac = max(0.0, min(1.0, used / total)) if total else 0
            col = pal["accent"] if frac < 0.8 else pal["warn"]
            self.bar(lx, 500 * s, W - lx * 2, 24 * s, frac, col, track)
            c.text(W - lx, 550 * s, "%.1f%%" % (frac * 100), 36 * s,
                   pal["text"], anchor="rt")
        if m.get("reset"):
            c.text(lx, 560 * s, "RESET %s" % m["reset"], 26 * s, pal["dim"])

    def page_ext(self, payload, pal):
        """扩展模组通用布局：title/subtitle/lines/bars/text。"""
        s = self.s
        c = self.c
        W = self.W
        lx = 36 * s
        track = blend(pal["bg"], pal["dim"], 0.35)
        if payload.get("error"):
            c.text(lx, 120 * s, str(payload.get("title", "模组"))[:20], 44 * s,
                   pal["text"])
            c.text(lx, 200 * s, "ERROR: %s" % payload["error"], 26 * s,
                   pal["danger"])
            return
        c.text(lx, 110 * s, str(payload.get("title", ""))[:24], 52 * s,
               pal["text"])
        if payload.get("subtitle"):
            c.text(lx, 190 * s, str(payload["subtitle"])[:40], 24 * s,
                   pal["dim"])
        y = 260 * s
        for item in (payload.get("lines") or [])[:8]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            c.text(lx, y, str(item[0])[:16], 26 * s, pal["dim"])
            c.text(W - lx, y, str(item[1])[:24], 26 * s, pal["text"],
                   anchor="rt")
            y += 44 * s
        for item in (payload.get("bars") or [])[:4]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                pct = max(0, min(100, float(item[1])))
            except (TypeError, ValueError):
                continue
            c.text(lx, y, str(item[0])[:16], 24 * s, pal["dim"])
            col = pal["accent"] if pct < 80 else pal["warn"]
            self.bar(lx + 180 * s, y, W - lx * 2 - 280 * s, 14 * s,
                     pct / 100.0, col, track)
            c.text(W - lx, y, "%d%%" % pct, 24 * s, pal["text"], anchor="rt")
            y += 44 * s
        text = str(payload.get("text") or "")
        if text and y < 700 * s:
            c.text(lx, min(y + 20 * s, 700 * s), text[:44], 22 * s, pal["dim"])

    def footer(self, pal, pages, idx, rotate):
        s = self.s
        c = self.c
        n = len(pages)
        total_w = n * 18 * s
        x0 = self.W / 2 - total_w / 2
        dim = blend(pal["bg"], pal["dim"], 0.65)
        for i in range(n):
            c.ellipse(x0 + i * 18 * s, self.H - 34 * s, 10 * s, 10 * s,
                      pal["accent"] if i == idx else dim)
        remain = int(rotate - (time.time() % rotate))
        c.text(self.W - 24 * s, self.H - 44 * s, "%ds" % remain, 20 * s,
               pal["dim"], anchor="rt")


def human_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.0f%s" % (n, u) if u != "B" else "%dB" % n
        n /= 1024
    return "%.1fPB" % n


def human_num(n):
    n = float(n or 0)
    for u in ("", "K", "M", "G"):
        if n < 1000:
            return "%.1f%s" % (n, u) if u else "%d" % n
        n /= 1000
    return "%.1fT" % n


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "fnos-dashboard-fb/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class Fetcher(threading.Thread):
    """后台数据拉取：天气等慢接口不会阻塞渲染主循环。"""

    def __init__(self, api, interval):
        super().__init__(daemon=True)
        self.api = api
        self.interval = max(1.0, interval)
        self.status = None
        self.modules = {}
        self.config = {}
        self.offline = True

    def run(self):
        while True:
            try:
                data = fetch_json(self.api + "/api/status", timeout=8)
                self.status = data.get("stats")
                self.config = data.get("config") or {}
                if self.config.get("modules"):
                    try:
                        self.modules = (fetch_json(self.api + "/api/modules",
                                                   timeout=20)
                                        .get("modules") or {})
                    except Exception:
                        pass  # 保留上一次成功的模组数据
                self.offline = False
            except Exception:
                self.offline = True
            time.sleep(self.interval)


def main():
    ap = argparse.ArgumentParser(description="fnOS 状态监视 显示器渲染器")
    ap.add_argument("--fb", default="/dev/fb0")
    ap.add_argument("--api", default="http://127.0.0.1:8199")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    fb = FB(args.fb)
    W, H = fb.w, fb.h
    print("[fb] %dx%d stride=%d" % (W, H, fb.stride), flush=True)

    try:
        canvas = PilCanvas(W, H)
        print("[fb] PIL 后端（支持中文）", flush=True)
    except Exception as e:
        canvas = AsciiCanvas(W, H)
        print("[fb] 回退 ASCII 后端：%s" % e, flush=True)

    fetcher = Fetcher(args.api, args.interval)
    fetcher.start()

    # PIL 画布按显示方向复用：90/270 时用竖版画布，转置后与 fb 尺寸一致
    rotate = 0
    canvas_w, canvas_h = W, H
    while True:
        now = time.time()
        status, modules, offline = fetcher.status, fetcher.modules, fetcher.offline

        cfg = fetcher.config or {}
        pal_key = cfg.get("theme", "midnight")
        pal = {k: hex_rgb(v) for k, v in PALETTES.get(pal_key, PALETTES["midnight"]).items()}

        new_rotate = cfg.get("fb_rotate") if cfg.get("fb_rotate") in (0, 90, 180, 270) else 0
        if new_rotate != rotate:
            rotate = new_rotate
            canvas_w, canvas_h = (H, W) if rotate in (90, 270) else (W, H)
            try:
                canvas = PilCanvas(canvas_w, canvas_h)
                print("[fb] 画布 %dx%d（旋转 %d°）" % (canvas_w, canvas_h, rotate),
                      flush=True)
            except Exception as e:
                canvas = AsciiCanvas(canvas_w, canvas_h)
                print("[fb] 重建画布失败：%s" % e, flush=True)

        mods = cfg.get("modules") or {}
        pages = ["system"]
        if mods.get("calendar"):
            pages.append("calendar")
        if mods.get("weather"):
            pages.append("weather")
        ext_data = modules.get("ext") if isinstance(modules.get("ext"), dict) else {}
        for ext_id in sorted(ext_data):
            pages.append("ext:" + ext_id)
        rotate_sec = max(5, int(cfg.get("rotate_seconds", 15)))
        idx = int(now / rotate_sec) % len(pages)

        # 缩放：分辨率自适应；配置了屏幕英寸时按 PPI 修正
        res_scale = min(canvas_w / 1280.0, canvas_h / 800.0)
        inches = float(cfg.get("screen_inches") or 0)
        if inches > 0:
            ppi = (W * W + H * H) ** 0.5 / inches
            ui_scale = max(0.75, min(2.2, ppi / 150.0)) * max(0.8, min(1.6, res_scale))
        else:
            ui_scale = max(0.6, res_scale)

        canvas.begin(pal["bg"])
        r = Renderer(canvas, canvas_w, canvas_h, ui_scale)
        r.header(pal, (status or {}).get("host", {}).get("hostname", "fnOS"),
                 datetime.datetime.now(), offline,
                 page_name="" if len(pages) == 1 else "%d/%d" % (idx + 1, len(pages)))
        page = pages[idx]
        if page == "system":
            r.page_system(status or {}, pal)
        elif page == "calendar" and modules.get("calendar"):
            r.page_calendar(modules["calendar"], pal, canvas.name == "ascii")
        elif page == "weather" and modules.get("weather"):
            wp = modules.get("weather") or {}
            if wp.get("hourly"):     # 和风天气结构
                r.page_weather_qweather(wp, pal)
            else:
                r.page_weather(wp, pal, canvas.name == "ascii")
        elif page == "coding" and modules.get("coding"):
            r.page_coding(modules.get("coding") or {}, pal)
        elif page.startswith("ext:"):
            payload = ext_data.get(page[4:]) or {}
            r.page_ext(payload, pal)
        r.footer(pal, pages, idx, rotate_sec)
        fb.blit(canvas.to_bgra(rotate))
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
