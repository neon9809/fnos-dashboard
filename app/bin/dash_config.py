#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 配置层（etc/config.json 读写与白名单校验）。

从 dashboard.py 拆出；主题调色板正本在 web/assets/style.css 的
body[data-theme] 变量（fb_render 与前端 THEMES[].bg 均以 --bg1 锚定）。
"""

import json
import os
import re
import threading

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
    # 显示器（framebuffer）模式
    "fb_enabled": False,
    "rotate_seconds": 15,
    "screen_inches": 0,          # 0=按分辨率自适应；>0 时按 PPI 估算缩放
    "fb_rotate": 0,              # 显示方向：0/90/180/270
    "modules": {"calendar": True, "weather": False},
    "weather_city": "北京",
    # 和风天气（qweather）配置
    "weather_provider": "open-meteo",   # open-meteo | qweather
    "qweather_host": "devapi.qweather.com",
    "qweather_key": "",
    "weather_location": "",             # "经度,纬度" / LocationID / 城市名（空=用 weather_city）
    "qweather_indices": "7",            # 生活指数 type ID，7=花粉过敏；见和风指数类型表
    # 扩展模组（.neon-dash）：id -> {"enabled": bool, "config": {...}}
    "ext_modules": {},
}

_ID_RE = re.compile(r"[a-z0-9_-]{1,32}")


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


class Config:
    def __init__(self, etc_path, var_dir):
        self.var_dir = var_dir
        self.path = etc_path if _writable_dir(os.path.dirname(etc_path)) \
            else os.path.join(var_dir, "config.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        candidates = [self.path]
        # var 下可能存在手动覆盖的配置，优先级更高
        var_override = os.path.normpath(os.path.join(self.var_dir, "config.json"))
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
        keep = []
        for s in re.split(r"[^0-9]+", ids):
            if s and 0 < int(s) <= 30:
                keep.append(s)
        out["qweather_indices"] = ",".join(dict.fromkeys(keep)) or "7"
        ext = out.get("ext_modules")
        clean_ext = {}
        if isinstance(ext, dict):
            for mid, mc in ext.items():
                if not isinstance(mid, str) or not _ID_RE.fullmatch(mid):
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
