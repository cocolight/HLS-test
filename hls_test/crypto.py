"""Pure-Python AES-128-CBC with PKCS7 padding (zero dependencies).
纯 Python 实现的 AES-128-CBC + PKCS7 填充（零依赖）。

Used to encrypt HLS test segments. Correctness is verified at import time
against the FIPS-197 Appendix C.1 known-answer vector; if that check fails
the process aborts loudly instead of serving wrong ciphertext.

用于加密 HLS 测试分片。导入时会用 FIPS-197 附录 C.1 的标准测试向量做自检；
一旦不通过，进程会直接大声报错退出，而不是继续下发错误密文。

性能提示：本实现是逐块纯 Python 循环，约 3.5MB/50s，仅适合测试床，
不要拿去做真实业务加密。
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# GF(2^8) tables with generator 0x03 (standard for AES)
# GF(2^8) 有限域对数/指数表，生成元取 0x03（AES 标准取法）
# ---------------------------------------------------------------------------

_EXP = [0] * 256
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    # 乘 3（x+1）：先左移一位做 x2，再异或原值；0x1B 是 AES 的本原多项式低位。
    _x ^= (_x << 1) ^ (0x1B if _x & 0x80 else 0)
    _x &= 0xFF


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（查对数表相加后取指数）。"""
    if a == 0 or b == 0:
        return 0
    return _EXP[(_LOG[a] + _LOG[b]) % 255]


def _rotl8(v: int, n: int) -> int:
    """8 位循环左移，用于 S-box 的仿射变换。"""
    return ((v << n) | (v >> (8 - n))) & 0xFF


# S-box: multiplicative inverse followed by the AES affine transform.
# S-box：先求乘法逆元，再做 AES 的仿射变换。
SBOX = [0] * 256
for _v in range(256):
    _inv = 0 if _v == 0 else _EXP[(255 - _LOG[_v]) % 255]
    SBOX[_v] = (
        _inv ^ _rotl8(_inv, 1) ^ _rotl8(_inv, 2) ^ _rotl8(_inv, 3) ^ _rotl8(_inv, 4) ^ 0x63
    )

