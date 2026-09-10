#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— HTTP 层（API + 静态页 + framebuffer 重启/预览）。

访问模型（从 dashboard.py 拆出）：
  - 设置页与全部管理接口仅限本机 127.0.0.1（CGI 反代经 fnOS 登录态校验后回源）
  - 局域网直连仅提供只读面板 / 状态接口，敏感配置自动脱敏
"""

import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler

from dash_config import APP_VERSION
from dash_modules import MODULES, calendar_payload

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def fb_raw_read():
    """读取 /dev/fb0 当前帧，返回 (w, h, bgra, stride)。"""
    base = "/sys/class/graphics/fb0"
    vs = open(os.path.join(base, "virtual_size")).read().strip()
    w, h = (int(x) for x in vs.split(",")[:2])
    bpp = int(open(os.path.join(base, "bits_per_pixel")).read().strip())
    if bpp != 32:
        raise RuntimeError("仅支持 32bpp 帧缓冲，当前 %dbpp" % bpp)
    stride_file = os.path.join(base, "stride")
    stride = max(w * 4,
                 int(open(stride_file).read().strip())
                 if os.path.exists(stride_file) else w * 4)
    import mmap
    fd = os.open("/dev/fb0", os.O_RDONLY)
    try:
        mm = mmap.mmap(fd, stride * h, access=mmap.ACCESS_READ)
        # 必须取到 (h-1)*stride + w*4：stride 有填充时末行数据超出 w*4*h
        raw = mm[:(h - 1) * stride + w * 4]
        mm.close()
    finally:
        os.close(fd)
    return w, h, raw, stride


def bgra_to_png(w, h, bgra, stride):
    """BGRA 帧 → PNG（RGB）。有 PIL 用 PIL；缺失时纯 zlib 编码，预览不依赖 PIL。"""
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        import io as _io
        packed = bytearray(w * h * 4)
        for y in range(h):
            # 去除行填充后再交给 PIL（raw 解码要求紧排数据）
            packed[y * w * 4:(y + 1) * w * 4] = bgra[y * stride:y * stride + w * 4]
        img = Image.frombytes("RGBA", (w, h), bytes(packed), "raw", "BGRA")
        out = _io.BytesIO()
        img.convert("RGB").save(out, "PNG")
        return out.getvalue()
    import struct
    import zlib

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    rows = bytearray()
    for y in range(h):
        row = bgra[y * stride: y * stride + w * 4]
        rgb = bytearray(w * 3)
        rgb[0::3] = row[2::4]           # R（源为 BGRA）
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows += b"\x00" + rgb           # scanline filter 0
    idat = zlib.compress(bytes(rows), 6)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def fb_restart_needed(prev, cur):
    """仅显示器进程级配置变化才重启渲染进程；其余（模组、主题、强调色等）均热加载。"""
    return any(prev.get(k) != cur.get(k)
               for k in ("fb_enabled", "fb_rotate", "screen_inches"))


class Handler(BaseHTTPRequestHandler):
    server_version = "fnos-dashboard/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # 由 main() 注入
    web_root = "."
    stats = None
    config = None
    var_dir = "."
    etc_dir = "."
    ext = None  # ExtModules
    http_port = 0          # TCP 服务端口（fb_render 回源用）

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，避免 app.log 膨胀

    # ---- 基础工具 ----
    def _is_trusted(self):
        # CGI 反代来自 fnOS 本机回环（已校验 NAS 登录态）；fb 渲染进程亦走回环
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _public_cfg(self, cfg):
        """非可信请求脱敏：API Key 仅回掩码。"""
        if self._is_trusted():
            return cfg
        cfg = json.loads(json.dumps(cfg))
        if cfg.get("qweather_key"):
            cfg["qweather_key"] = "••••••••"
        return cfg

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

    def _send_json(self, code, obj):
        self._send(code, obj)

    def _deny_local_only(self):
        self._send_json(403, {"ok": False,
                              "error": "设置页与管理接口仅限本机访问"
                                       "（请通过飞牛桌面应用入口访问）"})

    def _read_body(self, limit=8 * 1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def _read_json(self, limit=65536):
        body = self._read_body(limit)
        if body is None:
            return None
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _static_path(self, rel):
        root = os.path.realpath(self.web_root)
        full = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if os.path.isfile(full):
            return full
        return None

    def _serve_static(self, rel):
        full = self._static_path(rel)
        if full is None:
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        self._send(200, body, ctype=ctype)

    # ---- framebuffer 信息与预览 ----
    def _fb_restart(self):
        """设置变更后重启显示器渲染进程（启用时拉起，关闭时仅清理）。"""
        pid_file = os.path.join(self.var_dir, "fb.pid")

        def _scan_renderers():
            # pid 文件可能过期（手动拉起/升级遗留），按进程表全量清理
            found = []
            me = os.getpid()
            try:
                names = os.listdir("/proc")
            except OSError:
                return found
            for name in names:
                if not name.isdigit() or int(name) == me:
                    continue
                try:
                    with open("/proc/%s/cmdline" % name, "rb") as f:
                        cmd = f.read().replace(b"\x00", b" ").decode(
                            "utf-8", "replace")
                except OSError:
                    continue
                if "fb_render.py" in cmd:
                    found.append(int(name))
            return found

        def _reap():
            try:
                os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

        def _log(msg):
            # 诊断写入 fb.log：真机上排障时 cat var/fb.log 即可看到重启过程
            try:
                with open(os.path.join(self.var_dir, "fb.log"), "a",
                          encoding="utf-8") as f:
                    f.write("[backend %s] %s\n"
                            % (time.strftime("%m-%d %H:%M:%S"), msg))
            except OSError:
                pass

        old_pids = _scan_renderers()
        for pid in old_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 2
        while time.time() < deadline:
            _reap()
            if not _scan_renderers():
                break
            time.sleep(0.1)
        for pid in _scan_renderers():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _reap()
        try:
            os.unlink(pid_file)
        except OSError:
            pass
        if old_pids:
            _log("restart: 已终止旧渲染进程 %s" % old_pids)

        cfg = self.config.get()
        if not cfg.get("fb_enabled") or not os.path.exists("/dev/fb0"):
            _log("restart: 未拉起（fb_enabled=%s, /dev/fb0=%s）"
                 % (cfg.get("fb_enabled"), os.path.exists("/dev/fb0")))
            return
        fb_bin = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fb_render.py")
        if not os.path.isfile(fb_bin):
            _log("restart: 未拉起（缺少 %s）" % fb_bin)
            return
        port = self.http_port
        log_path = os.path.join(self.var_dir, "fb.log")
        try:
            log = open(log_path, "ab")
            proc = subprocess.Popen(
                [sys.executable, fb_bin, "--fb", "/dev/fb0",
                 "--api", "http://127.0.0.1:%d" % port],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True)
            log.close()
        except OSError as e:
            _log("restart: 拉起失败 %r" % e)
            return
        _log("restart: 已拉起渲染进程 pid=%d (%s)"
             % (proc.pid, sys.executable))
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))

    def _fb_info(self):
        base = "/sys/class/graphics/fb0"
        info = {"exists": os.path.exists("/dev/fb0"), "w": 0, "h": 0,
                "bpp": 0, "pil": False, "renderer_running": False}
        if not info["exists"]:
            return info
        try:
            vs = open(os.path.join(base, "virtual_size")).read().strip()
            info["w"], info["h"] = (int(x) for x in vs.split(",")[:2])
            info["bpp"] = int(
                open(os.path.join(base, "bits_per_pixel")).read().strip())
        except (OSError, ValueError, IndexError):
            pass
        try:
            import PIL  # noqa
            info["pil"] = True
        except ImportError:
            pass
        pid_file = os.path.join(self.var_dir, "fb.pid")
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            info["renderer_running"] = True
        except (OSError, ValueError):
            pass
        return info

    def _fb_dump_png(self):
        try:
            w, h, bgra, stride = fb_raw_read()
        except Exception:
            return None
        try:
            return bgra_to_png(w, h, bgra, stride)
        except Exception:
            return None

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/api/status":
            cfg = self.config.get()
            self._send_json(200, {
                "ok": True,
                "version": APP_VERSION,
                "config": self._public_cfg(cfg),
                "stats": self.stats.snapshot(),
            })
            return
        if path == "/api/config":
            self._send_json(200, {"ok": True,
                                  "config": self._public_cfg(self.config.get())})
            return
        if path == "/api/modules":
            cfg = self.config.get()
            mods = cfg.get("modules") or {}
            out = {}
            if mods.get("calendar"):
                out["calendar"] = calendar_payload()
            if mods.get("weather"):
                out["weather"] = MODULES.weather(cfg)
            ext_out = {}
            for mid, mc in (cfg.get("ext_modules") or {}).items():
                if not mc.get("enabled"):
                    continue
                ext_out[mid] = self.ext.payload(mid, mc.get("config") or {})
            if ext_out:
                out["ext"] = ext_out
            self._send_json(200, {"ok": True, "modules": out,
                                  "config": self._public_cfg(cfg)})
            return

        # ---- 管理面：仅限可信来源（本机回环） ----
        if not self._is_trusted():
            if path == "/settings" or path.startswith("/settings/") or \
                    path.startswith("/api/fb/") or path.startswith("/api/ext/") or \
                    path == "/api/settings":
                self._deny_local_only()
                return

        if path == "/settings":
            return self._serve_static("/settings.html")
        if path == "/api/fb/info":
            self._send_json(200, {"ok": True, "fb": self._fb_info()})
            return
        if path == "/api/fb/dump.png":
            png = self._fb_dump_png()
            if png is None:
                self._send_json(404, {"ok": False,
                                      "error": "帧缓冲不可用（未启用或无权限）"})
                return
            self._send(200, png, ctype="image/png")
            return
        if path == "/api/ext/modules":
            installed = self.ext.list()
            cfg = self.config.get()
            for m in installed:
                mc = (cfg.get("ext_modules") or {}).get(m["id"]) or {}
                m["enabled"] = bool(mc.get("enabled"))
                m["config"] = mc.get("config") or {}
            self._send_json(200, {"ok": True, "modules": installed})
            return
        if path == "/api/ext/keys":
            self._send_json(200, {"ok": True, "keys": self.ext.keys()})
            return

        rel = path if path != "/" else "/index.html"
        self._serve_static(rel)

    # ---- POST / DELETE ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._is_trusted():
            if path == "/api/settings" or path == "/api/config" or \
                    path.startswith("/api/ext/"):
                self._deny_local_only()
                return
        if path == "/api/settings" or path == "/api/config":
            patch = self._read_json()
            if patch is None:
                self._send_json(400, {"ok": False, "error": "无效的请求体"})
                return
            prev = self.config.get()
            ok, err = self.config.update(patch)
            if ok:
                cur = self.config.get()
                # 显示器进程级配置真正变化才重启渲染进程（避免无谓的杀进程/拉起）
                if fb_restart_needed(prev, cur):
                    self._fb_restart()
                self._send_json(200, {"ok": True, "config": cur})
            else:
                self._send_json(400, {"ok": False, "error": err})
            return
        if path == "/api/ext/install":
            q = self.path.split("?", 1)
            filename = ""
            if len(q) > 1:
                from urllib.parse import parse_qs
                filename = (parse_qs(q[1]).get("filename") or [""])[0]
            data = self._read_body()
            if data is None:
                self._send_json(400, {"ok": False, "error": "无效的包数据"})
                return
            try:
                manifest = self.ext.install(data, filename)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            self._send_json(200, {"ok": True, "manifest": manifest})
            return
        if path == "/api/ext/uninstall":
            obj = self._read_json()
            if not obj or not obj.get("id"):
                self._send_json(400, {"ok": False, "error": "缺少模组 ID"})
                return
            try:
                self.ext.uninstall(str(obj["id"]))
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            mid = str(obj["id"])
            remaining = {k: v
                         for k, v in (self.config.get().get("ext_modules") or {}).items()
                         if k != mid}
            self.config.update({"ext_modules": remaining})
            self._send_json(200, {"ok": True})
            return
        if path == "/api/ext/enable":
            obj = self._read_json()
            if not obj or not obj.get("id"):
                self._send_json(400, {"ok": False, "error": "缺少模组 ID"})
                return
            cfg = self.config.get()
            ext = cfg.get("ext_modules") or {}
            mid = str(obj["id"])
            mc = ext.get(mid) or {"enabled": False, "config": {}}
            mc["enabled"] = bool(obj.get("enabled"))
            ext[mid] = mc
            ok, err = self.config.update({"ext_modules": ext})
            if ok:
                self._send_json(200, {"ok": True, "config": self.config.get()})
            else:
                self._send_json(400, {"ok": False, "error": err})
            return
        if path == "/api/ext/keys":
            obj = self._read_json()
            if not obj:
                self._send_json(400, {"ok": False, "error": "无效的请求体"})
                return
            try:
                kid = self.ext.keys_add(obj.get("name"), obj.get("public_key"))
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            self._send_json(200, {"ok": True, "key_id": kid})
            return
        self._send_json(405, {"ok": False, "error": "Method Not Allowed"})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if not self._is_trusted():
            self._deny_local_only()
            return
        if path == "/api/ext/keys":
            obj = self._read_json()
            if not obj or not obj.get("key_id"):
                self._send_json(400, {"ok": False, "error": "缺少 key_id"})
                return
            self.ext.keys_delete(str(obj["key_id"]))
            self._send_json(200, {"ok": True})
            return
        self._send_json(405, {"ok": False, "error": "Method Not Allowed"})
