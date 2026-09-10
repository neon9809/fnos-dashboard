#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按官方 fnpack 格式打包 .fpk（格式规格来自 fpk-sandbox 的逆向文档）。

.fpk = gzip(tar)，外层成员按不区分大小写字母序排列：
  app.tgz（置首）、cmd/**、config/**、ICON.PNG、ICON_256.PNG、manifest、wizard/**
app.tgz = gzip(tar(app/** + config/** 副本))，条目相对 app 根目录（不带 app/ 前缀）
manifest 追加一行 checksum = md5(app.tgz) 供安装器完整性校验（值为纯 hex 摘要）。
可复现构建：tar/gzip 时间戳取 SOURCE_DATE_EPOCH（默认 0），同源码产物逐字节一致。
"""

import glob
import gzip
import hashlib
import io
import os
import sys
import tarfile


def add_bytes(tar, name, data, mtime):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def add_file(tar, path, arcname, mtime, executable=False):
    with open(path, "rb") as f:
        data = f.read()
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o755 if executable else 0o644
    tar.addfile(info, io.BytesIO(data))


def build_app_tgz(root, mtime):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # app.tgz 条目相对 app 根目录（无 app/ 前缀），config/ 副本按官方格式保留
        for base, arc_prefix in (("app", ""), ("config", "config")):
            base_dir = os.path.join(root, base)
            for dirpath, dirnames, filenames in os.walk(base_dir):
                dirnames.sort()
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    arc = os.path.join(arc_prefix,
                                       os.path.relpath(full, base_dir))
                    # fnOS CGI 要求入口脚本带执行权限
                    add_file(tar, full, arc.replace(os.sep, "/"), mtime,
                             executable=fn.endswith((".cgi", ".sh")))
    # 安装器要求 app.tgz 是 gzip 压缩的 tar；mtime=0 保证产物可复现
    return gzip.compress(buf.getvalue(), mtime=0)


def _source_mtime():
    """可复现构建：tar 成员时间戳固定（默认 0，可用 SOURCE_DATE_EPOCH 覆盖）。"""
    try:
        return int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    except ValueError:
        return 0


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    out = sys.argv[2] if len(sys.argv) > 2 else "dist/com.fnos.dashboard.fpk"
    mtime = _source_mtime()

    manifest_path = os.path.join(root, "manifest")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = f.read().rstrip("\n")

    # 内层 app.tgz
    app_tgz = build_app_tgz(root, mtime)
    checksum = hashlib.md5(app_tgz).hexdigest()
    manifest_full = (manifest + "\nchecksum = %s\n"
                     % checksum).encode("utf-8")

    # 收集外层成员
    members = [("app.tgz", app_tgz, None)]
    for pattern in ("cmd/*", "config/*", "wizard/*"):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            if os.path.isfile(path):
                rel = os.path.relpath(path, root)
                members.append((rel, None, path))
    for name in ("ICON.PNG", "ICON_256.PNG"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            members.append((name, None, path))
    members.append(("manifest", manifest_full, None))
    members.sort(key=lambda m: m[0].lower())  # 不区分大小写字母序

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data, path in members:
            if data is not None:
                add_bytes(tar, name, data, mtime)
            else:
                add_file(tar, path, name, mtime,
                         executable=name.startswith("cmd/"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(gzip.compress(buf.getvalue(), mtime=0))
    print("打包完成：%s (%.1f KB)  app.tgz md5=%s"
          % (out, os.path.getsize(out) / 1024, checksum))


if __name__ == "__main__":
    main()