# 轮常量：AES-128 共 10 轮，取 Rcon[1..10]。
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def expand_key(key: bytes) -> list[list[int]]:
    """AES-128 key schedule: 16-byte key -> 44 32-bit words (column-major).
    AES-128 密钥编排：16 字节密钥 -> 44 个 32 位字（按列主序）。

    轮密钥是 11 组 × 4 字（初始轮 + 10 轮），每 4 个连续字为一组轮密钥。
    """
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    w = [list(key[4 * i: 4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                 # RotWord 循环左移一字节
            temp = [SBOX[b] for b in temp]             # SubWord 逐字节过 S-box
            temp[0] ^= RCON[i // 4 - 1]                # 与轮常量异或
        w.append([a ^ b for a, b in zip(w[i - 4], temp)])
    return w


def _encrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    # state[r][c]; input fills column-major: s[r][c] = in[r + 4c]
    # 状态矩阵按列主序装填：s[r][c] = in[r + 4c]。
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rnd: int) -> None:
        """AddRoundKey：把第 rnd 组轮密钥逐字节异或进状态。"""
        for c in range(4):
            word = w[4 * rnd + c]
            for r in range(4):
                s[r][c] ^= word[r]

    add_round_key(0)
    for rnd in range(1, 10):
        # SubBytes 逐字节代换
        s = [[SBOX[v] for v in row] for row in s]
        # ShiftRows (row r shifted left by r) 第 r 行循环左移 r 字节
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        # MixColumns 列混合（GF(2^8) 上的固定矩阵乘法）
        for c in range(4):
            a0, a1, a2, a3 = (s[0][c], s[1][c], s[2][c], s[3][c])
            s[0][c] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
            s[1][c] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
            s[2][c] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
            s[3][c] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
        add_round_key(rnd)
    # final round (no MixColumns) 最后一轮不做列混合
    s = [[SBOX[v] for v in row] for row in s]
    for r in range(1, 4):
        s[r] = s[r][r:] + s[r][:r]
    add_round_key(10)
    return bytes(s[r][c] for c in range(4) for r in range(4))


# ---------------------------------------------------------------------------
# CBC + PKCS7
# ---------------------------------------------------------------------------

# Inverse tables for decryption / 解密用的逆 S-box
INV_SBOX = [0] * 256
for _v, _s in enumerate(SBOX):
    INV_SBOX[_s] = _v


def _decrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    """AES-128 inverse cipher (the straight Inverse Cipher of FIPS-197 5.3).
    AES-128 逆密码（FIPS-197 5.3 的直接逆运算顺序）。"""
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            word = w[4 * rnd + c]
            for r in range(4):
                s[r][c] ^= word[r]

    add_round_key(10)
    for rnd in range(9, 0, -1):
        # InvShiftRows (row r shifted right by r) 第 r 行循环右移 r 字节
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
        # InvSubBytes 逆字节代换
        s = [[INV_SBOX[v] for v in row] for row in s]
        add_round_key(rnd)
        # InvMixColumns 逆列混合
        for c in range(4):
            a0, a1, a2, a3 = (s[0][c], s[1][c], s[2][c], s[3][c])
            s[0][c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            s[1][c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            s[2][c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            s[3][c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    # final round (no InvMixColumns) 最后一轮不做逆列混合
    for r in range(1, 4):
        s[r] = s[r][-r:] + s[r][:-r]
    s = [[INV_SBOX[v] for v in row] for row in s]
    add_round_key(0)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes,
                       unpad: bool = True) -> bytes:
    """Inverse of aes128_cbc_encrypt; verify.py uses it to check segments.
    aes128_cbc_encrypt 的逆运算；verify.py 用它校验分片是否可解密。

    unpad=False 时不做 PKCS7 去填充，用于检查"合并后整体解密"这类错误用法。
    """
    if len(iv) != 16:
        raise ValueError("IV must be 16 bytes")
    if len(data) % 16:
        raise ValueError("ciphertext must be a multiple of the 16-byte block")
    w = expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        # CBC 解密：明文 = D(密文块) XOR 前一个密文块（第一块用 IV）。
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, w), prev))
        prev = block
    return pkcs7_unpad(bytes(out)) if unpad else bytes(out)


def pkcs7_unpad(data: bytes) -> bytes:
    """PKCS7 去填充；填充非法时抛 ValueError（下游"错误密钥"场景依赖此行为）。"""
    if not data or len(data) % 16:
        raise ValueError("bad PKCS7 length")
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad PKCS7 padding")
    return data[:-n]


def pkcs7_pad(data: bytes) -> bytes:
    """PKCS7 填充：补齐到 16 字节整数倍（整倍数时补满一整块）。"""
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 加密（含 PKCS7 填充）。HLS 的 AES-128 分片就用它生成。"""
    if len(iv) != 16:
        raise ValueError("IV must be 16 bytes")
    w = expand_key(key)
    padded = pkcs7_pad(data)
    out = bytearray()
    prev = iv
    for i in range(0, len(padded), 16):
        block = bytes(a ^ b for a, b in zip(padded[i:i + 16], prev))
        prev = _encrypt_block(block, w)
        out += prev
    return bytes(out)


# ---------------------------------------------------------------------------
# Self-test (FIPS-197 Appendix C.1 + NIST SP 800-38A F.2.1 CBC vector)
# 导入时自检（FIPS-197 附录 C.1 + NIST SP 800-38A F.2.1 的 CBC 向量）
# ---------------------------------------------------------------------------

def _selftest() -> None:
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    expect = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    got = _encrypt_block(pt, expand_key(key))
    if got != expect:
        raise RuntimeError(f"AES self-test FAILED (FIPS-197): got {got.hex()}")

    # NIST SP 800-38A F.2.1 (CBC-AES128.Encrypt)
    key2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv2 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt2 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    expect2 = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
    got2 = aes128_cbc_encrypt(key2, iv2, pt2)[:16]
    if got2 != expect2:
        raise RuntimeError(f"AES CBC self-test FAILED (SP800-38A): got {got2.hex()}")

    # inverse cipher must round-trip (both vectors + a multi-block body)
    # 逆密码必须能往返（两个向量 + 一块多分组数据）
    if _decrypt_block(expect, expand_key(key)) != pt:
        raise RuntimeError("AES decrypt self-test FAILED (FIPS-197)")
    body = bytes(range(256)) * 3
    if aes128_cbc_decrypt(key2, iv2, aes128_cbc_encrypt(key2, iv2, body)) != body:
        raise RuntimeError("AES-CBC round-trip self-test FAILED")

    # S-box spot checks (guards against a broken table build)
    # S-box 抽查（防止表构造出错）
    if SBOX[0x00] != 0x63 or SBOX[0x01] != 0x7c or SBOX[0x53] != 0xed:
        raise RuntimeError("AES S-box self-test FAILED")


# 模块被 import 时立即执行自检——失败就中止，绝不静默下发错误密文。
_selftest()

if __name__ == "__main__":
    print("AES self-test OK")
    print("python:", sys.version)
