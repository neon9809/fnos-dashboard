#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neon-dash 模组开发预览器 —— 在本地实时模拟显示器渲染效果（无需真机）。

与 fb_render.py 共用同一套 Renderer/画布/主题代码，预览即实机效果。

用法：
  python3 neon-dash/preview.py            # http://127.0.0.1:8188
  可选 --port / --api（fnOS 后端地址，填了则系统页用实机数据）

需要 PIL（pip3 install --user pillow）。
"""

import argparse
import base64
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "bin"))
import fb_render as fr  # noqa: E402  复用渲染器与主题

W, H = 1280, 800

try:
    from PIL import Image  # noqa
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ---------------- 演示数据（与 fnOS 实机结构一致） ----------------
GB = 2 ** 30

DEMO_STATS = {
    "host": {
        "hostname": "Homeland", "os": "Debian GNU/Linux 12 (bookworm)",
        "kernel": "6.12.18.c1032-trim", "arch": "x86_64",
        "cpu_model": "Intel(R) Core(TM) i5-4590 CPU @ 3.30GHz",
        "procs": 394, "uptime": 50400,
    },
    "cpu": {"usage": 23.5, "cores": [35, 18, 42, 8, 61, 12, 25, 30],
            "core_count": 8, "load": [0.86, 0.72, 0.64]},
    "memory": {"total": 16642998272, "used": 3478e6, "available": 13165e6,
               "usage": 20.9, "swap_total": 0, "swap_used": 0},
    "disks": [
        {"mount": "/vol1", "dev": "/dev/mapper/trim-0", "fs": "btrfs",
         "total": 931 * GB, "used": 233 * GB, "usage": 25.0, "ro": False},
        {"mount": "/vol2", "dev": "/dev/mapper/trim-1", "fs": "ext4",
         "total": 158 * GB, "used": 83 * GB, "usage": 52.7, "ro": False},
        {"mount": "/", "dev": "/dev/nvme0n1p2", "fs": "ext4",
         "total": 62.6 * GB, "used": 19.5 * GB, "usage": 31.2, "ro": False},
    ],
    "net": {"ifaces": [
        {"name": "enp6s0", "rx": 324e6, "tx": 19.9e6,
         "rx_rate": 184612, "tx_rate": 8454},
        {"name": "eno1", "rx": 0, "tx": 0, "rx_rate": 0, "tx_rate": 0},
    ]},
    "temps": {"sensors": [
        {"label": "Core 0", "chip": "coretemp", "celsius": 40.0},
        {"label": "Core 1", "chip": "coretemp", "celsius": 39.0},
        {"label": "Package id 0", "chip": "coretemp", "celsius": 43.0},
        {"label": "Composite", "chip": "nvme", "celsius": 39.9},
        {"label": "acpitz", "chip": "acpitz", "celsius": 27.8},
    ], "cpu_temp": 41.0},
}


def _calendar_payload():
    import calendar as _cal
    today = datetime.date.today()
    first_wd, days = _cal.monthrange(today.year, today.month)
    weeks, week = [], [None] * first_wd
    for d in range(1, days + 1):
        week.append({"d": d, "today": d == today.day})
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    wd = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[first_wd]
    return {"year": today.year, "month": today.month, "day": today.day,
            "weekday": wd, "weeks": weeks}


DEMO_WEATHER = {
    "provider": "qweather",
    "city": "北京",
    "hourly": [
        {"time": "14:00", "temp": 26, "text": "晴", "humidity": 40, "pop": 0},
        {"time": "15:00", "temp": 27, "text": "多云", "humidity": 42, "pop": 0},
        {"time": "16:00", "temp": 27, "text": "多云", "humidity": 45, "pop": 10},
        {"time": "17:00", "temp": 26, "text": "阴", "humidity": 48, "pop": 20},
        {"time": "18:00", "temp": 25, "text": "阴", "humidity": 52, "pop": 30},
        {"time": "19:00", "temp": 24, "text": "小雨", "humidity": 60, "pop": 60},
    ],
    "indices": [
        {"type": "7", "name": "花粉过敏指数", "level": "3", "category": "较易发",
         "text": "天气条件易诱发过敏，易过敏人群应减少外出，外出请佩戴口罩。"},
        {"type": "5", "name": "紫外线指数", "level": "3", "category": "中等",
         "text": "紫外线中等强度，建议涂擦防晒护肤品。"},
        {"type": "9", "name": "感冒指数", "level": "2", "category": "较易发",
         "text": "昼夜温差较大，较易发生感冒。"},
    ],
    "error": "",
}

DEMO_CODING = {"name": "GLM Coding Plan", "ok": True, "used": 123.4,
               "total": 500.0, "reset": "2026-10-01", "error": ""}

DEMO_PAYLOAD = {
    "title": "示例模组",
    "subtitle": "由 .neon-dash 包提供",
    "lines": [["包 ID", "mock"], ["版本", "1.0.0"],
              ["自定义计数", "3"], ["当前时间", "12:00:00"]],
    "bars": [["模拟负载", 42], ["示例进度", 76]],
    "text": "编辑左侧 payload，右侧实时渲染",
}

DEFAULT_CODE = '''"""自定义模组：实现 get_payload(config) 即可，详见 MODULE_SPEC.md"""
import time


def get_payload(config: dict) -> dict:
    return {
        "title": "我的模组",
        "subtitle": "get_payload 返回的数据决定页面内容",
        "lines": [
            ["示例键", "示例值"],
            ["配置项", str(config.get("count", "-"))],
            ["当前时间", time.strftime("%H:%M:%S")],
        ],
        "bars": [["进度", 60]],
        "text": "保存后自动渲染，与实机完全一致",
    }
'''


def run_code(code, config):
    """执行开发者的 get_payload 代码，返回 (payload, error)。"""
    if not HAS_PIL:
        return None, "本地缺少 PIL"
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(code)
            path = f.name
        spec = importlib.util.spec_from_file_location("nd_dev_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "get_payload"):
            return None, "缺少 get_payload(config) 函数"
        payload = mod.get_payload(dict(config or {}))
        if not isinstance(payload, dict):
            return None, "get_payload 应返回 dict"
        return payload, ""
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def render_page(req):
    """按请求渲染一帧，返回 (png_bytes, payload_out, err)。"""
    page = str(req.get("page", "custom"))
    theme = str(req.get("theme", "midnight"))
    try:
        rotate = int(req.get("rotate", 0))
    except (TypeError, ValueError):
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        rotate = 0
    try:
        inches = float(req.get("inches") or 0)
    except (TypeError, ValueError):
        inches = 0.0

    pal = {k: fr.hex_rgb(v)
           for k, v in fr.PALETTES.get(theme, fr.PALETTES["midnight"]).items()}
    cw, ch = (H, W) if rotate in (90, 270) else (W, H)
    canvas = fr.PilCanvas(cw, ch)
    res_scale = min(cw / 1280.0, ch / 800.0)
    if inches > 0:
        ppi = (W * W + H * H) ** 0.5 / inches
        ui = max(0.75, min(2.2, ppi / 150.0)) * max(0.8, min(1.6, res_scale))
    else:
        ui = max(0.6, res_scale)

    canvas.begin(pal["bg"])
    r = fr.Renderer(canvas, cw, ch, ui)
    now = datetime.datetime.now()

    payload_out, err = None, ""
    page_name = ""
    if page == "system":
        r.page_system(DEMO_STATS, pal)
    elif page == "calendar":
        r.page_calendar(_calendar_payload(), pal, False)
        page_name = "日历"
    elif page == "weather":
        p = req.get("payload")
        if isinstance(p, dict) and ("hourly" in p or "current" in p
                                    or p.get("provider")):
            payload_out = p
        else:
            payload_out = dict(DEMO_WEATHER)
        r.page_weather(payload_out, pal, False)
        page_name = "天气预报"
    elif page == "coding":
        p = req.get("payload")
        if isinstance(p, dict) and ("ok" in p or p.get("name")):
            payload_out = p
        else:
            payload_out = dict(DEMO_CODING)
        r.page_coding(payload_out, pal)
        page_name = "Coding Plan"
    else:  # custom：payload 或代码
        if req.get("code"):
            payload_out, err = run_code(req.get("code"), req.get("config") or {})
        else:
            payload_out = req.get("payload") \
                if isinstance(req.get("payload"), dict) else dict(DEMO_PAYLOAD)
        if err:
            payload_out = {"title": "渲染错误", "error": err}
        if not isinstance(payload_out, dict):
            payload_out = {"title": "渲染错误", "error": "payload 不是 dict"}
        r.page_ext(payload_out, pal)
        page_name = str(payload_out.get("title") or "自定义模组")[:16]

    r.header(pal, DEMO_STATS["host"]["hostname"], now, False,
             page_name=page_name)
    r.footer(pal, [page], 0, int(req.get("rotate_seconds", 15) or 15))

    buf = io.BytesIO()
    canvas.img.save(buf, "PNG")
    return buf.getvalue(), payload_out, err


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/preview"):
            with open(os.path.join(HERE, "preview.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        self._send(404, {"ok": False, "error": "Not Found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/render":
            self._send(404, {"ok": False, "error": "Not Found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError
        except (OSError, ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "无效请求"})
            return
        if not HAS_PIL:
            self._send(500, {"ok": False,
                             "error": "本地缺少 PIL：pip3 install --user pillow"})
            return
        try:
            png, payload_out, err = render_page(req)
        except Exception as e:
            self._send(500, {"ok": False,
                             "error": "渲染失败：%s: %s" % (type(e).__name__, e)})
            return
        self._send(200, {
            "ok": True,
            "png": base64.b64encode(png).decode("ascii"),
            "payload": payload_out,
            "error": err,
        })


def main():
    ap = argparse.ArgumentParser(description="neon-dash 模组开发预览器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8188)
    args = ap.parse_args()
    if not HAS_PIL:
        print("错误：本地缺少 PIL（pip3 install --user pillow）", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("[ndash-preview] http://%s:%d  （Ctrl+C 退出）"
          % (args.host, args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
