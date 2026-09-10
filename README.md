# fnOS 状态监视（fnos-dashboard）

参考 [lcdsimple](../lcdsimple) 的实现原理（采集器 → JSON 快照 → 主题化渲染 → 定时刷新），
为飞牛 fnOS 开发的系统状态监视应用，遵循 FPK 规范打包。

## 功能

### Web 面板（浏览器，可鼠标键盘交互）
- CPU（总量 + 每核心 + 负载）、内存/Swap、存储卷（`/vol*` 自动识别）、网络速率、温度传感器、系统信息
- **自定义显示主题**：6 款内置主题（午夜蓝 / 石墨黑 / 翡翠绿 / 日光橙 / 樱粉 / 云白）+ 12 色强调色 + 任意自定义颜色
- 卡片显示开关、刷新间隔、温度单位（℃/℉）
- 面板为纯查看端，跟随全局配置渲染

### 设置页（`/settings`，仅限 127.0.0.1 访问）
- **左右分栏**：左侧配置（4 个选项卡），右侧为**显示屏实时预览**（帧缓冲真实画面，含旋转方向）
- **主题**：主题卡片 + 强调色 + 温度单位
- **显示**：面板卡片开关、刷新频次、页面轮换时间、**显示方向旋转（0°/90°/180°/270°）**
- **显示器**：帧缓冲输出开关、屏幕尺寸（英寸，按 PPI 修正缩放）、设备/PIL/渲染进程状态
- **模组**：内置模组开关与配置；扩展模组（.neon-dash）安装 / 启用 / 卸载 / 信任密钥管理
- 左下角「保存并同步到显示屏」，约 2 秒内生效

### 显示器模式（HDMI/VGA 直连屏幕，无键鼠交互）
- 参考 lcdsimple 直接渲染到 `/dev/fb0`，与 Web 面板共用同一采集后端与主题配置
- **单屏布局**：按 fb 实际分辨率缩放；配置屏幕尺寸（英寸）后按 PPI 修正缩放倍率
- **自动翻页轮换**：系统状态页 + 启用的模组页，按「轮换间隔」自动切换，页脚指示点 + 倒计时
- 字体双后端：优先 PIL + 系统 CJK 字体（中文完整显示）；无 PIL 时回退内置 5x7 ASCII 字体

### 显示模组
**内置模组**（设置页勾选启用）：

| 模组 | 说明 | 数据来源 |
|------|------|----------|
| 日历 | 当月月历 + 今日高亮 + 大字时钟 | 本地计算 |
| 天气预报 | 实时温度/天气 + 逐小时预报 + 生活指数（**花粉过敏指数**等 10 种可选），城市/位置可配置 | **和风天气**（需 API Key，`dev.qweather.com` 注册；免费 Open-Meteo 兜底） |
| Coding Plan 用量 | 订阅额度使用进度条 | 通用 HTTP 探针：配置 JSON 接口 URL / Bearer Token / 已用与总额度的 JSON 路径 / 重置日期 |

**扩展模组（.neon-dash）**：社区/自定义模组以 `.neon-dash`（ZIP）包安装，
包含 `manifest.json` + Python 入口 `mod.py`（实现 `get_payload(config)` 返回通用渲染数据）。
支持 **ed25519 签名验证**（未签名/未知签名者 → 风险告知后放行；签名不符 → 拒绝安装），
启用时弹出风险告知。`app/bin/*.neon-dash` 为**官方预装包**（随应用启动自动安装，版本变化时覆盖）。

内置官方模组：

| 模组 | 说明 |
|------|------|
| volc-plan | 火山方舟 Plan 用量：Coding Plan / Agent Plan 的 5h/周/月额度窗口与重置时间（OpenAPI V4 签名，AK/SK 配置） |
| glm-plan | 智谱 GLM Coding Plan 用量：5h/周 token 窗口、MCP·联网工具月额度、套餐档位（bigmodel.cn / 国际站 z.ai，API Key 配置） |

开发者工具与规范见 [`neon-dash/`](neon-dash/):
- [`neon-dash/preview.py`](neon-dash/preview.py)：**模组开发实时预览器**（本地模拟显示器渲染，无需真机）
- [`neon-dash/ndash.py`](neon-dash/ndash.py)：打包 / 签名 / 验证一体工具
- [`neon-dash/MODULE_SPEC.md`](neon-dash/MODULE_SPEC.md)：模组开发规范
- [`neon-dash/sign/README.md`](neon-dash/sign/README.md)：签名机制说明与操作方法
- [`neon-dash/example/`](neon-dash/example/)：官方模组源码（volc-plan / glm-plan）
- 官方签名者公钥内置信任（`neon-dash/keys/neon.pub`），官方模组开箱「✓ 已验证」

## 目录结构

```
fnos-dashboard/
├── app/                    # 打包后 → target（TRIM_APPDEST）
│   ├── bin/
│   │   ├── dashboard.py    # 后端：采集 + API + 静态页（纯 Python 标准库）
│   │   ├── fb_render.py    # 显示器渲染器（/dev/fb0，轮换翻页 + 旋转）
│   │   ├── neon_crypto.py  # .neon-dash 签名验证（纯标准库 ed25519）
│   │   └── *.neon-dash     # 官方预装模组（mock / volc-plan / glm-plan）
│   ├── web/                # Web 面板 + 设置页（原生 HTML/CSS/JS，无构建依赖）
│   └── ui/                 # 桌面图标配置（desktop_uidir）
│       ├── images/icon-{64,256}.png
│       └── config
├── neon-dash/              # 模组开发者套件
│   ├── ndash.py            # 打包/签名/验证一体工具
│   ├── MODULE_SPEC.md      # 模组开发规范
│   ├── keys/neon.pub       # 官方签名者公钥（内置信任）
│   └── sign/README.md      # 签名机制说明与操作方法
├── cmd/                    # FPK 生命周期脚本（main / install / uninstall / upgrade / config）
├── config/                 # privilege（run-as package）+ resource
├── wizard/                 # 安装向导 / 配置向导（主题、刷新间隔、温度单位）
├── tools/                  # 图标生成器等
├── manifest                # com.fnos.dashboard, platform=all, service_port=8199
├── build.sh                # 构建入口
├── ICON.PNG / ICON_256.PNG # 由 build.sh 生成
```

