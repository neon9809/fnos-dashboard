#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neon-dash 模组包签名支持（纯标准库）。

- 纯 Python ed25519（RFC 8032）verify + sign，安装验证一次性的性能足够
- 规范化摘要：与 zip 内条目顺序/时间戳无关
  payload_digest = SHA256( Σ sorted(name): name + "\\0" + SHA256(content) )
- key_id = "SHA256:" + sha256(public_key_raw).hexdigest()[:16]
"""

import base64
import hashlib
import json
import os

__q = 2 ** 255 - 19
__l = 2 ** 252 + 27742317777372353535851937790883648493
__d = -121665 * pow(121666, __q - 2, __q) % __q
__I = pow(2, (__q - 1) // 4, __q)


def _inv(x):
    return pow(x, __q - 2, __q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(__d * y * y + 1)
    x = pow(xx, (__q + 3) // 8, __q)
    if (x * x - xx) % __q != 0:
        x = (x * __I) % __q
    if x % 2 != 0:
        x = __q - x
    return x


_By = 4 * _inv(5) % __q
_Bx = _xrecover(_By)
_B = (_Bx % __q, _By % __q, 1, (_Bx * _By) % __q)
_IDENT = (0, 1, 1, 0)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % __q
    b = (y1 + x1) * (y2 + x2) % __q
    c = t1 * 2 * __d * t2 % __q
    e = b - a
    f = 2 * z1 * z2 - c
    g = 2 * z1 * z2 + c
    h = b + a
    return (e * f % __q, g * h % __q, f * g % __q, e * h % __q)


def _mult(P, e):
    Q = _IDENT
    while e:
        if e & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        e >>= 1
    return Q


def _is_oncurve(P):
    x, y, z, t = P
    # Ed25519: -x² + y² ≡ 1 + d·x²y²  →  y² - x² - z² - d·t² ≡ 0（齐次坐标）
    return (y * y - x * x - z * z - __d * t * t) % __q == 0 \
        and t == (x * y * _inv(z)) % __q


def _decodepoint(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if x & 1 != (s[31] >> 7) & 1:
        x = __q - x
    P = (x, y, 1, x * y % __q)
    if not _is_oncurve(P):
        raise ValueError("point not on curve")
    return P


def _encodepoint(P):
    x, y, z, _t = P
    zi = _inv(z)
    x = x * zi % __q
    y = y * zi % __q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(b):
    a = bytearray(b)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


def verify(public_raw, signature, message):
    """ed25519 验签：public_raw/signature/message 均为 bytes。"""
    if len(public_raw) != 32 or len(signature) != 64:
        return False
    try:
        A = _decodepoint(public_raw)
        # 拒绝小阶公钥点（单位元 / 2·4·8 阶）：不在素数阶子群的公钥
        # 可构造小阶等价点伪造「有效」签名（cofactor 攻击面）。
        # _mult 结果是射影坐标，须规范化后与单位元比较
        if _encodepoint(_mult(A, 8)) == _encodepoint(_IDENT):
            return False
        rs = signature[:32]
        s = int.from_bytes(signature[32:], "little")
        if s >= __l:
            return False
        h = int.from_bytes(hashlib.sha512(rs + public_raw + message).digest(),
                           "little") % __l
        left = _mult(_B, s)
        right = _add(_decodepoint(rs), _mult(A, h))
        return _encodepoint(left) == _encodepoint(right)
    except Exception:
        return False


def sign(secret_raw, message):
    """ed25519 签名，返回 64B 签名。"""
    h = hashlib.sha512(secret_raw).digest()
    a = _clamp(h[:32])
    public = _encodepoint(_mult(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % __l
    rs = _encodepoint(_mult(_B, r))
    k = int.from_bytes(hashlib.sha512(rs + public + message).digest(),
                       "little") % __l
    s = (r + k * a) % __l
    return rs + s.to_bytes(32, "little")


def keypair():
    """生成 (secret_raw, public_raw)。"""
    secret = os.urandom(32)
    return secret, _encodepoint(_mult(_B, _clamp(hashlib.sha512(secret).digest()[:32])))


def key_id(public_raw):
    return "SHA256:" + hashlib.sha256(public_raw).hexdigest()[:16]


# ---------------- 包摘要与 signature.json ----------------

def payload_digest(entries):
    """entries: [(name:str, content:bytes)]，排除 signature.json 由调用方处理。"""
    h = hashlib.sha256()
    for name, content in sorted(entries, key=lambda e: e[0].encode("utf-8")):
        h.update(name.encode("utf-8") + b"\x00")
        h.update(hashlib.sha256(content).digest())
    return h.hexdigest()


def b64e(b):
    return base64.b64encode(b).decode("ascii")


def b64d(s):
    return base64.b64decode(s)


def sign_package(entries, secret_raw, signer_id=""):
    """entries 不含 signature.json；返回 signature.json 的 dict。"""
    digest = payload_digest(entries)
    public = _encodepoint(_mult(_B, _clamp(hashlib.sha512(secret_raw).digest()[:32])))
    sig = sign(secret_raw, bytes.fromhex(digest))
    return {
        "alg": "ed25519",
        "payload_sha256": digest,
        "signature": b64e(sig),
        "signer": {"id": signer_id, "key_id": key_id(public),
                   "public_key": b64e(public)},
    }


def verify_package(entries, sig_obj, trusted_keys):
    """entries 不含 signature.json；trusted_keys: [ {key_id, public_key(b64), ...} ]。

    返回 (status, info)：status ∈ verified / untrusted / unsigned / invalid。
    """
    if not isinstance(sig_obj, dict):
        return "unsigned", {}
    if sig_obj.get("alg") != "ed25519":
        return "invalid", {"reason": "不支持的签名算法 %r" % sig_obj.get("alg")}
    digest = payload_digest(entries)
    if str(sig_obj.get("payload_sha256", "")).lower() != digest:
        return "invalid", {"reason": "包内容与签名摘要不符（可能被篡改）"}
    signer = sig_obj.get("signer") or {}
    try:
        public = b64d(signer.get("public_key", ""))
        sig = b64d(sig_obj.get("signature", ""))
    except Exception:
        return "invalid", {"reason": "签名字段解码失败"}
    if not verify(public, sig, bytes.fromhex(digest)):
        return "invalid", {"reason": "签名验证失败"}
    kid = key_id(public)
    info = {"key_id": kid, "signer_id": signer.get("id", ""),
            "public_key": signer.get("public_key", "")}
    for tk in trusted_keys or []:
        if tk.get("key_id") == kid:
            info["trusted_name"] = tk.get("name", "")
            return "verified", info
    return "untrusted", info


def load_trusted_keys(etc_dir):
    path = os.path.join(etc_dir, "trusted_keys.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_trusted_keys(etc_dir, keys):
    path = os.path.join(etc_dir, "trusted_keys.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
