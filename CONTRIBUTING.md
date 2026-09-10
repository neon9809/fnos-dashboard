# 参与开发（Contributing Guide）

欢迎加入 fnos-dashboard 开发！无论是修 Bug、新功能、新模组还是文档，都欢迎提交 Pull Request。
本文是面向整个项目的开发文档；显示器模组（.neon-dash）的专项规范见 [neon-dash/MODULE_SPEC.md](neon-dash/MODULE_SPEC.md)。

## 架构总览

```
fnOS 设备
├── dashboard.py          后端入口：装配并启动 HTTP 服务
│   ├── dash_config.py    配置读写 / 白名单校验（etc/config.json）
│   ├── dash_stats.py     /proc 采集（CPU/内存/磁盘/网络/温度）
│   ├── dash_modules.py   日历 / 天气（Open-Meteo + 和风）/ .neon-dash 扩展模组
│   └── dash_http.py      HTTP API / 静态页 / fb 渲染进程管理 / 帧预览
├── fb_render.py          显示器渲染器：拉取 API → 绘制 /dev/fb0
├── neon_crypto.py        .neon-dash ed25519 签名验证（纯标准库）
├── app/web/              前端：面板（app.js）+ 设置页（settings.js），原生 JS 无构建
├── app/ui/               桌面入口（CGI 反代 → 127.0.0.1:8199）
├── cmd/                  FPK 生命周期脚本（start/stop/install/upgrade/config）
├── neon-dash/            模组开发者套件（ndash.py 打包签名 / preview.py 预览器）
└── tools/pack_fpk.py     .fpk 打包（官方格式，可复现构建）
```

数据流：前端/渲染器 → `GET /api/status`、`/api/modules` → 后端采集 + 模组缓存 → JSON。
显示器与 Web 面板共用同一后端与配置，主题/强调色实时同步。

## 开发环境

要求：Python ≥ 3.9（纯标准库即可跑通全部后端功能）、可选 Pillow（用于图标生成与 PIL 渲染后端调试）。

```bash
# 启动后端（macOS/Linux 均可；macOS 下部分 /proc 数据显示 "--" 属正常）
python3 app/bin/dashboard.py --port 8199 --web app/web

# 设置页（含帧缓冲实时预览）：http://127.0.0.1:8199/settings
# 面板：http://127.0.0.1:8199/

# 显示器渲染离线预览（无需真机 fb）：
python3 neon-dash/preview.py

# 指定配置/数据目录（模拟 fnOS 布局）：
python3 app/bin/dashboard.py --port 8199 --web app/web \
    --config /tmp/t/etc/config.json --var /tmp/t/var
```

## 各模块开发要点

### 后端（dash_*.py）
- **配置**：所有可写字段必须在 `dash_config.Config.update()` 白名单校验、`_sanitize()` 清洗，
  禁止直接透传用户输入。新增配置字段 = `DEFAULT_CONFIG` + `_sanitize` + `update` 三处。
- **访问模型**：管理接口仅限 127.0.0.1（CGI 反代经 fnOS 登录态校验回源）；
  非可信请求敏感字段必须经 `_public_cfg` 掩码。
- **渲染进程管理**：`fb_restart_needed(prev, cur)` 为值比较语义——只有
  `fb_enabled / fb_rotate / screen_inches` 真变化才重启渲染进程，其余配置一律热加载
  （渲染器每秒轮询配置）。新增「需重启」字段前先考虑能否热加载。

### 显示器渲染（fb_render.py）
- 所有页面以 **1280×800 基准坐标 × `ui_scale`** 绘制，需在 800×600 到 4K 均可用。
- **双后端**：PIL（PilCanvas）与 ASCII 回退（AsciiCanvas）行为必须一致；
  PIL 下测宽用 `draw.textlength`（真实字宽），ASCII 用 5x7 估宽公式，二者不要混用。
- 帧输出有差量缓存（内容不变跳过写屏），任何绘制异常必须可见（ERR 帧 + 强制刷帧），
  不得让画面静默冻结。
- 主题调色板正本在 `app/web/assets/style.css` 的 `--bg1`，四处同步：
  `style.css` / `app.js THEMES` / `settings.js THEMES` / `fb_render.PALETTES`。

### 前端（app/web/）
- 原生 ES5 风格 JS，无构建步骤；改动后 `node --check` 校验语法。
- 设置页保存载荷为**全量字段**，后端做白名单校验——新增字段需同步 `settings.js save()`。

### 模组（neon-dash/）
- 规范见 [neon-dash/MODULE_SPEC.md](neon-dash/MODULE_SPEC.md)；`get_payload(config)` 返回
  `{title, subtitle, lines, bars, text}`，渲染上限 lines 8 条 / bars 4 条（注意截断）。
- 模组版本号变更才会被真机 `preinstall` 覆盖升级；签名由 CI 用 `secrets.NDASH_SIGNING_KEY` 完成。
- 官方模组（volc-plan / glm-plan）源码在 `neon-dash/example/`，修改后必须升版本号。

## 提交前自检清单

```bash
python3 -m py_compile app/bin/*.py                  # 后端语法
node --check app/web/assets/app.js && node --check app/web/assets/settings.js
bash -n cmd/* build.sh                              # 生命周期脚本
python3 tools/pack_fpk.py . /tmp/a.fpk && python3 tools/pack_fpk.py . /tmp/b.fpk
cmp /tmp/a.fpk /tmp/b.fpk                           # 打包可复现（时间戳固定）
```

功能自查：面板 / 设置页 / 预览三处过一遍；改显示器相关代码时双后端（有/无 PIL）
与 0/90/180/270° 旋转都要覆盖；改配置字段时验证白名单拒绝非法值。

## 构建与发布

```bash
export NDASH_KEY=/path/to/neon-dash.secret   # 官方模组签名私钥（必填）
./build.sh                                    # 图标 → 模组打包签名 → .fpk（若装 fnpack）
```

正式发布流程（维护者）：
1. 更新 `manifest` 的 `version` 与 `changelog`（时间倒序，一行一版）。
2. 提交并打 tag：正式版 `vX.Y.Z`（CI 也接受 `beta-X.Y.Z`），tag 必须与 `version` 一致。
3. CI（`.github/workflows/fpk-release.yml`）自动：冒烟测试 → 图标 → 模组签名 →
   fpk 打包 → 结构校验 → 附着到 GitHub Release。

## Pull Request 流程

1. Fork / 建分支（`feat/xxx`、`fix/xxx`），一个 PR 聚焦一件事。
2. 提交信息用一句话说明**用户可感知的变化**（如 `beta-0.0.16：修复保存后显示器卡死…`）。
3. PR 描述里写清：改了什么、为什么、如何验证（贴自检清单结果或截图）。
4. 涉及显示器渲染请附 preview.py 或真机帧预览截图。
5. 不要提交：私钥、API Key、真机配置、临时产物（dist/、__pycache__、.log/）。

## 安全基线

- 新增任何接受用户输入的接口：白名单校验 + 长度截断，遵循既有访问模型（管理面仅本机）。
- 引入第三方依赖前先讨论——本项目以「纯 Python 标准库、零构建」为设计原则。
- 发现安全问题请勿公开提 Issue，通过项目主页联系方式私下报告。

有拿不准的设计，先开 Issue/Discussion 聊一下再动手，欢迎交流！
