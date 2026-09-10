#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 系统状态采集器。

差分计算：CPU / 网络速率需跨请求保持上次采样（从 dashboard.py 拆出）。
"""

import glob
import os
import re
import socket
import threading
import time

REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs",
    "ntfs", "ntfs3", "vfat", "exfat", "jfs",
}
PRIORITY_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "x86_pkg_temp")
PHYS_IFACE_RE = re.compile(r"^(eth|ens|enp|eno|em|enx|wl|ww|wlan)")


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
        # /proc 仅 Linux 可用（macOS 本地预览时返回 0 而非整体失败）
        try:
            names = os.listdir("/proc")
        except OSError:
            return 0
        return sum(1 for name in names if name.isdigit())

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
