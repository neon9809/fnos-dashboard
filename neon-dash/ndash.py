#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neon-dash 模组开发者工具（打包 + 签名，纯标准库）。

子命令：
  pack   <模块目录> -o out.neon-dash           打包为 .neon-dash
  sign   <pkg.neon-dash> --key K --signer NAME  追加 signature.json
  keygen -o secret.key                          生成 ed25519 密钥对
  verify <pkg.neon-dash> [--keys keys.json]     自检签名（发行前）

示例（完整流程见 sign/README.md）：
  python3 neon-dash/ndash.py keygen -o mykey.secret
  python3 neon-dash/ndash.py pack tools/mock-module -o dist/mock.neon-dash
  python3 neon-dash/ndash.py sign dist/mock.neon-dash --key mykey.secret --signer neon
  python3 neon-dash/ndash.py verify dist/mock.neon-dash
"""

import argparse
import io
import json
import os
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "app", "bin"))
import neon_crypto  # noqa: E402


def cmd_keygen(args):
    secret, public = neon_crypto.keypair()
    with open(args.o, "w", encoding="utf-8") as f:
        f.write(neon_crypto.b64e(secret))
    os.chmod(args.o, 0o600)
    print("私钥已写入 %s（务必妥善保管，不要提交到仓库）" % args.o)
    print("\n公钥（base64，交给 NAS 管理员加入信任列表）：")
    print("  " + neon_crypto.b64e(public))
    print("\nkey_id: " + neon_crypto.key_id(public))


def read_entries(pkg_path):
    try:
        with open(pkg_path, "rb") as f:
            zf = zipfile.ZipFile(io.BytesIO(f.read()))
    except (OSError, zipfile.BadZipFile):
        sys.exit("错误：无法读取 .neon-dash 包 %s" % pkg_path)
    return [(n, zf.read(n)) for n in zf.namelist() if not n.endswith("/")]


def write_package(entries, path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in entries:
            z.writestr(n, c)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


def cmd_pack(args):
    src = os.path.abspath(args.src)
    if not os.path.isfile(os.path.join(src, "manifest.json")):
        sys.exit("错误：%s 缺少 manifest.json（见 MODULE_SPEC.md）" % src)
    entries = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            with open(full, "rb") as f:
                entries.append((rel, f.read()))
    names = [n for n, _ in entries]
    if "signature.json" in names:
        sys.exit("错误：模块目录内不应包含 signature.json（请用 sign 子命令生成）")
    if "manifest.json" not in names:
        sys.exit("错误：缺少 manifest.json")
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_package(entries, out_path)
    print("打包完成：%s (%.1f KB)" % (out_path, os.path.getsize(out_path) / 1024))
    print("提示：发行前请签名 —— python3 %s sign %s --key <私钥> --signer <名字>"
          % (sys.argv[0], out_path))


def cmd_sign(args):
    secret = load_secret(args.key)
    entries = read_entries(args.package)
    payload = [(n, c) for n, c in entries if n != "signature.json"]
    if len(payload) != len(entries):
        print("警告：包内已有 signature.json，将被覆盖")
    sig = neon_crypto.sign_package(payload, secret, args.signer)
    write_package(payload + [("signature.json",
                              json.dumps(sig, ensure_ascii=False, indent=2))],
                  args.package)
    print("签名完成：%s" % args.package)
    print("signer: %s   key_id: %s" % (args.signer or "-", sig["signer"]["key_id"]))
    print("公钥（base64，请交 NAS 管理员加入信任列表）：")
    print("  " + sig["signer"]["public_key"])


def cmd_verify(args):
    entries = read_entries(args.package)
    sig_entry = dict(entries).get("signature.json")
    if sig_entry is None:
        print("状态：unsigned（未签名）")
        return
    trusted = []
    if args.keys:
        try:
            with open(args.keys, "r", encoding="utf-8") as f:
                trusted = json.load(f)
        except (OSError, ValueError):
            sys.exit("错误：无法读取信任列表 %s" % args.keys)
    try:
        sig_obj = json.loads(sig_entry.decode("utf-8"))
    except ValueError:
        sys.exit("错误：signature.json 解析失败")
    status, info = neon_crypto.verify_package(
        [(n, c) for n, c in entries if n != "signature.json"], sig_obj, trusted)
    print("状态：%s" % status)
    if info.get("reason"):
        print("原因：%s" % info["reason"])
    if info.get("signer_id"):
        print("签名者：%s（%s）" % (info["signer_id"], info.get("key_id", "")))
    if status == "verified":
        print("✓ 签名有效且签名者在信任列表中")
    elif status == "untrusted":
        print("⚠ 签名有效，但签名者不在信任列表（key_id 可提供给管理员核对）")
    elif status == "invalid":
        sys.exit(1)


def load_secret(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return neon_crypto.b64d(f.read().strip())
    except (OSError, ValueError):
        sys.exit("错误：无法读取密钥文件 %s（用 keygen 生成）" % path)


def main():
    ap = argparse.ArgumentParser(
        prog="ndash", description=".neon-dash 模组打包/签名工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="生成 ed25519 密钥对")
    p.add_argument("-o", required=True, help="私钥输出路径")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("pack", help="打包模块目录为 .neon-dash")
    p.add_argument("src", help="模块目录（含 manifest.json）")
    p.add_argument("-o", "--out", required=True, help="输出路径")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("sign", help="签名 .neon-dash")
    p.add_argument("package", help=".neon-dash 路径")
    p.add_argument("--key", required=True, help="私钥文件")
    p.add_argument("--signer", default="", help="开发者署名")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help="自检签名")
    p.add_argument("package", help=".neon-dash 路径")
    p.add_argument("--keys", help="信任列表 JSON（可选）")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
