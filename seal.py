#!/usr/bin/env python3
"""a2a-seal —— 零依赖的密封信封（sealed box）。

为什么要有这个东西
------------------
任务从群里出发，要经过：本机 → 代理 → 公网 → 某个反代/隧道边缘 → 目标 agent。
其中任何一跳只要做了 TLS 终结（Cloudflare 就是这么干的），正文就是明文。
"HTTPS 就够安全"这个说法在边缘终结的场景下不成立。

所以正文自己再套一层：X25519 密封盒。边缘、代理、日志系统能看到的只有一串密文。
服务端也看不到发出去的任务正文（只有收件人的私钥能开）。

算法组合
--------
  临时密钥对   X25519（每封信现生成，用完即弃 → 前向保密）
  共享密钥     ECDH(临时私钥, 收件人公钥)
  派生         HKDF-SHA256(共享密钥, salt=临时公钥||收件人公钥, info="a2a-seal-v1")
  加密         ChaCha20（96-bit nonce，32-bit counter）
  完整性       HMAC-SHA256（encrypt-then-MAC，先加密再认证，避免 padding oracle）

只用标准库，Python 3.8+ 直接跑，不需要 pip install 任何东西。

信封格式（base64url，无填充）
----------------------------
  1B   版本号 0x01
  32B  临时公钥
  12B  nonce
  nB   密文
  32B  HMAC 标签

用法
----
  # 在 agent 那侧：生成自己的密钥对，把 pub 交给管理员注册
  python3 seal.py keygen muse            # 产出 muse.key（含私钥，自己留着）

  # 收到信封后解密
  python3 seal.py open muse.key "<信封>"
  echo "<信封>" | python3 seal.py open muse.key -

  # 加密后交回服务端（用服务端公钥）
  python3 seal.py seal <服务端公钥> "我的产出"

  # 自检（对 RFC 测试向量）
  python3 seal.py selftest
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import time

VERSION = b"\x01"
INFO = b"a2a-seal-v1"
P = 2 ** 255 - 19
BASE_U = b"\x09" + b"\x00" * 31
MASK32 = 0xFFFFFFFF


# ------------------------------------------------------------------ X25519

def _clamp(scalar: bytes) -> int:
    b = bytearray(scalar)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(bytes(b), "little")


def x25519(scalar: bytes, u: bytes) -> bytes:
    """RFC 7748 的 Montgomery ladder。"""
    if len(scalar) != 32 or len(u) != 32:
        raise ValueError("x25519: 参数必须都是 32 字节")
    k = _clamp(scalar)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % P
        aa = (a * a) % P
        b = (x2 - z2) % P
        bb = (b * b) % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = (d * a) % P
        cb = (c * b) % P
        x3 = ((da + cb) ** 2) % P
        z3 = (x1 * ((da - cb) ** 2)) % P
        x2 = (aa * bb) % P
        z2 = (e * (aa + 121665 * e)) % P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return ((x2 * pow(z2, P - 2, P)) % P).to_bytes(32, "little")


def keygen() -> tuple[bytes, bytes]:
    """返回 (私钥, 公钥)，都是 32 字节。"""
    priv = bytearray(secrets.token_bytes(32))
    priv[0] &= 248
    priv[31] &= 127
    priv[31] |= 64
    priv = bytes(priv)
    return priv, x25519(priv, BASE_U)


def pub_of(priv: bytes) -> bytes:
    return x25519(priv, BASE_U)


# ------------------------------------------------------------------ HKDF

def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    i = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


# ------------------------------------------------------------------ ChaCha20

def _rotl(v: int, n: int) -> int:
    return ((v << n) & MASK32) | (v >> (32 - n))


def _quarter(x: list, a: int, b: int, c: int, d: int) -> None:
    x[a] = (x[a] + x[b]) & MASK32
    x[d] = _rotl(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & MASK32
    x[b] = _rotl(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & MASK32
    x[d] = _rotl(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & MASK32
    x[b] = _rotl(x[b] ^ x[c], 7)


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    state = (
        list(struct.unpack("<4I", b"expand 32-byte k"))
        + list(struct.unpack("<8I", key))
        + [counter & MASK32]
        + list(struct.unpack("<3I", nonce))
    )
    w = state[:]
    for _ in range(10):
        _quarter(w, 0, 4, 8, 12)
        _quarter(w, 1, 5, 9, 13)
        _quarter(w, 2, 6, 10, 14)
        _quarter(w, 3, 7, 11, 15)
        _quarter(w, 0, 5, 10, 15)
        _quarter(w, 1, 6, 11, 12)
        _quarter(w, 2, 7, 8, 13)
        _quarter(w, 3, 4, 9, 14)
    return struct.pack("<16I", *[(w[i] + state[i]) & MASK32 for i in range(16)])


def chacha20(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """加密即解密。counter 从 0 起。"""
    if len(key) != 32:
        raise ValueError("chacha20: key 必须 32 字节")
    if len(nonce) != 12:
        raise ValueError("chacha20: nonce 必须 12 字节")
    out = bytearray()
    for idx, off in enumerate(range(0, len(data), 64)):
        ks = _chacha20_block(key, idx, nonce)
        chunk = data[off:off + 64]
        out.extend(a ^ b for a, b in zip(chunk, ks))
    return bytes(out)


# ------------------------------------------------------------------ 密封盒

def seal(recipient_pub: bytes, plaintext: bytes) -> bytes:
    if len(recipient_pub) != 32:
        raise ValueError("seal: 收件人公钥必须 32 字节")
    eph_priv, eph_pub = keygen()
    shared = x25519(eph_priv, recipient_pub)
    okm = hkdf_sha256(shared, eph_pub + recipient_pub, INFO, 64)
    ek, mk = okm[:32], okm[32:]
    nonce = secrets.token_bytes(12)
    ct = chacha20(ek, nonce, plaintext)
    tag = hmac.new(mk, INFO + eph_pub + nonce + ct, hashlib.sha256).digest()
    return VERSION + eph_pub + nonce + ct + tag


def unseal(priv: bytes, blob: bytes) -> bytes:
    if len(blob) < 1 + 32 + 12 + 32:
        raise ValueError("unseal: 信封太短，不是合法信封")
    if blob[:1] != VERSION:
        raise ValueError("unseal: 信封版本不认识")
    eph_pub = blob[1:33]
    nonce = blob[33:45]
    ct = blob[45:-32]
    tag = blob[-32:]
    my_pub = pub_of(priv)
    shared = x25519(priv, eph_pub)
    okm = hkdf_sha256(shared, eph_pub + my_pub, INFO, 64)
    ek, mk = okm[:32], okm[32:]
    want = hmac.new(mk, INFO + eph_pub + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        raise ValueError("unseal: 校验失败——信封被改过，或这把私钥不是收件人的")
    return chacha20(ek, nonce, ct)


# ------------------------------------------------------------------ 文本层

def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    s = "".join(text.split())
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def seal_text(recipient_pub_b64: str, text: str) -> str:
    return b64e(seal(b64d(recipient_pub_b64), text.encode("utf-8")))


def unseal_text(priv: bytes, blob_b64: str) -> str:
    return unseal(priv, b64d(blob_b64)).decode("utf-8")


# ------------------------------------------------------------------ 密钥文件

def save_keyfile(path: str, agent: str, priv: bytes, pub: bytes) -> None:
    payload = {
        "agent": agent,
        "priv": b64e(priv),
        "pub": b64e(pub),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "alg": "x25519-hkdf-sha256-chacha20-hmac-sha256",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_priv(path: str) -> bytes:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        return b64d(json.loads(raw)["priv"])
    return b64d(raw)


# ------------------------------------------------------------------ 自检

def selftest() -> int:
    fails = 0

    def check(name: str, got: bytes, want: bytes) -> None:
        nonlocal fails
        if got == want:
            print(f"  ok   {name}")
        else:
            fails += 1
            print(f"  FAIL {name}\n       got  {got.hex()}\n       want {want.hex()}")

    print("X25519 (RFC 7748)")
    check(
        "scalar mult #1",
        x25519(
            bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
            bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c"),
        ),
        bytes.fromhex("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"),
    )
    check(
        "base point",
        x25519(
            bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"),
            BASE_U,
        ),
        bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"),
    )
    check(
        "ecdh shared",
        x25519(
            bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"),
            bytes.fromhex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"),
        ),
        bytes.fromhex("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"),
    )

    print("ChaCha20 (RFC 8439 §2.3.2, 全零向量)")
    check(
        "zero block",
        _chacha20_block(b"\x00" * 32, 0, b"\x00" * 12),
        bytes.fromhex(
            "76b8e0ada0f13d90405d6ae55386bd28bdd219b8a08ded1aa836efcc8b770dc7"
            "da41597c5157488d7724e03fb8d84a376a43b8f41518a11cc387b669b2ee6586"
        ),
    )

    print("HKDF-SHA256 (RFC 5869 Test Case 1)")
    check(
        "okm",
        hkdf_sha256(b"\x0b" * 22, bytes.fromhex("000102030405060708090a0b0c"), bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42),
        bytes.fromhex(
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"
        ),
    )

    print("密封盒")
    a_priv, a_pub = keygen()
    b_priv, b_pub = keygen()
    msg = "群里的任务：把这份周报整理成三段，@muse".encode()
    env = seal(b_pub, msg)
    check("往返", unseal(b_priv, env), msg)
    try:
        unseal(a_priv, env)
        fails += 1
        print("  FAIL 错误私钥居然解开了")
    except ValueError:
        print("  ok   错误私钥被拒绝")
    tampered = bytearray(env)
    tampered[60] ^= 0x01
    try:
        unseal(b_priv, bytes(tampered))
        fails += 1
        print("  FAIL 篡改信封居然解开了")
    except ValueError:
        print("  ok   篡改被检测到")
    if len(env) != 1 + 32 + 12 + len(msg) + 32:
        fails += 1
        print("  FAIL 信封长度不符")
    else:
        print("  ok   信封长度")

    print()
    if fails:
        print(f"{fails} 项失败")
        return 1
    print("全部通过")
    return 0


# ------------------------------------------------------------------ CLI

def _read_arg_or_stdin(value: str | None) -> str:
    if value and value != "-":
        return value
    return sys.stdin.read()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]

    if cmd == "selftest":
        return selftest()

    if cmd == "keygen":
        if len(argv) < 3:
            print("用法: seal.py keygen <名字> [--out 文件]", file=sys.stderr)
            return 2
        name = argv[2]
        out = f"{name}.key"
        if "--out" in argv:
            out = argv[argv.index("--out") + 1]
        priv, pub = keygen()
        save_keyfile(out, name, priv, pub)
        print(f"私钥已写入 {out}（权限 600，不要外传）")
        print(f"公钥（交给管理员注册）：{b64e(pub)}")
        return 0

    if cmd == "pub":
        if len(argv) < 3:
            print("用法: seal.py pub <密钥文件>", file=sys.stderr)
            return 2
        print(b64e(pub_of(load_priv(argv[2]))))
        return 0

    if cmd == "seal":
        if len(argv) < 3:
            print("用法: seal.py seal <收件人公钥> [明文|-]", file=sys.stderr)
            return 2
        pub_b64 = argv[2]
        if os.path.exists(pub_b64):
            with open(pub_b64, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            pub_b64 = json.loads(raw)["pub"] if raw.startswith("{") else raw
        print(seal_text(pub_b64, _read_arg_or_stdin(argv[3] if len(argv) > 3 else None)))
        return 0

    if cmd == "open":
        if len(argv) < 3:
            print("用法: seal.py open <密钥文件> [信封|-]", file=sys.stderr)
            return 2
        priv = load_priv(argv[2])
        blob = _read_arg_or_stdin(argv[3] if len(argv) > 3 else None)
        try:
            sys.stdout.write(unseal_text(priv, blob))
            sys.stdout.write("\n")
        except ValueError as exc:
            print(f"解密失败: {exc}", file=sys.stderr)
            return 1
        return 0

    print(f"不认识的命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
