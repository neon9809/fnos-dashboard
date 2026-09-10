#!/usr/bin/env bash
# fpksb 冒烟测试：init -> check -> build -> unpack -> 与官方 fnpack（若存在）对比格式
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FPKSB="node $ROOT/bin/fpksb.js"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 1. init"
$FPKSB init demo --image nginx:alpine --port 8080 --net bridge \
  --display-name "Demo" --dir "$WORK/demo" >/dev/null

echo "== 2. check (expect pass)"
$FPKSB check "$WORK/demo" --json | grep -q '"errors": \[\]' || { echo "check failed"; exit 1; }

echo "== 3. build (internal packer)"
$FPKSB build "$WORK/demo" --packer internal -o "$WORK/demo.fpk" >/dev/null
test -s "$WORK/demo.fpk"

echo "== 4. unpack roundtrip"
$FPKSB unpack "$WORK/demo.fpk" --list --deep | grep -q 'app/ui/config'

echo "== 5. tar readability + checksum line"
tar tzf "$WORK/demo.fpk" | grep -q '^manifest$'
tar xzf "$WORK/demo.fpk" -O manifest 2>/dev/null | grep -qE '^checksum[[:space:]]*=[[:space:]]*[0-9a-f]{32}$'

echo "== 6. official fnpack cross-check (optional)"
FNPACK="${FNPACK_BIN:-}"
if [ -z "$FNPACK" ]; then
  if command -v fnpack >/dev/null 2>&1; then FNPACK=fnpack; fi
fi
if [ -n "$FNPACK" ]; then
  cp -r "$WORK/demo" "$WORK/demo-official"
  (cd "$WORK/demo-official" && "$FNPACK" build >/dev/null)
  # 官方包与我们包的外层成员集合必须一致
  python3 - "$WORK/demo-official/demo.fpk" "$WORK/demo.fpk" <<'PY'
import sys, tarfile
a = sorted(m.name for m in tarfile.open(sys.argv[1]).getmembers())
b = sorted(m.name for m in tarfile.open(sys.argv[2]).getmembers())
assert a == b, f"member mismatch:\n{a}\n{b}"
print("members identical:", len(a))
PY
else
  echo "   (未找到官方 fnpack，跳过；可设置 FNPACK_BIN 启用)"
fi

echo "== all tests passed ✓"