## 构建与安装

```bash
./build.sh        # 图标 + 官方模组打包签名（需本机私钥）+ 赋权 + （若安装了 fnpack）打包 .fpk
```

### CI 构建（GitHub Actions）

`.github/workflows/fpk-release.yml`：推送 `v*.*.*` 标签或手动触发，自动完成
冒烟测试 → 图标 → 官方模组打包签名 → FPK 打包 → 结构/完整性校验 → 附着到 Release。

签名私钥通过仓库 **Secrets → Actions → `NDASH_SIGNING_KEY`** 配置
（内容为 keygen 生成的 base64 私钥；不配置则产出未签名包）。

本地预览（无需 fnOS）：`python3 app/bin/dashboard.py --port 8199 --web app/web`
- fnOS 安装后自动创建 `com.fnos.dashboard` 桌面图标，点击打开 Web 面板
- 显示器模式：在 Web 面板「设置 → 显示器模式」开启；`cmd/main` 会在启动时拉起 `fb_render.py`

## 显示器模式的系统适配

1. **权限**：安装脚本（root 阶段）写入 udev 规则
   `/etc/udev/rules.d/99-fnos-dashboard-fb.rules`（`SUBSYSTEM=="graphics"` MODE 0666）
   并对当前开机立即 `chmod 666 /dev/fb0`，使 package 用户可写 framebuffer；卸载时移除。
2. **中文渲染**：`apt install python3-pil` 后自动使用系统字体
   （拉丁：Noto Sans Mono / DejaVu；CJK：DroidSansFallback）。
   未安装 PIL 时自动回退内置 5x7 ASCII 字体（系统页完整可用，中文模组显示受限）。
3. **分辨率/PPI**：从 `/sys/class/graphics/fb0/{virtual_size,bits_per_pixel,stride}` 读取；
   默认按 `min(W/1280, H/800)` 缩放，配置屏幕英寸后按 `对角线像素/英寸` 估算 PPI 再修正。
4. **数据流**：渲染器仅依赖 `http://127.0.0.1:8199/api/*`，后台线程拉取（天气等慢接口不阻塞绘制），
   后端离线时保持最后画面并显示 OFFLINE 徽标。

## API

| 端点 | 方法 | 访问 | 说明 |
|------|------|------|------|
| `/` `/assets/*` | GET | 局域网 | 查看面板 |
| `/api/status` | GET | 局域网 | 系统 JSON 快照 + 当前配置 |
| `/api/modules` | GET | 局域网 | 已启用模组数据（内置缓存 + 扩展模组 payload） |
| `/api/config` | GET | 局域网 | 读取显示配置 |
| `/settings` | GET | **仅 127.0.0.1** | 设置页 |
| `/api/settings` | POST | **仅 127.0.0.1** | 保存配置（白名单校验，原子写盘） |
| `/api/fb/info` `dump.png` | GET | **仅 127.0.0.1** | 显示器状态 / 帧缓冲实时预览 |
| `/api/ext/*` | GET/POST | **仅 127.0.0.1** | 扩展模组安装/启停/卸载、信任密钥管理 |

安全：设置页与全部管理接口仅限 127.0.0.1（远程一律 403）；静态文件有路径穿越防护；
配置更新仅接受白名单字段；模组安装执行签名验证（篡改包直接拒绝）并需风险告知确认。

## 配置文件

`etc/config.json`（FPK 安装后由系统挂载到 `@appconf`），示例：

```json
{
  "theme": "midnight", "accent": "", "refresh": 2, "temp_unit": "C",
  "fb_enabled": true, "rotate_seconds": 10, "screen_inches": 0, "fb_rotate": 0,
  "modules": { "calendar": true, "weather": true, "coding": false },
  "weather_city": "北京",
  "coding": { "name": "", "url": "", "token": "",
              "path_used": "", "path_total": "", "reset": "" },
  "ext_modules": { "volc-plan": { "enabled": true, "config": { "plan_type": "coding" } } }
}
```

## 已在真实 fnOS 上验证 （v1.2.0604）

- Debian 12 / Python 3.11 / x86_64，`/vol1`(btrfs) `/vol2`(ext4) 双卷识别
- CPU 差分采样、4 核心条、8 温度传感器（CPU 优先排序）、真实网卡速率
- Web 端主题切换实时同步到显示器；多页轮换（状态/日历/天气/扩展模组）实测通过
- 设置页四选项卡 + 实时预览 + 保存同步全链路（经 SSH 隧道以 127.0.0.1 身份实测）
- 远程访问管理接口返回 403；配置白名单校验、路径穿越防护、非法主题拒绝
- ed25519 签名：RFC 8032 全部测试向量通过；篡改包拒装；官方包「✓ 已验证」
- 显示方向 90° 旋转实测（画面整体旋转，4 页轮换不受影响）
- 扩展模组：安装 → 风险告知 → 启用 → 显示器轮换展示（官方 volc-plan / glm-plan 预装实测）
