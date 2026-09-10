#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 后端服务（纯 Python 标准库，无第三方依赖）。

实现思路参考 lcdsimple：采集器从系统接口读取数据、生成 JSON 快照，
前端按主题渲染并定时刷新。适配 fnOS FPK 环境：
  - TRIM_SERVICE_PORT / TRIM_PKGETC / TRIM_PKGVAR 定位端口与目录
  - 配置持久化在 etc/config.json，支持前端运行时修改主题
"""

import argparse
import glob
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import neon_crypto

APP_VERSION = "1.0.0"
THEMES = ("midnight", "graphite", "emerald", "solar", "sakura", "light")
CARD_KEYS = ("cpu", "memory", "disk", "network", "temperature", "info")
DEFAULT_CARDS = {k: True for k in CARD_KEYS}

DEFAULT_CONFIG = {
    "theme": "midnight",
    "accent": "",
    "refresh": 2,
    "temp_unit": "C",
    "cards": dict(DEFAULT_CARDS),
    "saved_at": 0,
    # 显示器（framebuffer）模式
    "fb_enabled": False,
    "rotate_seconds": 15,
    "screen_inches": 0,          # 0=按分辨率自适应；>0 时按 PPI 估算缩放
    "fb_rotate": 0,              # 显示方向：0/90/180/270
    "modules": {"calendar": True, "weather": False},  # coding 已迁移为预装的 volc-plan 扩展模组
    "weather_city": "北京",
    # 和风天气（qweather）配置
    "weather_provider": "open-meteo",   # open-meteo | qweather
    "qweather_host": "devapi.qweather.com",
    "qweather_key": "",
    "weather_location": "",             # "经度,纬度" / LocationID / 城市名（空=用 weather_city）
    "qweather_indices": "7",            # 生活指数 type ID，7=花粉过敏；见和风指数类型表
    "coding": {"name": "", "url": "", "token": "",
               "path_used": "", "path_total": "", "reset": ""},
    # 扩展模组（.neon-dash）：id -> {"enabled": bool, "config": {...}}
    "ext_modules": {},
}

REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs",
    "ntfs", "ntfs3", "vfat", "exfat", "jfs",
}
PRIORITY_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "x86_pkg_temp")
PHYS_IFACE_RE = re.compile(r"^(eth|ens|enp|eno|em|enx|wl|ww|wlan)")


def _writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _clip_conf_val(v):
    """扩展模组配置值白名单化：仅保留 bool/数值/短字符串。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v)[:300]


