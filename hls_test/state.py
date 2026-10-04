"""Mutable server state: sessions, one-time tokens, rate limits, bans, captcha.
可变的服务器状态：session、一次性 token、限流、封禁、验证码。

Everything is in-memory, lock-protected and aggressively pruned. This is a test
server: state intentionally resets on restart.
全部在内存中，加锁保护并会主动清理。注意这是测试服务器：状态在重启后会重置，
这是有意为之（每次验证都从干净状态开始）。

所有公开方法都持有 self._lock，因为 HTTP handler 跑在多线程里。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque

SESSION_TTL = 600          # 10 min / session 有效期 10 分钟
ONETIME_TTL = 300          # 5 min  / 一次性 token 兜底有效期
CAPTCHA_TTL = 600          # 10 min / 验证码解出后的放行窗口
_MAX_ENTRIES = 100_000     # 超过这个规模才触发清理，避免每次请求都全表扫描


class State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}       # token -> expiry 会话 token -> 过期时间
        self._onetime: set[str] = set()              # live (unconsumed) tokens 未消费的一次性 token
        self._captcha_ok: dict[str, float] = {}      # ip -> expiry 已验证 IP -> 过期时间
        self._hits: dict[tuple, deque] = {}          # (scope, ip) -> timestamps 滑动窗口命中时间戳
        self._banned: dict[tuple, float] = {}        # (scope, ip) -> until 封禁截止时间
        self._counters: dict[str, int] = {}           # 通用计数器（诊断用）
        self._ranges: dict[str, float] = {}         # range key -> first-use time 区间请求首次使用时间

    # -- sessions -----------------------------------------------------------

    def new_session(self) -> str:
        """颁发一个新 session token（32 个 hex 字符）。"""
        token = secrets.token_hex(16)
        with self._lock:
            self._prune()
            self._sessions[token] = time.time() + SESSION_TTL
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            return exp is not None and exp > time.time()

    # -- one-time tokens ------------------------------------------------------

    def mint_onetime(self) -> str:
        """签发一个一次性 token（24 个 hex 字符）。"""
        token = secrets.token_hex(12)
        with self._lock:
            self._onetime.add(token)
        return token

    def consume_onetime(self, token: str | None) -> bool:
        """True if the token was live and is now consumed.
        仅当 token 尚未被消费时返回 True，并立即把它消费掉。"""
        if not token:
            return False
        with self._lock:
            if token in self._onetime:
                self._onetime.remove(token)
                return True
            return False

    # -- captcha --------------------------------------------------------------

    def solve_captcha(self, ip: str) -> None:
        """标记该 IP 已通过验证码，放行 CAPTCHA_TTL 秒。"""
        with self._lock:
            self._captcha_ok[ip] = time.time() + CAPTCHA_TTL

    def captcha_ok(self, ip: str) -> bool:
        with self._lock:
            exp = self._captcha_ok.get(ip)
            return exp is not None and exp > time.time()

    # -- rate limiting / bans ---------------------------------------------------

    def hit(self, scope: str, ip: str, window: float) -> int:
        """Record a request; return the number of hits within the window.
        记录一次请求，返回该 window 秒滑动窗口内的命中次数。

        deque 中只保留窗口内的时间戳，所以返回值就是实时速率。
        """
        now = time.time()
        with self._lock:
            self._prune()
            dq = self._hits.setdefault((scope, ip), deque())
            dq.append(now)
            while dq and dq[0] < now - window:
                dq.popleft()
            return len(dq)

    def ban(self, scope: str, ip: str, seconds: float) -> None:
        """在 scope 作用域内封禁 ip 共 seconds 秒。"""
        with self._lock:
            self._banned[(scope, ip)] = time.time() + seconds

    def is_banned(self, scope: str, ip: str) -> float:
        """Return remaining ban seconds, or 0.0 if not banned.
        返回剩余封禁秒数；未封禁（或已过期）返回 0.0。"""
        with self._lock:
            until = self._banned.get((scope, ip))
            if until is None:
                return 0.0
            if until <= time.time():
                del self._banned[(scope, ip)]
                return 0.0
            return until - time.time()

    # -- misc -------------------------------------------------------------------

    # -- range/resume emulation -------------------------------------------------

    def range_first_use(self, key: str, ttl: float = 60.0) -> bool:
        """True the first time `key` is seen within `ttl` seconds.
        在 ttl 秒内首次见到 key 时返回 True。

        Used by the `restarts` range mode: the first Range request is honoured,
        later ones fall back to a full 200 body.
        供 range 模式 "restarts" 使用：第一次 Range 请求被尊重，
        之后再来的请求退回完整 200 响应体（模拟"续传进度丢失"）。
        """
        now = time.time()
        with self._lock:
            # 先淘汰过期的 key，避免字典无限增长。
            self._ranges = {k: t for k, t in getattr(
                self, "_ranges", {}).items() if t > now - ttl}
            if key in self._ranges:
                return False
            self._ranges[key] = now
            return True

    def bump(self, name: str) -> int:
        """计数器自增并返回新值（用于限流计数等）。"""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + 1
            return self._counters[name]

    def _prune(self) -> None:
        """当会话/token 总量超过阈值时清理过期条目（懒清理，摊薄成本）。"""
        if len(self._onetime) + len(self._sessions) < _MAX_ENTRIES:
            return
        now = time.time()
        self._sessions = {t: e for t, e in self._sessions.items() if e > now}
        self._captcha_ok = {t: e for t, e in self._captcha_ok.items() if e > now}
        self._banned = {t: e for t, e in self._banned.items() if e > now}
