---
name: fnos-fpk-packaging
description: 把 Docker 项目打包为飞牛 fnOS 应用（.fpk）的完整工程实践。包含 fpk-sandbox 工具复用、manifest/privilege/resource/compose/cmd/wizard 契约、向导端口动态生效的完整链路、CI 自动发布（镜像 + fpk + Release）、以及实战踩坑清单（包用户 docker 权限、compose 项目名、容器重建时机、healthcheck 端口跟随等）。当用户想给 Docker 项目打包 fnOS 应用、修改现有 fpk、或排查安装后配置不生效问题时使用。
---

# fnOS Docker 应用打包 Skill

## 何时使用

- 把 Docker 项目打包为飞牛 fnOS 的 `.fpk` 应用包
- 修改/升级现有 fpk（cf-dns-select-fpk 即参考实现）
- 排查「安装后配置不生效 / 端口不对 / 容器不重建」类问题
- 搭建 CI：随 git tag 自动构建镜像 + 打包 fpk + 发布 Release

## 工具复用：fpk-sandbox

`fpk-sandbox/` 是通用零依赖 CLI（仅需 Node ≥ 18），**直接整目录复制到新项目**即可：

```bash
cp -r fpk-sandbox /path/to/new-project/fpk-sandbox
cd new-project/fpk-sandbox
chmod +x bin/fpksb.js
node bin/fpksb.js --help
```

四个命令：

- `fpksb init <appname> --image <img> --port 8080 --net host|bridge` — 生成完整骨架
- `fpksb check <dir>` — 九大类一致性校验（errors 阻断 / warnings 提示）
- `fpksb build <dir> -o out.fpk` — 打包（复刻官方 fnpack 格式，含 `checksum=md5(app.tgz)`）
- `fpksb unpack <fpk> --list` — 检查包内容；`bash test/run.sh` 冒烟测试

## 标准目录结构

```
{appname}-fpk/
├── manifest                    # 元数据（根目录无扩展名键值文件）
├── ICON.PNG / ICON_256.PNG     # 64 / 256 图标
├── app/
│   ├── docker/
│   │   ├── docker-compose.yaml
│   │   ├── .env                # COMPOSE_PROFILES / BIND_PORT（随包携带）
│   │   └── env/app.env         # 容器运行参数（随包携带默认值）
│   └── ui/config               # 桌面入口（port=${wizard_port} 动态解析）
├── cmd/                        # 生命周期脚本（必须 +x）
│   ├── main                    # start/stop/status（status 返回 0 运行 / 3 停止）
│   ├── install_callback        # 安装后：向导值写入配置 + 重建容器
│   ├── config_callback         # 应用设置提交后：同上
│   ├── upgrade_callback        # 升级后从主副本恢复参数
│   └── 其余 *_init / uninstall_* 存根
├── config/
│   ├── privilege               # 身份与组
│   └── resource                # docker-project 声明
└── wizard/
    ├── install                 # 安装向导表单
    └── config                  # 应用设置表单
```

## 关键契约（每条都是踩坑换来的）

### config/privilege —— 包用户必须加入 docker 组

```json
{
  "defaults": { "run-as": "package" },
  "username": "my-app",
  "groupname": "my-app",
  "join-groups": ["docker"]
}
```

- **不要用 `run-as: root`**（个人开发者上架不推荐）。官方机制是 `join-groups`：
  包用户保持最小权限，仅加入 docker 系统组获得 `/var/run/docker.sock` 访问权。
- 缺了 `join-groups: ["docker"]`，生命周期脚本里所有 `docker`/`docker compose`
  调用全部 permission denied，且常被 `|| true` 静默吞掉——表现为「配置写对了但容器永不生效」。

### config/resource —— 项目名是契约

```json
{ "docker-project": { "projects": [ { "name": "my-app", "path": "docker" } ] } }
```

应用中心以 `name` 作为 compose 项目名启动项目。**compose 文件顶部必须声明同名**：

```yaml
name: my-app
services: ...
```

否则脚本里 `docker compose up -d`（不加 `-p` 时默认用目录名当项目名）会作用到
错误项目，重建容器时容器名冲突静默失败。验证：设备上 `docker compose ls -a`。

### compose 双模式（host / 端口映射）

- 两个 service 用 `profiles: ["host"]` / `profiles: ["bridge"]` 互斥，`container_name` 同名；
- `.env` 提供 `COMPOSE_PROFILES=host` 与 `BIND_PORT=xxxx`；
- **host 模式**：`network_mode: host`，`ports` 无效，端口由 `env_file` 里的
  `DSA_LISTEN` 类环境变量决定；
- **bridge 模式**：`ports: "${BIND_PORT:-8080}:8080"`，容器内固定 8080，
  环境变量保持 `0.0.0.0:8080`；
- `env_file: ${TRIM_APPDEST}/docker/env/app.env`，挂载 `${TRIM_PKGVAR}/data:/app/data`。

### 镜像：监听参数别硬编码

