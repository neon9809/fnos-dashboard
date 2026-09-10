#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 可选模组层。

- 内置模组：日历 / 天气（Open-Meteo 免费源 + 和风天气）
- 扩展模组（.neon-dash）：安装 / ed25519 签名验证 / 加载执行
- WMO_TEXT / WMO_EN 为 Open-Meteo 天气码正本，fb_render.py 亦从此导入
"""

import calendar as _calendar
import datetime as _dt
import gzip
import importlib.util
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import neon_crypto
from dash_config import _ID_RE

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

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


def calendar_payload():
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
    """天气 30 分钟内存缓存，失败时保留上次数据。"""

    def __init__(self):
        self._weather = {}
        self._weather_at = 0.0
        self._weather_key = ""
        self._geo = {}           # 城市名 -> (ts, location_str, name)
        self._lock = threading.Lock()

    @staticmethod
    def _http_json(url, headers=None, timeout=8):
        req = urllib.request.Request(
            url, headers=headers or {"User-Agent": "fnos-dashboard/1.0"})
        req.add_header("Accept-Encoding", "identity")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # 带上服务端错误体（如和风 {"error":{"code":...}}），便于定位
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:120]
            except Exception:
                pass
            raise RuntimeError("HTTP %s %s" % (e.code, body)) from e
        # 和风专属 API 域名会无视 identity 强制返回 gzip
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))

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
                a, b = [s.strip() for s in loc.split(",")]
                # 和风 v7 约定 经度,纬度；用户常填 纬度,经度——第二段>90 不可能是纬度，自动交换
                if abs(float(b)) > 90 >= abs(float(a)):
                    a, b = b, a
                return "%s,%s" % (a, b), cfg.get("weather_city") or loc
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
            # 逐小时与生活指数并行拉取，避免 /api/modules 串行阻塞过久
            with ThreadPoolExecutor(max_workers=2) as pool:
                fh = pool.submit(self._qweather_hourly, cfg, qkey, loc_str)
                fi = pool.submit(self._qweather_indices, cfg, qkey, loc_str)
                hourly = fh.result()
                try:
                    indices = fi.result()
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


MODULES = ModuleCache()


# --------------------------------------------------------------------------
# 扩展模组（.neon-dash）：安装 / 签名验证 / 加载执行
# --------------------------------------------------------------------------

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
