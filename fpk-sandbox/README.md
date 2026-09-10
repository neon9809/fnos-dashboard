# fpk-sandbox（`fpksb`）

飞牛 fnOS 应用**沙箱配置 CLI**：生成、校验、打包 Docker 应用的 `.fpk` 安装包。
零依赖，仅需 Node.js ≥ 18。

> 依据官方文档实现：<https://developer.fnnas.com/docs/quick-started>

## 为什么需要它

一个 fnOS 应用的"沙箱配置"由四部分契约组成：

| 组成 | 文件 | 作用 |
| --- | --- | --- |
| 身份隔离 | `config/privilege` | 专用包用户/用户组（run-as=package），最小权限运行 |
| 资源声明 | `config/resource` | Docker 项目、共享目录等系统能力的显式声明 |
| 目录隔离 | `TRIM_*` 环境变量 | etc/var/tmp/home 等目录经系统注入，禁止硬编码路径 |
| 生命周期 | `cmd/*`、`wizard/*` | 安装/启停/升级/卸载脚本与安装向导表单 |

手写这些文件容易出错且难自查。`fpksb` 把它们模板化生成、一致性校验，
并复刻官方 `fnpack build` 的打包格式（含 manifest 的 `checksum = md5(app.tgz)` 完整性字段）。

## 安装

```bash
cd fpk-sandbox
chmod +x bin/fpksb.js
sudo ln -sf "$(pwd)/bin/fpksb.js" /usr/local/bin/fpksb   # 可选
```

## 命令

### `fpksb init <appname> --image <img>` — 生成沙箱配置项目

```bash
fpksb init my-app --image ghcr.io/example/my-app:latest \
  --port 8080 --net host            # 或 --net bridge
```

选项：`--port` 服务端口；`--net host|bridge`（host 直接继承宿主网络栈，适合 IPv6 测速类应用；
bridge 用 `${TRIM_SERVICE_PORT}` 做端口映射）；`--container-name`；`--data-mount /app/data`
把应用沙箱 var 目录挂进容器做持久化；`--data-share` 额外声明用户可见共享目录；
`--platform all|x86|arm`；`--display-name/--desc/--maintainer`。

生成内容：`manifest`、`config/privilege`、`config/resource`、`app/docker/docker-compose.yaml`、
`app/ui/config` 与图标、全套 `cmd/*` 生命周期脚本、空 `wizard/`。

### `fpksb check [dir]` — 校验沙箱配置

覆盖九大类规则（错误导致打包失败，警告仅提示）：

1. fnpack 打包清单（必需文件/目录）
2. manifest 字段规范（appname/version/platform/service_port 等）
3. privilege 最小权限模型（root 运行告警）
4. resource 声明与实际文件对应（docker-project 路径存在且有 compose；data-share 顶级目录名）
5. 桌面入口一致性（入口 ID 前缀、图标存在性、端口与 manifest 一致、desktop_applaunchname 有效）
6. compose 一致性（container_name 缺失告警、host 与 ports 冲突、`${TRIM_SERVICE_PORT}` 使用、
   env_file 是否有生成逻辑）
7. cmd/main 必须处理 `status` 且返回 0/3；禁 curl|bash
8. wizard JSON 结构、已知字段类型、TRIM_ 保留前缀检查
9. 图标 PNG 尺寸（64/256）与 1MB 上限

`--json` 输出机器可读结果，便于 CI。

### `fpksb build [dir] [-o out.fpk] [--packer auto|internal|fnpack]`

- `auto`（默认）：优先调用官方 `fnpack`（PATH / `/usr/local/bin/fnpack` / `$FNPACK_BIN`），
  找不到则回退内置打包器；
- `internal`：内置打包器，逐字节复刻官方格式——外层 tar.gz（成员排序一致）+
  内层 `app.tgz`（`app/** ∪ config/**` 复制）+ manifest 注入 `checksum = md5(app.tgz)`；
- `fnpack`：强制官方工具，找不到即报错。

打包前自动执行 check（`--no-check` 可跳过）。

### `fpksb unpack <file.fpk> [--list] [--deep] [-C dir]`

查看/解包 `.fpk`；`--deep` 连内层 `app.tgz` 清单一起列出。

### `fpksb icons [dir]`

生成圆角渐变云朵图标：`ICON.PNG`(64)、`ICON_256.PNG`(256) 及 `app/ui/images/icon_{64,256}.png`。

### `fpksb vars`

打印沙箱环境变量速查表（`TRIM_APPDEST`、`TRIM_PKGVAR`、`TRIM_SERVICE_PORT`、
`TRIM_DATA_SHARE_PATHS`、向导 `wizard_*` 变量等）。

## 测试

```bash
bash test/run.sh                 # 基础冒烟测试
FNPACK_BIN=/path/to/fnpack bash test/run.sh   # 附带官方工具交叉校验
```

测试覆盖 init→check→build→unpack 全链路，并在存在官方 `fnpack` 时断言两者产物成员集合一致。

## 格式逆向说明

`.fpk` = gzip(tar)，外层成员：`app.tgz`（置首）、`cmd/**`、`config/**`、
`ICON.PNG`、`ICON_256.PNG`、`manifest`、`wizard/**`，按不区分大小写字母序排列；
`app.tgz` = gzip(tar(`app/**` + `config/**` 副本))；
`manifest` 在打包时被追加一行 `checksum = md5(app.tgz)` 用于安装器完整性校验。
以上均已通过与官方 fnpack v1.0.0 / v1.2.3 产物对比验证。