# --------------------------------------------------------------------------
# 配置：读写 etc/config.json（不可写时回退到 var/config.json）
# --------------------------------------------------------------------------
class Config:
    def __init__(self, etc_path, var_dir):
        self.path = etc_path if _writable_dir(os.path.dirname(etc_path)) \
            else os.path.join(var_dir, "config.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        candidates = [self.path]
        # var 下可能存在手动覆盖的配置，优先级更高
        var_override = os.path.join(
            os.path.dirname(self.path), "..", "config.json")
        var_override = os.path.normpath(var_override)
        if var_override != self.path and os.path.isfile(var_override):
            candidates.append(var_override)
        return self._merge_files(candidates)

    def _merge_files(self, paths):
        data = json.loads(json.dumps(DEFAULT_CONFIG))
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    patch = json.load(f)
                if isinstance(patch, dict):
                    data.update(patch)
                    cards = patch.get("cards")
                    if isinstance(cards, dict):
                        merged = dict(DEFAULT_CARDS)
                        for k in CARD_KEYS:
                            if k in cards:
                                merged[k] = bool(cards[k])
                        data["cards"] = merged
            except (OSError, ValueError):
                continue
        return self._sanitize(data)

    @staticmethod
    def _sanitize(data):
        out = dict(DEFAULT_CONFIG)
        out.update(data)
        if out.get("theme") not in THEMES:
            out["theme"] = DEFAULT_CONFIG["theme"]
        accent = out.get("accent") or ""
        if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            accent = ""
        out["accent"] = accent.lower()
        try:
            out["refresh"] = min(60, max(1, int(out.get("refresh", 2))))
        except (TypeError, ValueError):
            out["refresh"] = 2
        if out.get("temp_unit") not in ("C", "F"):
            out["temp_unit"] = "C"
        out["saved_at"] = int(out.get("saved_at") or 0)
        out["fb_enabled"] = bool(out.get("fb_enabled"))
        try:
            rotate = int(out.get("fb_rotate", 0))
        except (TypeError, ValueError):
            rotate = 0
        out["fb_rotate"] = rotate if rotate in (0, 90, 180, 270) else 0
        try:
            out["rotate_seconds"] = min(300, max(5, int(out.get("rotate_seconds", 15))))
        except (TypeError, ValueError):
            out["rotate_seconds"] = 15
        try:
            out["screen_inches"] = min(200.0, max(0.0, float(out.get("screen_inches", 0))))
        except (TypeError, ValueError):
            out["screen_inches"] = 0.0
        mods = out.get("modules")
        out["modules"] = {k: bool(mods.get(k, False)) if isinstance(mods, dict) else False
                          for k in ("calendar", "weather")}
        city = str(out.get("weather_city") or "").strip()[:40]
        out["weather_city"] = city or DEFAULT_CONFIG["weather_city"]
        out["weather_provider"] = "qweather" \
            if out.get("weather_provider") == "qweather" else "open-meteo"
        host = str(out.get("qweather_host") or "").strip().lower()
        # 只允许合法 host 字符，防 SSRF
        out["qweather_host"] = host if re.fullmatch(
            r"[a-z0-9.-]{3,100}", host) else DEFAULT_CONFIG["qweather_host"]
        out["qweather_key"] = str(out.get("qweather_key") or "").strip()[:128]
        out["weather_location"] = str(out.get("weather_location") or "").strip()[:64]
        ids = str(out.get("qweather_indices") or "").strip()
        keep = [s for s in re.split(r"[^0-9]+", ids) if s and 0 < int(s) <= 30]
        out["qweather_indices"] = ",".join(dict.fromkeys(keep)) or "7"
        coding = dict(DEFAULT_CONFIG["coding"])
        if isinstance(out.get("coding"), dict):
            coding.update({k: str(out["coding"].get(k, "") or "").strip()[:300]
                           for k in coding})
        out["coding"] = coding
        ext = out.get("ext_modules")
        clean_ext = {}
        if isinstance(ext, dict):
            for mid, mc in ext.items():
                if not isinstance(mid, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", mid):
                    continue
                mc = mc if isinstance(mc, dict) else {}
                mconf = mc.get("config") if isinstance(mc.get("config"), dict) else {}
                clean_ext[mid] = {
                    "enabled": bool(mc.get("enabled")),
                    "config": {str(k)[:64]: _clip_conf_val(v)
                               for k, v in list(mconf.items())[:32]},
                }
        out["ext_modules"] = clean_ext
        return out

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, patch):
        """校验并合并补丁，原子写盘。返回 (ok, error)。"""
        clean = {}
        if "theme" in patch:
            if patch["theme"] not in THEMES:
                return False, "不支持的主题：%r" % (patch["theme"],)
            clean["theme"] = patch["theme"]
        if "accent" in patch:
            a = patch["accent"] or ""
            if not isinstance(a, str) or (a != "" and not re.fullmatch(r"#[0-9a-fA-F]{6}", a)):
                return False, "强调色格式无效，应为 #RRGGBB 或空"
            clean["accent"] = a.lower()
        if "fb_enabled" in patch:
            clean["fb_enabled"] = bool(patch["fb_enabled"])
        if "fb_rotate" in patch:
            try:
                rotate = int(patch["fb_rotate"])
            except (TypeError, ValueError):
                return False, "显示方向无效"
            if rotate not in (0, 90, 180, 270):
                return False, "显示方向仅支持 0/90/180/270"
            clean["fb_rotate"] = rotate
        if "ext_modules" in patch:
            ext = patch["ext_modules"]
            if not isinstance(ext, dict):
                return False, "扩展模组配置无效"
            clean["ext_modules"] = {
                mid: ({"enabled": bool((mc or {}).get("enabled")),
                       "config": {str(k)[:64]: _clip_conf_val(v)
                                  for k, v in list(((mc or {}).get("config")
                                                    or {}).items())[:32]}}
                      if isinstance(mc, dict) else {"enabled": False, "config": {}})
                for mid, mc in list(ext.items())[:64]
                if isinstance(mid, str) and _ID_RE.fullmatch(mid)
            }
        if "rotate_seconds" in patch:
            try:
                clean["rotate_seconds"] = min(300, max(5, int(patch["rotate_seconds"])))
            except (TypeError, ValueError):
                return False, "轮换间隔无效"
        if "screen_inches" in patch:
            try:
                clean["screen_inches"] = min(200.0, max(0.0, float(patch["screen_inches"])))
            except (TypeError, ValueError):
                return False, "屏幕尺寸无效"
        if "modules" in patch:
            mods = patch["modules"]
            if not isinstance(mods, dict):
                return False, "模组配置无效"
            clean["modules"] = {k: bool(mods.get(k, False))
                                for k in ("calendar", "weather")}
        if "weather_city" in patch:
            city = str(patch["weather_city"] or "").strip()
            if len(city) > 40:
                return False, "城市名过长"
            clean["weather_city"] = city
        if "weather_provider" in patch:
            clean["weather_provider"] = "qweather" \
                if patch["weather_provider"] == "qweather" else "open-meteo"
        if "qweather_host" in patch:
            host = str(patch["qweather_host"] or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9.-]{3,100}", host):
                return False, "API Host 格式无效"
            clean["qweather_host"] = host
        if "qweather_key" in patch:
            v = str(patch["qweather_key"] or "").strip()
            if v != "••••••••":          # 掩码表示“未修改”，不覆盖原值
                clean["qweather_key"] = v[:128]
        if "weather_location" in patch:
            clean["weather_location"] = str(
                patch["weather_location"] or "").strip()[:64]
        if "qweather_indices" in patch:
            ids = str(patch["qweather_indices"] or "").strip()
            keep = [s for s in re.split(r"[^0-9]+", ids) if s and 0 < int(s) <= 30]
            clean["qweather_indices"] = ",".join(dict.fromkeys(keep)) or "7"
        if "coding" in patch:
            c = patch["coding"]
            if not isinstance(c, dict):
                return False, "Coding Plan 配置无效"
            old = dict(self._data.get("coding") or DEFAULT_CONFIG["coding"])
            old.update({k: str(c.get(k, "") or "").strip()[:300]
                        for k in old})
            if old.get("token") == "••••••••":   # 掩码表示“未修改”
                old["token"] = (self._data.get("coding") or {}).get("token", "")
            clean["coding"] = old
        if "refresh" in patch:
            try:
                clean["refresh"] = min(60, max(1, int(patch["refresh"])))
            except (TypeError, ValueError):
                return False, "刷新间隔无效"
        if "temp_unit" in patch:
            if patch["temp_unit"] not in ("C", "F"):
                return False, "温度单位无效"
            clean["temp_unit"] = patch["temp_unit"]
        if "cards" in patch:
            cards = patch["cards"]
            if not isinstance(cards, dict):
                return False, "卡片配置无效"
            merged = dict(DEFAULT_CARDS)
            for k in CARD_KEYS:
                if k in cards:
                    merged[k] = bool(cards[k])
            clean["cards"] = merged
        if "saved_at" in patch:
            try:
                clean["saved_at"] = int(patch["saved_at"])
            except (TypeError, ValueError):
                clean["saved_at"] = 0

        with self._lock:
            data = dict(self._data)
            data.update(clean)
            data = self._sanitize(data)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                return False, "配置写入失败：%s" % e
            self._data = data
        return True, ""


# --------------------------------------------------------------------------
# 系统状态采集器（差分计算：CPU/网络需跨请求保持上次采样）
# --------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._cpu_last = None            # (total, idle)
        self._core_last = {}             # idx -> (total, idle)
        self._net_last = {}              # name -> (rx, tx, monotonic)
        self._host = self._read_host()
        self._cpu_model = self._read_cpu_model()

    # ---- /proc 解析 ----
    def _cpu(self):
        total = None
        cores = {}
        try:
            with open("/proc/stat", "r") as f:
                for line in f:
                    parts = line.split()
                    if not parts or not parts[0].startswith("cpu"):
                        continue
                    nums = [int(x) for x in parts[1:]]
                    if parts[0] == "cpu":
                        total = nums
                    elif re.fullmatch(r"cpu\d+", parts[0]):
                        cores[int(parts[0][3:])] = nums
        except (OSError, ValueError):
            pass
        if not total:
            return {"usage": 0.0, "cores": [], "core_count": len(cores),
                    "load": [0.0, 0.0, 0.0]}

        idle = total[3] + total[4]
        tot = sum(total)
        usage = 0.0
        core_usage = []
        with self._lock:
            if self._cpu_last:
                d_tot = tot - self._cpu_last[0]
                if d_tot > 0:
                    usage = (1.0 - (idle - self._cpu_last[1]) / d_tot) * 100.0
            self._cpu_last = (tot, idle)
            for idx in sorted(cores):
                nums = cores[idx]
                ci, ct = nums[3] + nums[4], sum(nums)
                prev = self._core_last.get(idx)
                if prev and (ct - prev[0]) > 0:
                    u = (1.0 - (ci - prev[1]) / (ct - prev[0])) * 100.0
                else:
                    u = 0.0
                core_usage.append(round(min(100.0, max(0.0, u)), 1))
                self._core_last[idx] = (ct, ci)
        usage = round(min(100.0, max(0.0, usage)), 1)

        load = [0.0, 0.0, 0.0]
        try:
            with open("/proc/loadavg", "r") as f:
                load = [float(x) for x in f.read().split()[:3]]
        except (OSError, ValueError, IndexError):
            pass
        return {
            "usage": usage,
            "cores": core_usage,
            "core_count": len(cores),
            "load": load,
        }

    def _memory(self):
        mi = {}
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    parts = v.strip().split()
                    if parts:
                        mi[k.strip()] = int(parts[0])  # kB
        except (OSError, ValueError):
            pass
        total = mi.get("MemTotal", 0) * 1024
        avail = mi.get("MemAvailable", mi.get("MemFree", 0)) * 1024
        used = max(0, total - avail)
        swap_total = mi.get("SwapTotal", 0) * 1024
        swap_used = max(0, swap_total - mi.get("SwapFree", 0) * 1024)
        return {
            "total": total,
            "used": used,
            "available": avail,
            "usage": round(used / total * 100, 1) if total else 0.0,
            "swap_total": swap_total,
            "swap_used": swap_used,
        }

    def _disks(self):
        disks = []
        seen = set()
        try:
            with open("/proc/mounts", "r") as f:
                mounts = f.readlines()
        except OSError:
            mounts = []
        for line in mounts:
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mnt, fs = parts[0], parts[1], parts[2]
            if fs not in REAL_FS or not dev.startswith("/dev/"):
                continue
            mnt = mnt.replace("\\040", " ").replace("\\011", "\t")
            if not (mnt == "/" or
                    (mnt.startswith("/vol") and mnt.count("/") <= 2)):
                continue
            key = (dev, mnt)
            if key in seen:
                continue
            seen.add(key)
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            if st.f_blocks <= 0:
                continue
            fr = st.f_frsize
            total = st.f_blocks * fr
            free = st.f_bavail * fr
            used = max(0, total - free)
            disks.append({
                "mount": mnt,
                "dev": dev,
                "fs": fs,
                "total": total,
                "used": used,
                "usage": round(used / total * 100, 1) if total else 0.0,
                "ro": "ro" in parts[3].split(","),
            })
        disks.sort(key=lambda d: (0 if d["mount"].startswith("/vol") else 1,
                                  d["mount"]))
        return disks

    def _net(self):
        ifaces = []
        now = time.monotonic()
        try:
            with open("/proc/net/dev", "r") as f:
                lines = f.read().splitlines()[2:]
        except OSError:
            lines = []
        for ln in lines:
            if ":" not in ln:
                continue
            name, rest = ln.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            cols = rest.split()
            if len(cols) < 9:
                continue
            try:
                rx, tx = int(cols[0]), int(cols[8])
            except ValueError:
                continue
            rx_rate = tx_rate = 0.0
            with self._lock:
                prev = self._net_last.get(name)
                self._net_last[name] = (rx, tx, now)
            if prev and (now - prev[2]) > 0:
                rx_rate = max(0.0, (rx - prev[0]) / (now - prev[2]))
                tx_rate = max(0.0, (tx - prev[1]) / (now - prev[2]))
            total = rx + tx
            keep = bool(PHYS_IFACE_RE.match(name)) or total > 0 or rx_rate + tx_rate > 0
            if keep:
                ifaces.append({
                    "name": name,
                    "rx": rx,
                    "tx": tx,
                    "rx_rate": round(rx_rate, 1),
                    "tx_rate": round(tx_rate, 1),
                })
        ifaces.sort(key=lambda i: -(i["rx_rate"] + i["tx_rate"] + i["rx"] + i["tx"]))
        # 存在物理网卡时隐藏 Docker 虚拟链路，避免列表噪音
        has_phys = any(PHYS_IFACE_RE.match(i["name"]) for i in ifaces)
        if has_phys:
            ifaces = [i for i in ifaces
                      if not i["name"].startswith(("veth", "br-", "docker", "virbr"))]
        return {"ifaces": ifaces[:8]}

    def _temps(self):
        temps = []
        for inp in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
            try:
                with open(inp, "r") as f:
                    c = int(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if not (-40.0 < c < 150.0):
                continue
            chip = ""
            name_file = os.path.join(os.path.dirname(inp), "name")
            if os.path.exists(name_file):
                try:
                    with open(name_file, "r") as f:
                        chip = f.read().strip()
                except OSError:
                    pass
            label = chip
            lab_file = inp.replace("_input", "_label")
            if os.path.exists(lab_file):
                try:
                    with open(lab_file, "r") as f:
                        label = f.read().strip() or chip
                except OSError:
                    pass
            temps.append({"label": label or "sensor", "chip": chip,
                          "celsius": round(c, 1)})
        if not temps:
            for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
                try:
                    with open(os.path.join(tz, "temp"), "r") as f:
                        c = int(f.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if not (-40.0 < c < 150.0):
                    continue
                typ = os.path.basename(tz)
                tf = os.path.join(tz, "type")
                if os.path.exists(tf):
                    try:
                        with open(tf, "r") as f:
                            typ = f.read().strip() or typ
                    except OSError:
                        pass
                temps.append({"label": typ, "chip": typ, "celsius": round(c, 1)})
        # 去重并让 CPU 相关传感器排前
        uniq = []
        seen = set()
        for t in temps:
            key = (t["label"], t["celsius"])
            if key not in seen:
                seen.add(key)
                uniq.append(t)

        def prio(t):
            return (0 if t["chip"] in PRIORITY_CHIPS else 1, t["label"])
        uniq.sort(key=prio)
        cpu_temp = uniq[0]["celsius"] if uniq else None
        return {"sensors": uniq[:8], "cpu_temp": cpu_temp}

    @staticmethod
    def _read_host():
        h = {"hostname": socket.gethostname(), "os": "", "kernel": "", "arch": ""}
        try:
            info = {}
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.rstrip().split("=", 1)
                        info[k] = v.strip().strip('"')
            h["os"] = info.get("PRETTY_NAME", "")
        except OSError:
            pass
        try:
            h["kernel"] = os.uname().release
            h["arch"] = os.uname().machine
        except Exception:
            pass
        return h

    @staticmethod
    def _read_cpu_model():
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line and ":" in line:
                        return line.split(":", 1)[1].strip()
                    if line.startswith("Hardware") and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return ""

    @staticmethod
    def _procs():
        n = 0
        for name in os.listdir("/proc"):
            if name.isdigit():
                n += 1
        return n

    def snapshot(self):
        cpu = self._cpu()
        try:
            with open("/proc/uptime", "r") as f:
                uptime = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            uptime = 0
        host = dict(self._host)
        host["cpu_model"] = self._cpu_model
        host["procs"] = self._procs()
        host["uptime"] = uptime
        return {
            "ts": time.time(),
            "host": host,
            "cpu": cpu,
            "memory": self._memory(),
            "disks": self._disks(),
            "net": self._net(),
            "temps": self._temps(),
        }


# --------------------------------------------------------------------------
# 可选模组：日历 / 天气预报 / Coding Plan 用量
# --------------------------------------------------------------------------
import calendar as _calendar
import datetime as _dt
import urllib.parse
import urllib.request

WMO_TEXT = {
    0: "晴", 1: "多云", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛雨", 53: "毛雨", 55: "大毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "霰",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _calendar_payload():
    today = _dt.date.today()
    first_wd, days = _calendar.monthrange(today.year, today.month)  # 周一=0
    weeks = []
    week = [None] * first_wd
    for d in range(1, days + 1):
        week.append({"d": d,
                     "today": (d == today.day)})
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return {
        "year": today.year,
        "month": today.month,
        "day": today.day,
        "weekday": WEEKDAY_ZH[first_wd],
        "weeks": weeks,
    }


class ModuleCache:
    """天气 30 分钟、Coding 用量 5 分钟内存缓存，失败时保留上次数据。"""

    def __init__(self):
        self._weather = {}
        self._weather_at = 0.0
        self._weather_key = ""
        self._geo = {}           # 城市名 -> (ts, location_str, name)
        self._coding = {}
        self._coding_at = 0.0
        self._coding_key = ""
        self._lock = threading.Lock()

    @staticmethod
    def _http_json(url, headers=None, timeout=8):
        req = urllib.request.Request(
            url, headers=headers or {"User-Agent": "fnos-dashboard/1.0"})
        req.add_header("Accept-Encoding", "identity")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def weather(self, cfg):
        """cfg 为全量配置；30 分钟缓存，配置变更立即刷新。"""
        key = json.dumps({k: cfg.get(k) for k in (
            "weather_provider", "qweather_host", "qweather_key",
            "weather_location", "qweather_indices", "weather_city")},
            sort_keys=True, ensure_ascii=False)
        with self._lock:
            now = time.time()
            if key == self._weather_key and self._weather \
                    and now - self._weather_at < 1800:
                return self._weather
            self._weather_key = key
        provider = cfg.get("weather_provider")
        if provider == "qweather" and cfg.get("qweather_key"):
            payload = self._qweather(cfg)
        else:
            payload = self._open_meteo(cfg, now)
        with self._lock:
            self._weather = payload
            self._weather_at = now
        return payload

    # ---- Open-Meteo（免密钥回退源）----
    def _open_meteo(self, cfg, now):
        city = cfg.get("weather_city") or "北京"
        try:
            geo = self._http_json(
                "https://geocoding-api.open-meteo.com/v1/search?%s"
                % urllib.parse.urlencode(
                    {"name": city, "count": 1, "language": "zh", "format": "json"}))
            hits = geo.get("results") or []
            if not hits:
                raise ValueError("未找到城市：%s" % city)
            loc = hits[0]
            data = self._http_json(
                "https://api.open-meteo.com/v1/forecast?%s" % urllib.parse.urlencode({
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "current": "temperature_2m,weather_code",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "auto",
                    "forecast_days": 5,
                }))
            daily = data.get("daily") or {}
            return {
                "provider": "open-meteo",
                "city": loc.get("name", city),
                "current": {
                    "temp": (data.get("current") or {}).get("temperature_2m"),
                    "code": (data.get("current") or {}).get("weather_code"),
                },
                "daily": [
                    {"date": (daily.get("time") or [""])[i],
                     "code": (daily.get("weather_code") or [0])[i],
                     "max": (daily.get("temperature_2m_max") or [None])[i],
                     "min": (daily.get("temperature_2m_min") or [None])[i]}
                    for i in range(min(5, len(daily.get("time") or [])))
                ],
                "fetched_at": now,
                "error": "",
            }
        except Exception as e:
            with self._lock:
                if self._weather and not self._weather.get("error"):
                    old = dict(self._weather)
                    old["error"] = str(e)[:120]
                    return old
            return {"provider": "open-meteo", "city": city, "current": {},
                    "daily": [], "fetched_at": now, "error": str(e)[:120]}

    # ---- 和风天气 ----
    COORD_RE = re.compile(r"^-?[\d.]+,-?[\d.]+$")

    def _qweather_loc(self, cfg, qkey):
        """解析位置 → (location_str, 显示名)。支持 坐标/LocationID/城市名。"""
        loc = (cfg.get("weather_location") or "").strip()
        if loc:
            if self.COORD_RE.fullmatch(re.sub(r"\s+", "", loc)):
                lng, lat = [s.strip() for s in loc.split(",")]
                return "%s,%s" % (lng, lat), loc
            if loc.isdigit():
                return loc, cfg.get("weather_city") or loc
        city = cfg.get("weather_city") or "北京"
        with self._lock:
            cached = self._geo.get(city)
            if cached and time.time() - cached[0] < 86400:
                return cached[1], cached[2]
        data = self._http_json(
            "https://geoapi.qweather.com/v2/city/lookup?%s" % urllib.parse.urlencode(
                {"location": city, "number": 1, "lang": "zh"}),
            headers={"X-QW-Api-Key": qkey})
        if str(data.get("code")) != "200" or not data.get("location"):
            raise ValueError("GeoAPI 查询失败：%s" % data.get("code"))
        hit = data["location"][0]
        loc_str = "%s,%s" % (hit.get("lon"), hit.get("lat"))
        name = hit.get("name", city)
        with self._lock:
            self._geo[city] = (time.time(), loc_str, name)
        return loc_str, name

    def _qweather_hourly(self, cfg, qkey, loc_str):
        host = cfg.get("qweather_host") or "devapi.qweather.com"
        headers = {"X-QW-Api-Key": qkey}
        data = None
        m = re.fullmatch(r"(-?[\d.]+),(-?[\d.]+)", loc_str)
        if m:  # 新版坐标接口优先
            lng, lat = m.group(1), m.group(2)
            try:
                data = self._http_json(
                    "https://%s/weather/v1/hourly/%s/%s?hours=12&lang=zh&localTime=true"
                    % (host, lat, lng), headers=headers)
                if not isinstance(data, dict) or not data.get("hours"):
                    data = None
            except Exception:
                data = None
        if data is None:  # v7 回退（坐标 / LocationID 均可）
            data = self._http_json(
                "https://%s/v7/weather/24h?%s" % (host, urllib.parse.urlencode(
                    {"location": loc_str, "lang": "zh"})), headers=headers)
        out = []
        hours = data.get("hours") or data.get("hourly") or []
        for h in hours:
            if "forecastTime" in h:      # 新版结构
                t = h.get("forecastTime", "")
                temp = (h.get("temperature") or {}).get("value")
                text = (h.get("condition") or {}).get("text", "")
                hum = h.get("humidity")
                pop = (h.get("precipitation") or {}).get("probability")
            else:                         # v7 结构（字符串值）
                t = h.get("fxTime", "")
                temp = h.get("temp")
                text = h.get("text", "")
                hum = h.get("humidity")
                pop = h.get("pop")
            if not isinstance(temp, (int, float)):
                try:
                    temp = float(temp)
                except (TypeError, ValueError):
                    continue
            try:
                hh = _dt.datetime.fromisoformat(
                    t.replace("Z", "+00:00")).strftime("%H:%M")
            except ValueError:
                hh = t[11:16] if len(t) >= 16 else t
            try:
                hum = int(round(float(hum) * (100 if float(hum) <= 1 else 1)))
            except (TypeError, ValueError):
                hum = None
            try:
                pop = int(round(float(pop) * (100 if float(pop) <= 1 else 1)))
            except (TypeError, ValueError):
                pop = None
            out.append({"time": hh, "temp": int(temp), "text": text,
                        "humidity": hum, "pop": pop})
            if len(out) >= 6:
                break
        if not out:
            raise ValueError("小时预报为空")
        return out

    def _qweather_indices(self, cfg, qkey, loc_str):
        host = cfg.get("qweather_host") or "devapi.qweather.com"
        ids = cfg.get("qweather_indices") or "7"
        data = self._http_json(
            "https://%s/v7/indices/1d?%s" % (host, urllib.parse.urlencode(
                {"type": ids, "location": loc_str, "lang": "zh"})),
            headers={"X-QW-Api-Key": qkey})
        if str(data.get("code")) != "200":
            raise ValueError("指数接口失败：%s" % data.get("code"))
        return [{"type": d.get("type"), "name": d.get("name", ""),
                 "level": d.get("level", ""), "category": d.get("category", ""),
                 "text": (d.get("text") or "")[:80]}
                for d in (data.get("daily") or [])][:6]

    def _qweather(self, cfg):
        city = cfg.get("weather_city") or "北京"
        qkey = cfg.get("qweather_key") or ""
        try:
            if not qkey:
                raise ValueError("未配置和风天气 API Key")
            loc_str, name = self._qweather_loc(cfg, qkey)
            hourly = self._qweather_hourly(cfg, qkey, loc_str)
            try:
                indices = self._qweather_indices(cfg, qkey, loc_str)
            except Exception:
                indices = []
            return {"provider": "qweather", "city": name, "hourly": hourly,
                    "indices": indices, "error": "", "fetched_at": time.time()}
        except Exception as e:
            with self._lock:
                if self._weather and not self._weather.get("error") \
                        and self._weather.get("provider") == "qweather":
                    old = dict(self._weather)
                    old["error"] = str(e)[:120]
                    return old
            return {"provider": "qweather", "city": city, "hourly": [],
                    "indices": [], "error": str(e)[:120], "fetched_at": time.time()}

    @staticmethod
    def _dig(obj, path):
        cur = obj
        for part in (path or "").split("."):
            part = part.strip()
            if not part:
                continue
            if isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                    continue
                except (ValueError, IndexError):
                    return None
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def coding(self, cfg):
        c = cfg.get("coding") or {}
        key = json.dumps(c, sort_keys=True, ensure_ascii=False)
        with self._lock:
            now = time.time()
            if key == self._coding_key and self._coding and now - self._coding_at < 300:
                return self._coding
            self._coding_key = key
            url = c.get("url", "")
            base = {"name": c.get("name") or "Coding Plan",
                    "reset": c.get("reset") or "", "error": ""}
            if not url:
                payload = dict(base, ok=False, error="未配置用量接口")
            else:
                headers = {"User-Agent": "fnos-dashboard/1.0"}
                if c.get("token"):
                    headers["Authorization"] = "Bearer " + c["token"]
                try:
                    data = self._http_json(url, headers=headers)
                    used = self._dig(data, c.get("path_used"))
                    total = self._dig(data, c.get("path_total"))
                    if used is None:
                        raise ValueError("path_used 未命中")
                    payload = dict(base, ok=True,
                                   used=float(used),
                                   total=float(total) if total is not None else None)
                except Exception as e:
                    payload = dict(base, ok=False, error=str(e)[:120])
            self._coding = payload
            self._coding_at = now
            return payload


MODULES = ModuleCache()


# --------------------------------------------------------------------------
# 扩展模组（.neon-dash）：安装 / 签名验证 / 加载执行
# --------------------------------------------------------------------------
_ID_RE = re.compile(r"[a-z0-9_-]{1,32}")

# 随项目内置信任的官方签名者公钥（neon-dash/keys/neon.pub）。
# 用户信任列表（etc/trusted_keys.json）可自行增删，内置项不受 API 影响。
BUILTIN_TRUSTED = [
    {
        "key_id": "SHA256:9002f14abbf7506c",
        "name": "neon (fnos-dashboard)",
        "public_key": "N4R81vfhxqaNH5uwDFrW5mHkW6+lhEAs3UKm8Su+gDk=",
        "builtin": True,
    },
]


class ExtModules:
    """var/modules/<id>/ 目录形式管理，安装时完成签名验证。"""

    def __init__(self, var_dir, etc_dir):
        self.root = os.path.join(var_dir, "modules")
        self.etc_dir = etc_dir
        self._mods = {}          # id -> loaded module object
        self._payload = {}       # id -> (ts, payload)
        self._payload_ttl = 30
        self._lock = threading.Lock()

    def _mod_dir(self, mod_id):
        if not _ID_RE.fullmatch(mod_id):
            raise ValueError("非法模组 ID")
        return os.path.join(self.root, mod_id)

    def list(self):
        out = []
        try:
            ids = sorted(os.listdir(self.root))
        except OSError:
            ids = []
        for mid in ids:
            mdir = os.path.join(self.root, mid)
            manifest_path = os.path.join(mdir, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, ValueError):
                continue
            verify = {}
            vpath = os.path.join(mdir, ".verify.json")
            try:
                with open(vpath, "r", encoding="utf-8") as f:
                    verify = json.load(f)
            except (OSError, ValueError):
                pass
            out.append({"id": mid,
                        "name": manifest.get("name", mid),
                        "version": manifest.get("version", ""),
                        "author": manifest.get("author", ""),
                        "desc": manifest.get("desc", ""),
                        "config_schema": manifest.get("config_schema", []),
                        "verify": verify})
        return out

    def install(self, data, filename):
        """安装 .neon-dash（zip bytes）。返回 manifest dict。"""
        if not filename or not filename.endswith(".neon-dash"):
            raise ValueError("仅支持 .neon-dash 包")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("无效的 zip 包")
        names = zf.namelist()
        if "manifest.json" not in names:
            raise ValueError("缺少 manifest.json")
        entries = []
        for n in names:
            if n.endswith("/"):
                continue
            entries.append((n, zf.read(n)))
        manifest = {}
        for n, c in entries:
            if n == "manifest.json":
                try:
                    manifest = json.loads(c.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ValueError("manifest.json 解析失败")
                break
        mod_id = str(manifest.get("id", ""))
        if not _ID_RE.fullmatch(mod_id):
            raise ValueError("manifest.id 非法（限 a-z0-9_-，≤32 字符）")
        if manifest.get("entry", "mod.py") not in dict(entries):
            raise ValueError("缺少入口文件 %r" % manifest.get("entry"))

        # 签名验证：invalid 直接拒绝（防篡改）；unsigned/untrusted 警示放行
        sig_entry = dict(entries).get("signature.json")
        trusted = self.keys()  # 内置信任 + 用户信任列表
        payload = [(n, c) for n, c in entries if n != "signature.json"]
        if sig_entry is not None:
            try:
                sig_obj = json.loads(sig_entry.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("signature.json 解析失败")
            status, info = neon_crypto.verify_package(payload, sig_obj, trusted)
            if status == "invalid":
                raise ValueError("签名验证失败，已拒绝安装：%s"
                                 % info.get("reason", ""))
        else:
            status, info = "unsigned", {}

        # 防路径穿越解压
        mdir = self._mod_dir(mod_id)
        tmp = mdir + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        for n, c in payload:
            dest = os.path.realpath(os.path.join(tmp, n))
            if dest != os.path.realpath(tmp) and \
                    not dest.startswith(os.path.realpath(tmp) + os.sep):
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError("包内路径非法：%r" % n)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(c)
        with open(os.path.join(tmp, ".verify.json"), "w", encoding="utf-8") as f:
            json.dump({"status": status, "at": time.time(), **info},
                      f, ensure_ascii=False)
        shutil.rmtree(mdir, ignore_errors=True)
        os.replace(tmp, mdir)
        with self._lock:
            self._mods.pop(mod_id, None)
            self._payload.pop(mod_id, None)
        manifest["_verify"] = {"status": status, **info}
        return manifest

    def uninstall(self, mod_id):
        mdir = self._mod_dir(mod_id)
        if not os.path.isdir(mdir):
            raise ValueError("模组未安装")
        shutil.rmtree(mdir)
        with self._lock:
            self._mods.pop(mod_id, None)
            self._payload.pop(mod_id, None)

    def payload(self, mod_id, mod_conf):
        """调用模组 get_payload，带 30s 缓存与异常隔离。"""
        now = time.time()
        with self._lock:
            cached = self._payload.get(mod_id)
            if cached and now - cached[0] < self._payload_ttl:
                return cached[1]
        mdir = self._mod_dir(mod_id)
        try:
            with open(os.path.join(mdir, "manifest.json"), "r",
                      encoding="utf-8") as f:
                manifest = json.load(f)
            entry = os.path.join(mdir, manifest.get("entry", "mod.py"))
            with self._lock:
                mod = self._mods.get(mod_id)
            if mod is None:
                spec = importlib.util.spec_from_file_location(
                    "neon_mod_" + mod_id, entry)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                with self._lock:
                    self._mods[mod_id] = mod
            data = mod.get_payload(dict(mod_conf))
            if not isinstance(data, dict):
                raise ValueError("get_payload 应返回 dict")
        except Exception as e:
            data = {"title": mod_id, "error": str(e)[:100]}
        with self._lock:
            self._payload[mod_id] = (now, data)
        return data

    def preinstall(self, bundled_dir):
        """把应用自带（官方签名）的 .neon-dash 预装到本地，版本变化时覆盖。"""
        try:
            files = sorted(f for f in os.listdir(bundled_dir)
                           if f.endswith(".neon-dash"))
        except OSError:
            return
        for fn in files:
            path = os.path.join(bundled_dir, fn)
            try:
                with zipfile.ZipFile(path) as zf:
                    manifest = json.loads(zf.read("manifest.json")
                                          .decode("utf-8"))
                mod_id = str(manifest.get("id", ""))
                if not _ID_RE.fullmatch(mod_id):
                    continue
                mdir = self._mod_dir(mod_id)
                cur = os.path.join(mdir, "manifest.json")
                if os.path.isfile(cur):
                    try:
                        with open(cur, "r", encoding="utf-8") as f:
                            if json.load(f).get("version") == manifest.get("version"):
                                continue  # 同版本已预装
                    except (OSError, ValueError):
                        pass
                with open(path, "rb") as f:
                    self.install(f.read(), fn)
                print("[ext] 预装模组 %s v%s"
                      % (mod_id, manifest.get("version")), flush=True)
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                print("[ext] 预装 %s 失败：%s" % (fn, e), flush=True)

    def keys(self):
        """内置信任 + 用户信任列表合并（按 key_id 去重）。"""
        seen = set()
        out = []
        for k in BUILTIN_TRUSTED + neon_crypto.load_trusted_keys(self.etc_dir):
            kid = k.get("key_id")
            if kid and kid not in seen:
                seen.add(kid)
                out.append(k)
        return out

    def keys_add(self, name, public_key_b64):
        try:
            pub = neon_crypto.b64d(public_key_b64)
        except Exception:
            raise ValueError("公钥 base64 无效")
        if len(pub) != 32:
            raise ValueError("公钥应为 32 字节 ed25519 raw key")
        keys = self.keys()
        kid = neon_crypto.key_id(pub)
        if any(k.get("key_id") == kid for k in keys):
            raise ValueError("该密钥已存在")
        keys.append({"key_id": kid, "name": str(name or "")[:64],
                     "public_key": neon_crypto.b64e(pub),
                     "added_at": time.time()})
        neon_crypto.save_trusted_keys(self.etc_dir, keys)
        return kid

    def keys_delete(self, key_id):
        keys = [k for k in self.keys() if k.get("key_id") != key_id]
        neon_crypto.save_trusted_keys(self.etc_dir, keys)


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
    gateway_prefix = ""    # 统一网关前缀，如 /app/com.fnos.dashboard

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，避免 app.log 膨胀

    # ---- 基础工具 ----
    def _trim_gateway_prefix(self, path):
        gp = self.gateway_prefix
        if gp and path != gp and path.startswith(gp + "/"):
            path = path[len(gp):] or "/"
        elif path == gp:
            path = "/"
        return path

    def _is_trusted(self):
        # 统一网关请求已由飞牛校验 NAS 登录态后转发，视为可信；
        # 直连 TCP 端口仍要求来源为本机
        if getattr(self.server, "gateway", False):
            return True
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _public_cfg(self, cfg):
        """非可信请求脱敏：API Key / Token 仅回掩码。"""
        if self._is_trusted():
            return cfg
        cfg = json.loads(json.dumps(cfg))
        if cfg.get("qweather_key"):
            cfg["qweather_key"] = "••••••••"
        c = cfg.get("coding")
        if isinstance(c, dict) and c.get("token"):
            c["token"] = "••••••••"
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
        try:
            with open(pid_file) as f:
                old = int(f.read().strip())
            os.kill(old, signal.SIGTERM)
            for _ in range(15):
                if not _pid_alive(old):
                    break
                time.sleep(0.1)
            if _pid_alive(old):
                os.kill(old, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        try:
            os.unlink(pid_file)
        except OSError:
            pass

        cfg = self.config.get()
        if not cfg.get("fb_enabled") or not os.path.exists("/dev/fb0"):
            return
        fb_bin = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fb_render.py")
        if not os.path.isfile(fb_bin):
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
        except OSError:
            return
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
            from PIL import Image
        except ImportError:
            return None
        base = "/sys/class/graphics/fb0"
        try:
            vs = open(os.path.join(base, "virtual_size")).read().strip()
            w, h = (int(x) for x in vs.split(",")[:2])
            bpp = int(open(os.path.join(base, "bits_per_pixel")).read().strip())
            if bpp != 32:
                return None
            stride_file = os.path.join(base, "stride")
            stride = max(w * 4,
                         int(open(stride_file).read().strip())
                         if os.path.exists(stride_file) else w * 4)
            fd = os.open("/dev/fb0", os.O_RDONLY)
            import mmap
            mm = mmap.mmap(fd, stride * h, access=mmap.ACCESS_READ)
            img = Image.frombytes("RGBA", (w, h), bytes(mm[:w * 4 * h]),
                                  "raw", "BGRA")
            mm.close()
            os.close(fd)
            import io as _io
            out = _io.BytesIO()
            img.convert("RGB").save(out, "PNG")
            return out.getvalue()
        except Exception:
            return None

    # ---- GET ----
    def do_GET(self):
        path = self._trim_gateway_prefix(
            self.path.split("?", 1)[0].split("#", 1)[0])
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
                out["calendar"] = _calendar_payload()
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

        # ---- 管理面：仅限可信来源（本机 / 统一网关） ----
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
                                      "error": "帧缓冲不可用（未启用或无 PIL/权限）"})
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
        if path == "/api/ext/mock.zip":
            self._send_json(410, {"ok": False, "error": "示例模组已下线，"
                                       "官方模组随应用预装（volc-plan / glm-plan）"})
            return
        if path == "/api/ext/keys":
            self._send_json(200, {"ok": True, "keys": self.ext.keys()})
            return

        rel = path if path != "/" else "/index.html"
        if path == "/":
            # 图标经统一网关打开根路径时直接进入设置页；?panel=1 或
            # TCP 直连（NAS 本机 / 局域网）仍是只读面板
            if getattr(self.server, "gateway", False) and \
                    "panel" not in self.path:
                return self._serve_static("/settings.html")
        self._serve_static(rel)

    # ---- POST / DELETE ----
    def do_POST(self):
        path = self._trim_gateway_prefix(self.path.split("?", 1)[0])
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
            ok, err = self.config.update(patch)
            if ok:
                # 显示器相关设置变更后立即重启渲染进程，无需重启应用
                if {"fb_enabled", "fb_rotate", "screen_inches",
                    "rotate_seconds"} & set(patch):
                    self._fb_restart()
                self._send_json(200, {"ok": True, "config": self.config.get()})
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
        path = self._trim_gateway_prefix(self.path.split("?", 1)[0])
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


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
class UnixThreadingHTTPServer(ThreadingHTTPServer):
    """统一网关入口：监听 ${TRIM_APPDEST}/app.sock（请求经飞牛登录态校验转发）。"""
    address_family = socket.AF_UNIX


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="fnOS 状态监视后端")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TRIM_SERVICE_PORT", "8199")))
    ap.add_argument("--web", default=os.path.normpath(os.path.join(here, "..", "web")))
    ap.add_argument("--config",
                    default=os.path.join(
                        os.environ.get("TRIM_PKGETC",
                                       os.path.normpath(os.path.join(here, "..", "etc"))),
                        "config.json"))
    ap.add_argument("--var",
                    default=os.environ.get("TRIM_PKGVAR",
                                           os.path.normpath(os.path.join(here, "..", "var"))))
    ap.add_argument("--socket", default="",
                    help="统一网关 Unix Socket 路径（如 ${TRIM_APPDEST}/app.sock）")
    ap.add_argument("--gateway-prefix", default="/app/fnos-dashboard",
                    help="统一网关前缀（须与 ui/config 的 gatewayPrefix 一致，"
                         "公开路径不允许点号）")
    args = ap.parse_args()

    var_dir = args.var
    try:
        os.makedirs(var_dir, exist_ok=True)
    except OSError:
        pass

    cfg = Config(args.config, var_dir)
    stats = Stats()
    etc_dir = os.path.dirname(cfg.path)
    ext = ExtModules(var_dir, etc_dir)
    ext.preinstall(os.path.dirname(os.path.abspath(__file__)))

    Handler.web_root = args.web
    Handler.config = cfg
    Handler.stats = stats
    Handler.var_dir = var_dir
    Handler.etc_dir = etc_dir
    Handler.ext = ext
    Handler.http_port = args.port
    Handler.gateway_prefix = args.gateway_prefix.rstrip("/")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.gateway = False
    servers = [server]

    if args.socket:
        try:
            os.unlink(args.socket)      # 清理上次异常退出遗留的 Socket
        except OSError:
            pass
        gw = UnixThreadingHTTPServer(args.socket, Handler)
        gw.daemon_threads = True
        gw.gateway = True
        try:
            os.chmod(args.socket, 0o666)  # 供系统网关进程连接
        except OSError:
            pass
        servers.append(gw)
        threading.Thread(
            target=gw.serve_forever, kwargs={"poll_interval": 0.5},
            daemon=True, name="gateway-socket").start()

    def _shutdown(signum, _frame):
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print("[fnos-dashboard] v%s listening on %s:%d, web=%s, config=%s%s"
          % (APP_VERSION, args.host, args.port, args.web, cfg.path,
             ", gateway=%s (%s)" % (args.socket, Handler.gateway_prefix)
             if args.socket else ""), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        for s in servers:
            s.server_close()
        if args.socket:
            try:
                os.unlink(args.socket)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
