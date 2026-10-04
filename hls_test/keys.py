"""Deterministic key material and HMAC signing for the test server.
测试服务器的确定性密钥材料与 HMAC 签名。

All values are derived from a fixed root so they are stable across restarts
and can be documented / asserted against.
所有值都由固定根派生，因此重启后保持不变，可以写进文档、也可以被断言校验。
"""

from __future__ import annotations

import hashlib
import hmac

# 派生根：改这个字符串会让所有密钥 / 签名一起变化（不要在生产场景照抄）。
ROOT = b"hls-test-server/v1"


def derive16(purpose: str) -> bytes:
    """派生 16 字节（AES-128 密钥长度）。"""
    return hashlib.sha256(ROOT + b":" + purpose.encode()).digest()[:16]


def derive32(purpose: str) -> bytes:
    """派生 32 字节（HMAC-SHA256 密钥长度）。"""
    return hashlib.sha256(ROOT + b":" + purpose.encode()).digest()


AES128_KEY = derive16("aes128")            # 主 AES-128 密钥（hex 写在 README 里）
WRONG_KEY = derive16("wrong-key")         # 用于构造"解出乱码"场景
# 逐片轮换用的 12 个密钥（EXT-X-KEY 每个分片换一次）。
ROT_KEYS = [derive16(f"rotation-{i}") for i in range(12)]
SIGN_SECRET = derive32("url-signing")     # 签名 URL（?exp&s）用的 HMAC 密钥


def hexs(b: bytes) -> str:
    return b.hex()


def sign(payload: str) -> str:
    """Short HMAC-SHA256 signature (16 hex chars) for URL signing.
    签名 URL 用的短 HMAC-SHA256 签名（16 个 hex 字符）。"""
    return hmac.new(SIGN_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]


def verify(payload: str, sig: str) -> bool:
    """常量时间比对，避免时序侧信道（虽然本工程只是测试床）。"""
    return hmac.compare_digest(sign(payload), sig)
