#!/bin/bash
# fnOS 状态监视 —— 构建与打包脚本
# 用法：./build.sh   （可选 fnpack 存在时自动打包 .fpk）
# 官方模组签名需要私钥：export NDASH_KEY=/path/to/neon-dash.secret
set -e
cd "$(dirname "$0")"

: "${NDASH_KEY:?请先 export NDASH_KEY=<ed25519 签名私钥文件路径>（不写入任何默认路径）}"

echo "[1/4] 生成应用图标..."
python3 tools/make_icons.py

echo "[2/4] 打包官方模组..."
python3 neon-dash/ndash.py pack neon-dash/example/volc-plan -o app/bin/volc-plan.neon-dash
python3 neon-dash/ndash.py sign app/bin/volc-plan.neon-dash --key "$NDASH_KEY" --signer neon
python3 neon-dash/ndash.py pack neon-dash/example/glm-plan -o app/bin/glm-plan.neon-dash
python3 neon-dash/ndash.py sign app/bin/glm-plan.neon-dash --key "$NDASH_KEY" --signer neon

echo "[3/4] 设置脚本执行权限..."
chmod +x build.sh cmd/* app/bin/*.py tools/*.py

if command -v fnpack >/dev/null 2>&1; then
    echo "[4/4] 使用 fnpack 打包..."
    fnpack pack .
    echo "完成：生成 .fpk 安装包。"
else
    echo "[4/4] 未检测到 fnpack，跳过打包。"
    echo "      安装 fnpack 后执行 'fnpack pack' 即可生成 .fpk："
    echo "      https://developer.fnnas.com/docs/cli/fnpack"
fi

echo ""
echo "本地预览（无需安装）：python3 app/bin/dashboard.py --port 8199 --web app/web"
echo "设置页：http://127.0.0.1:8199/settings（仅限本机）"