`CMD []` + 程序内 flag 默认值 + 环境变量覆盖（未显式传 flag 时读 env），
否则向导端口永远不生效。健康检查端口也要跟随 env 解析：

```dockerfile
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD port="${LISTEN##*:}"; wget -qO- "http://127.0.0.1:${port:-8080}/api/health" >/dev/null 2>&1 || exit 1
```

### 生命周期脚本要点

- `cmd/main`：`start`（幂等）、`stop`、`status`（运行 0 / 未运行 3 / 未知 1）。
- **主副本 + 随包副本双写**：主副本 `${TRIM_PKGETC}/app.env`（升级保留）、
  compose 引用的 `${TRIM_APPDEST}/docker/env/app.env`。`start` 时同步 + 比对容器 env，
  不一致就 `docker rm -f` + `compose up -d`（自动纠正）。
- **install_callback / config_callback 必须无条件用向导值重写配置**（不能带
  `[ ! -f ]` 守卫）——应用中心可能在回调前就用随包默认值拉起过容器并生成默认主副本。
- 写配置后**强制重建容器**：`docker rm -f` + `compose up -d`；失败不能静默，
  写 `TRIM_TEMP_LOGFILE`。
- 向导字段命名 `wizard_*`：`wizard_port` 要校验 100–65535；端口占用检测可用
  bash 内建 `/dev/tcp`，无需 nc。
- 环境变量：`TRIM_APPDEST`（应用目录）/ `TRIM_PKGETC`（配置）/ `TRIM_PKGVAR`（数据）/
  `TRIM_PKGTMP` / `TRIM_PKGHOME` / `TRIM_TEMP_LOGFILE`，禁止硬编码路径。
- 打包前 `chmod +x cmd/*`（git 保留 100755 可执行位）。

### manifest 要点

`appname` 稳定不改名；`source=thirdparty`；`platform=all`（无原生依赖时）；
`checkport=false`（端口由向导动态决定时）；`ctl_stop=true`；changelog 保留历史条目。
打包后 `checksum` 字段由工具生成，勿手写。

## 版本与 CI 发布

**三处版本必须一致**，CI 校验：`git tag v0.3.4` = `manifest.version 0.3.4`
= `compose image: ...:0.3.4`。

`.github/workflows/docker-publish.yml` 参考实现（tag 触发 + workflow_dispatch）：

1. 构建前端（如需要）→ QEMU/Buildx → 打 `:version`、`:vX.Y.Z`、`latest`
   （latest 仅非预发布 tag）→ 推送镜像；
2. `fpksb check` → 校验已发布镜像存在 → 三方版本一致性 → 冒烟测试；
3. `fpksb build` + `sha256sum` → upload-artifact → 附着 GitHub Release（仅 tag）。

额外防护：构建 job 第一步校验 tag 与 manifest 版本一致，不一致直接失败——
防止将来误推旧 tag 把旧代码构建成 `latest` 覆盖正常镜像。

## 实战踩坑清单（本仓库血泪史）

| # | 症状 | 根因 | 解法 |
|---|------|------|------|
| 1 | 配置写入正确但容器永不重建，端口一直默认 | 包用户不在 docker 组，脚本 docker 调用全部静默失败 | privilege 加 `join-groups: ["docker"]` |
| 2 | 回调 compose up 报容器名冲突被吞掉 | compose 未声明 `name:`，与 docker-project 项目名不一致 | compose 顶部 `name:` 与 resource 一致 |
| 3 | 应用中心在回调前用默认 env 拉起容器 | 生命周期顺序：拉起项目 → 回调写配置 | 回调无条件重写 + 容器存在即重建；main start 做 reconcile |
| 4 | `CMD ["-listen","0.0.0.0:8080"]` 压掉环境变量 | 镜像硬编码启动参数 | `CMD []` + flag 默认值 + env 覆盖 |
| 5 | 改端口后容器 healthy 但健康检查打 8080 | healthcheck 端口硬编码 | 从 env 解析端口 + 默认回退 |
| 6 | 出问题看不到任何报错 | `|| true` 吞错 | 失败写 `TRIM_TEMP_LOGFILE` |
| 7 | fnpack 提示 cmd/main 无执行位 | 源码未加 x | `chmod +x cmd/*` |
| 8 | 推旧 tag 把旧代码打成 latest | workflow 无版本防护 | 构建前校验 tag == manifest.version |

## 设备验证清单

```bash
id <包用户名>                                  # 应含 docker 组
cat /vol2/@appcenter/<app>/docker/env/app.env   # env 文件内容
docker inspect <容器名> --format '{{range .Config.Env}}{{println .}}{{end}}'
docker compose ls -a                            # 项目名必须一致
ss -lntp | grep <端口>                           # 实际监听
```

## 官方文档

- 开发指南：https://developer.fnnas.com/docs/guide/
- 开放 API：https://developer.fnnas.com/api/overview/
- 权限模型（run-as / join-groups / root 模式）：https://developer.fnnas.com/docs/core-concepts/privilege/
