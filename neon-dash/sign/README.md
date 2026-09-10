# neon-dash 模组签名机制

`.neon-dash` 包可选携带 ed25519 数字签名。签名用于回答两个问题：

1. **完整性**：包内容自签名后未被篡改（签名与内容不符 → 安装被**硬性拒绝**）
2. **来源可信**：签名者是否在 NAS 管理员维护的「信任密钥列表」中

状态由低到高：

| 状态 | 含义 | 安装行为 | 设置页显示 |
|------|------|----------|------------|
| `unsigned` | 未签名 | 允许，黄色警示 | 灰色「未签名」 |
| `untrusted` | 签名有效，但签名者不在信任列表 | 允许，黄色警示 | 橙色「⚠ 未知签名者」 |
| `verified` | 签名有效 + 签名者可信 | 正常 | 绿色「✓ 已验证」 |
| `invalid` | 签名与内容不符 / 字段损坏 | **拒绝安装** | （不可安装） |

## 包结构

```
<mod-id>.neon-dash        # ZIP 容器
├── manifest.json         # 必需，见 MODULE_SPEC.md
├── mod.py                # 入口（manifest.entry）
├── ...其他资源
└── signature.json        # 可选，由 sign 子命令生成
```

`signature.json`：

```json
{
  "alg": "ed25519",
  "payload_sha256": "<规范化摘要>",
  "signature": "<base64 的 64 字节签名>",
  "signer": {
    "id": "开发者署名",
    "key_id": "SHA256:公钥指纹前16字节hex",
    "public_key": "<base64 的 32 字节 ed25519 公钥>"
  }
}
```

### 规范化摘要（payload_sha256）

对包内**除 `signature.json` 外**的所有条目：

```
按条目文件名（UTF-8 字节序）排序，依次送入 SHA-256：
    name 的 UTF-8 字节 + 0x00 + SHA-256(条目内容)
最终输出 hex 字符串。
```

与 zip 内部时间戳、压缩参数、条目物理顺序无关——任何人重打包只要内容一致，摘要一致。

### 密钥与指纹

- 算法：Ed25519（RFC 8032），纯 Python 实现（`app/bin/neon_crypto.py`），无第三方依赖
- 公钥：32 字节 raw，base64 传输
- `key_id` = `SHA256:` + sha256(public_key) 前 16 字节的 hex
- 私钥：32 字节 raw，base64 存于本地文件（keygen 生成，权限 600）

## 操作方法

### 开发者：打包并签名

```bash
# 1. 生成密钥对（仅需一次；私钥妥善保管，不要提交仓库）
python3 neon-dash/ndash.py keygen -o mykey.secret

# 2. 打包模块目录
python3 neon-dash/ndash.py pack tools/mock-module -o dist/mock.neon-dash

# 3. 签名
python3 neon-dash/ndash.py sign dist/mock.neon-dash \
    --key mykey.secret --signer your-name

# 4. 发行前自检
python3 neon-dash/ndash.py verify dist/mock.neon-dash
```

> 每次修改包内容后必须**重新打包并重新签名**，否则校验为 `invalid` 被拒。

### 管理员：信任开发者公钥

**项目内置信任**：`neon-dash/keys/neon.pub`（官方签名者，key_id `SHA256:9002f14abbf7506c`）
已内置在后端中随项目分发——官方签名（如示例模组）开箱即为「✓ 已验证」，无需配置。
内置项不受设置页增删影响。

其他开发者的公钥加入方式，设置页（`http://127.0.0.1:8199/settings`）→ 模组 →
「信任的签名密钥」：粘贴公钥（base64）与备注，添加后其签名的模组显示「✓ 已验证」。

也可以直接编辑 `etc/trusted_keys.json`：

```json
[
  {
    "key_id": "SHA256:xxxxxxxxxxxxxxxx",
    "name": "开发者名",
    "public_key": "<base64>",
    "added_at": 1789000000.0
  }
]
```

### 管理员：安装时的验证行为

- `invalid` → 后端直接拒绝安装，返回错误原因
- `unsigned` / `untrusted` → 前端弹出风险告知（见下），管理员确认后才安装/启用
- 验证结果持久化在 `var/modules/<id>/.verify.json`，列表 API 透出

## 风险告知（启用/安装模组时展示）

> 1. 模组包含第三方开发者编写的**可执行代码**，启用后将在本机（NAS）以后端进程身份运行，
>    可能访问系统资源与网络，请**自行甄别开发者与代码来源的风险**。
> 2. 请**谨慎向模组提交个人关键凭证、密钥、密码**等敏感信息——模组作者可在其代码中读取这些配置。
> 3. 仅安装可信来源的 .neon-dash 包，优先选择「✓ 已验证」且签名者在信任列表中的模组。

签名机制防的是「篡改」与「冒名」，**不背书模组本身的行为安全**：签名只证明包出自该私钥持有者。
