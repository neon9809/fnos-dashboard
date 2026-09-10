# .neon-dash 模组开发规范

`.neon-dash` 是状态监视的扩展模组包格式：一个 ZIP 容器，内含声明文件与一个 Python 入口。
模组向显示器轮换页面中贡献一个独立页面，也可在面板侧提供数据。

## 1. 包结构

```
<mod-id>.neon-dash          # ZIP
├── manifest.json           # 必需
├── mod.py                  # 入口（manifest.entry 指定，默认 mod.py）
└── ...任意资源文件
```

> 打包/签名工具：`python3 neon-dash/ndash.py pack|sign|keygen|verify`（见 sign/README.md）
> 完整实例参考 [`neon-dash/example/volc-plan`](example/volc-plan)（火山方舟用量模组）。

## 2. manifest.json

```json
{
  "id": "mock",                       // 必需。[a-z0-9_-]{1,32}，全局唯一
  "name": "示例模组",                  // 必需，显示名
  "version": "1.0.0",                 // 必需
  "author": "you",                    // 建议
  "desc": "一句话说明",                // 建议
  "entry": "mod.py",                  // 必需，Python 入口
  "config_schema": [                  // 可选：设置页动态渲染的配置表单
    { "field": "count", "type": "number", "label": "自定义计数", "default": 3 },
    { "field": "city",  "type": "text",   "label": "城市",     "default": "北京" }
  ]
}
```

`config_schema.type`：`text` | `number`（设置页据此渲染输入框，值校验由模组自行兜底）。
用户配置持久化在 `etc/config.json` 的 `ext_modules.<id>.config`，调用时传入 `get_payload`。

## 3. 入口接口

```python
def get_payload(config: dict) -> dict:
    """返回本模组页面的渲染数据（dict）。

    - 由后端在模组页面被轮换展示前调用，结果缓存 30 秒
    - 网络请求等耗时操作请自行控制超时（建议 < 5s），异常会被捕获并以 error 页展示
    - config 即设置页用户填写的键值（str/number/bool）
    """
    return {
        "title": "页面大标题（≤24 字符）",
        "subtitle": "可选副标题",
        "lines": [                       # 键值表格（≤8 行）
            ["标签", "值"],
        ],
        "bars": [                        # 进度条（≤4 条，0-100）
            ["标签", 42],
        ],
        "text": "底部补充说明（≤44 字符）",
    }
```

### payload 渲染规则

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | str | 页面标题，超长截断 |
| `subtitle` | str | 标题下小字 |
| `lines` | `[k, v][]` | 右对齐值表格，每项 `[标签, 值]`，值转字符串显示 |
| `bars` | `[label, pct][]` | 进度条，`pct` 0–100，≥80 变警示色 |
| `text` | str | 底部一行说明 |
| `error` | str | 出现时整页渲染为错误态（模组内部抛异常时后端自动填充） |

布局由渲染器统一负责（自动缩放/主题化/分页指示），模组只产出数据。

## 4. 生命周期与约束

- **加载**：安装时解压到 `var/modules/<id>/`，首次调用 `importlib` 加载 entry
- **调用频率**：后端对每个模组的 payload 缓存 30s；显示器页面轮换时读取
- **执行身份**：以后端进程用户（package 用户）运行——无 root 权限
- **失败隔离**：`get_payload` 抛异常不影响其他页面，模组页显示错误信息
- **禁止**：写 `/etc`、`/usr` 等系统路径；监听端口；读其他应用目录

## 5. 设计建议

- 页面信息密度克制：8 行表格 + 4 条进度条已是一屏上限
- 数值格式化在模组侧完成（渲染器原样显示字符串）
- 需要鉴权的 API：凭据放在 `config_schema` 中由用户填写，**不要硬编码**；
  并在模组说明中提醒用户谨慎提交敏感凭证
- 版本迭代只改内容时无需重新签名——但**任何包内容变化都必须重新签名**，
  否则签名校验为 invalid 被拒装

## 6. 本地自测

```bash
# 实时预览器：本地模拟显示器渲染（与 fb_render 同一套代码，所见即实机）
python3 neon-dash/preview.py          # http://127.0.0.1:8188
#   - 「自定义模组」页支持 Payload JSON / mod.py 代码两种模式，编辑后自动重渲染
#   - 可切主题 / 显示方向 / 屏幕尺寸（PPI），需要本地 PIL（pip3 install --user pillow）

python3 neon-dash/ndash.py pack your-module -o dist/your.neon-dash
python3 neon-dash/ndash.py sign dist/your.neon-dash --key mykey.secret --signer you
python3 neon-dash/ndash.py verify dist/your.neon-dash
# NAS 本机：设置页 → 模组 → 安装 .neon-dash 包 → 启用
```
