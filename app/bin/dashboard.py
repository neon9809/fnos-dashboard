#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 状态监视 —— 后端服务入口（纯 Python 标准库，无第三方依赖）。

实现思路参考 lcdsimple：采集器从系统接口读取数据、生成 JSON 快照，
前端按主题渲染并定时刷新。适配 fnOS FPK 环境：
  - TRIM_SERVICE_PORT / TRIM_PKGETC / TRIM_PKGVAR 定位端口与目录
  - 配置持久化在 etc/config.json，支持前端运行时修改主题

模块划分：
  dash_config  —— 配置读写与白名单校验
  dash_stats   —— /proc 系统状态采集
  dash_modules —— 日历/天气模组 + .neon-dash 扩展模组
  dash_http    —— HTTP API 与静态页
"""

import argparse
import os
import signal
import sys
import threading
from http.server import ThreadingHTTPServer

import dash_config
import dash_http
import dash_modules
import dash_stats


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
    args = ap.parse_args()

    var_dir = args.var
    try:
        os.makedirs(var_dir, exist_ok=True)
    except OSError:
        pass

    cfg = dash_config.Config(args.config, var_dir)
    stats = dash_stats.Stats()
    etc_dir = os.path.dirname(cfg.path)
    ext = dash_modules.ExtModules(var_dir, etc_dir)
    ext.preinstall(here)

    dash_http.Handler.web_root = args.web
    dash_http.Handler.config = cfg
    dash_http.Handler.stats = stats
    dash_http.Handler.var_dir = var_dir
    dash_http.Handler.etc_dir = etc_dir
    dash_http.Handler.ext = ext
    dash_http.Handler.http_port = args.port

    server = ThreadingHTTPServer((args.host, args.port), dash_http.Handler)
    server.daemon_threads = True

    def _shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print("[fnos-dashboard] v%s listening on %s:%d, web=%s, config=%s"
          % (dash_config.APP_VERSION, args.host, args.port, args.web, cfg.path),
          flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
