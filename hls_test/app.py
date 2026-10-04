"""HTTP routing and scenario handlers for the HLS test server.
HLS 测试服务器的 HTTP 路由与场景处理器。

分层：本模块是"薄路由 + 场景 handler"层，本身不生成媒体或播放列表文本：
- 播放列表文本由 :mod:`hls_test.playlists` 生成（纯函数）
- 媒体字节由 :mod:`hls_test.media` 提供
- 可变的会话 / 限流 / 封禁状态在 :mod:`hls_test.state`
- 页面文案与中英映射在 :mod:`hls_test.i18n`

每个场景通常对应 2–3 个 handler：播放列表、可选密钥、分片。
新增场景的完整流程见 docs/DEVELOPING.zh-CN.md（中文）或 docs/DEVELOPING.md（英文）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import crypto, i18n, keys, media, playlists
from .state import State

# ---------------------------------------------------------------------------
# Fixed credentials (documented on the index page)
# 固定凭据（同时展示在索引页上，方便手工验证）
# ---------------------------------------------------------------------------

API_KEY = "hls-test-api-key"
REFERER = "https://portal.example.com/"
UA_PREFIX = "HLSClient/1.0"
COOKIE_VALUE = "access=granted"
CAPTCHA_CODE = "1234"
IV = bytes(range(16))  # 0x000102...0e0f，AES 场景统一使用的 IV

# 常用 MIME 类型
M3U8 = "application/vnd.apple.mpegurl"
TS = "video/mp2t"
MP4 = "video/iso.segment"
BIN = "application/octet-stream"

MEDIA: media.MediaProvider = media.MediaProvider()

QUIET = False   # 置 True 可关闭逐请求的访问日志（verify.py 会打开）


def init_media(cache_dir: str | None = None,
               ffmpeg_path: str | None = None,
               sample_path: str | None = None) -> None:
    """(Re)create the media provider; call before serving if you want the
    optional ffmpeg 'real content' mode.
    重建媒体提供器；若想启用可选的 ffmpeg"真实内容"模式，请在开始服务前调用。

    ``ffmpeg_path`` 可指定 ffmpeg 可执行文件或其所在目录；为 None 时按
    media._resolve_ffmpeg 的优先级（$HLS_FFMPEG > ./tools/ffmpeg > PATH）自动查找。

    ``sample_path`` 可指定样例视频文件或其所在目录；为 None 时按
    media._resolve_sample 的优先级（$HLS_SAMPLE > ./tools/sample/）自动查找。
    找到样例后，真实内容将由它转码而来（否则用内置测试图）。
    """
    global MEDIA
    MEDIA = media.MediaProvider(cache_dir=cache_dir, ffmpeg_path=ffmpeg_path,
                                sample_path=sample_path)
STATE = State()
# The sliding-window stream pretends it has been running for an hour, so the
# window is already full when a client tunes in (that is what real live looks
# like). The event stream starts now, so it can be watched growing.
# 滑窗直播流假装已经跑了一小时，因此客户端一接入窗口就是满的（这才是真实直播的
# 样子）。Event 流从现在开始，方便观察它逐步长高。
LIVE_START = time.time() - 3600
EVENT_START = time.time()

# 加密分片 / 大文件缓存（都加锁；避免每次请求都重新算纯 Python AES）。
_ENC_CACHE: dict[tuple, bytes] = {}
_ENC_LOCK = threading.Lock()
_BIG_LOCK = threading.Lock()
_BIG_CACHE: dict[str, bytes] = {}


def enc_segment(group: str, index: int, key: bytes, iv: bytes,
                streams: str = "av", pts_start: int | None = None) -> bytes:
    """取一个分片并加密，结果按 (group,index,streams,pts,key,iv) 缓存。
    纯 Python AES 较慢（见 crypto 模块注释），缓存是必要的。"""
    plain = MEDIA.segment(group, index, streams=streams, pts_start=pts_start)
    ck = ("enc", group, index, streams, pts_start, key, iv)
    with _ENC_LOCK:
        got = _ENC_CACHE.get(ck)
    if got is None:
        got = crypto.aes128_cbc_encrypt(key, iv, plain)
        with _ENC_LOCK:
            _ENC_CACHE[ck] = got
    return got


def big_file(name: str, builder) -> bytes:
    """按名字缓存"大文件"（byterange 场景共用的容器文件）。"""
    with _BIG_LOCK:
        got = _BIG_CACHE.get(name)
    if got is None:
        got = builder()
        with _BIG_LOCK:
            _BIG_CACHE[name] = got
    return got


# ---------------------------------------------------------------------------
# Scenario catalog (drives the index page, /scenarios.json and the README)
# 场景目录（驱动索引页、/scenarios.json 与 README 的场景表）
# ---------------------------------------------------------------------------
#
# 元组字段顺序：id, category, user's original item no, title, path, expectation
# （原始需求编号用于和用户最初列出的 17 项需求对照）
#
# 注意：这里的 title/expect 是**英文原文**，同时也是 ZH_SCENARIOS 缺失时的回退值。
# 对应的中文文案统一维护在 i18n.ZH_SCENARIOS（键 = 本行的 id），不要在 CATALOG 里写中文。
#
# 每个场景同时在 README / README.zh-CN.md 的场景表里有一行；新增场景时三处
# 都要补（见 docs/DEVELOPING.zh-CN.md「新增一个场景（7 步流程）」）。

CATALOG = [
    # id, category, user's original item no, title, path, expectation
    # --- 加密 Encryption ---------------------------------------------------
    ("aes128", "Encryption", "1", "AES-128, explicit IV",
     "/hls/aes128/playlist.m3u8",
     "Decryptable; first plaintext byte must be 0x47 (TS sync)."),
    ("aes128-msn", "Encryption", "1", "AES-128, IV from media sequence number",
     "/hls/aes128/msn-iv/playlist.m3u8",
     "No IV attribute -> IV must be the segment's media sequence number."),
    ("aes128-master", "Encryption", "1", "AES-128 multivariant + SESSION-KEY",
     "/hls/aes128/master.m3u8",
     "EXT-X-SESSION-KEY in master; both variants AES-128."),
    ("key-expiry", "Encryption", "2", "Key URL expires (TTL, signed)",
     "/hls/key-expiry/playlist.m3u8?ttl=30",
     "Key endpoint 403s after ?ttl seconds; retry/re-fetch behaviour. "
     "TTL is configurable (min 5)."),
    ("sample-aes", "Encryption", "3", "SAMPLE-AES (identity keyformat)",
     "/hls/sample-aes/playlist.m3u8",
     "Downloader should refuse: unsupported encryption method."),
    ("fairplay", "Encryption", "3", "SAMPLE-AES + FairPlay keyformat (skd://)",
     "/hls/fairplay/playlist.m3u8",
     "DRM; should be rejected as undecryptable, not treated as parse error."),
    ("widevine", "Encryption", "3", "Widevine KEYFORMAT + data: pssh URI",
     "/hls/widevine/playlist.m3u8",
     "DRM; the data: URI is a pssh box, not a key."),
    ("key-header", "Encryption", "4", "Key requires a custom request header",
     "/hls/key-header/playlist.m3u8",
     f"Key endpoint needs header 'X-Api-Key: {API_KEY}' (403 otherwise)."),
    ("key-rotation", "Encryption", "15", "AES key rotates per segment",
     "/hls/key-rotation/playlist.m3u8",
     "One EXT-X-KEY per segment; sticky-key handling is wrong here."),
    ("aes-fmp4", "Encryption", "13", "AES-128 + fMP4 (clear init, encrypted m4s)",
     "/hls/aes-fmp4/playlist.m3u8",
     "EXT-X-KEY precedes EXT-X-MAP; the init segment is CLEAR, only the .m4s "
     "fragments are AES-128 encrypted."),
    ("aes-byterange", "Encryption", "15", "AES-128 + EXT-X-BYTERANGE",
     "/hls/aes-byterange/playlist.m3u8",
     "Ranges index the CIPHERTEXT (each range independently PKCS7-padded); "
     "concatenating then decrypting as one stream fails."),
    ("aes-disc", "Encryption", "14", "Key changes at EXT-X-DISCONTINUITY",
     "/hls/aes-disc/playlist.m3u8",
     "Clips A/B use different keys; decrypting across the boundary with one "
     "key produces garbage."),

    # --- 播放列表结构 Playlist structure ---------------------------------
    ("paths", "Playlist structure", "5", "Mixed URI forms + redirect",
     "/hls/paths/master.m3u8",
     "Relative / dot-segments / percent-encoded / query / absolute / "
     "protocol-relative / 302 redirect, in variants AND segment URIs."),
    ("byterange", "Playlist structure", "5", "TS segments via EXT-X-BYTERANGE",
     "/hls/byterange/playlist.m3u8",
     "All segments are byte ranges of one big.ts file."),
    # --- 分发 Delivery ----------------------------------------------------
    ("vod", "Delivery", "6", "Plain VOD (ENDLIST)",
     "/hls/vod/playlist.m3u8",
     "Baseline happy path."),
    ("live", "Delivery", "6", "Live sliding window (6 x 6s)",
     "/hls/live/playlist.m3u8",
     "No ENDLIST; MEDIA-SEQUENCE advances; PDT included."),
    ("live-event", "Delivery", "6", "Event playlist -> becomes VOD",
     "/hls/live/event.m3u8",
     "Window grows to 12 segments then appends ENDLIST."),

    # --- 访问控制 Access control -----------------------------------------
    ("guard", "Access control", "7", "Referer / UA / Cookie checks",
     "/hls/guard/playlist.m3u8?auth=all",
     f"?auth=referer|ua|cookie|all. Values: Referer '{REFERER}', "
     f"User-Agent prefix '{UA_PREFIX}', Cookie '{COOKIE_VALUE}'."),
    ("sign", "Access control", "8", "Signed segment URLs with expiry",
     "/hls/sign/playlist.m3u8?ttl=60",
     "HMAC-signed ?exp&s params; 403 after TTL (default 60s, min 5)."),
    ("session", "Access control", "10", "Session required (cookie or param)",
     "/hls/session/playlist.m3u8",
     "GET /hls/session/start first; segments need Cookie HLSSESS=... "
     "or ?session=... (401 otherwise)."),
    ("ratelimit", "Access control", "9", "Rate limited (429 + Retry-After)",
     "/hls/ratelimit/playlist.m3u8?n=5",
     "More than ?n segment fetches per 30s -> 429."),
    ("throttle", "Access control", "9", "Bandwidth throttled delivery",
     "/hls/throttle/playlist.m3u8?kbps=64",
     "Segments delivered at ?kbps (default 64)."),
    ("ban", "Access control", "9", "IP ban after burst",
     "/hls/ban/playlist.m3u8?n=8",
     "Burst over ?n in 60s -> 403 for 60s (scope: /hls/ban only)."),
    ("captcha", "Access control", "9", "Captcha gate before segments",
     "/hls/captcha/playlist.m3u8",
     f"First segment request returns 403 HTML; solve with "
     f"/hls/captcha/solve?code={CAPTCHA_CODE}."),

    # --- 源端缺陷 Source defects -----------------------------------------
    ("broken", "Source defects", "11", "Missing / 500 / garbage / truncated",
     "/hls/broken/playlist.m3u8",
     "seg2=404, seg4=500, seg6=garbage bytes, seg8=truncated."),
    ("broken-enc", "Source defects", "11", "AES stream with a wrong-key segment",
     "/hls/broken-enc/playlist.m3u8",
     "seg6 encrypted with a different key -> decrypts to garbage "
     "(PKCS7 unpad errors downstream)."),
    ("gap", "Source defects", "11", "EXT-X-GAP placeholders",
     "/hls/gap/playlist.m3u8",
     "Segments 3-4 marked GAP; should be skipped, not fetched."),
    ("ads", "Source defects", "11", "Ad break signalled 3 ways",
     "/hls/ads/playlist.m3u8",
     "One ad break marked simultaneously by EXT-X-DATERANGE "
     "(SCTE35-OUT/SCTE35-IN, hex-encoded binary), EXT-X-CUE-OUT/CUE-IN and "
     "EXT-X-GAP on the ad segments."),

    ("discontinuity", "Source defects", "14", "PTS jump + EXT-X-DISCONTINUITY",
     "/hls/discontinuity/playlist.m3u8",
     "After 5 segments the PTS restarts at its initial value."),

    ("onetime", "Source defects", "15", "One-time segment URLs (hard resume)",
     "/hls/onetime/playlist.m3u8",
     "Each playlist fetch mints fresh tokens; every segment URL works "
     "exactly once, then 403. Re-fetching the playlist is the only way "
     "to recover."),
    ("range-ok", "Resume", "15", "Range requests: RFC-correct 206/416",
     "/hls/range/ok/playlist.m3u8",
     "Accept-Ranges: bytes, correct Content-Range, 416 with "
     "Content-Range: bytes */len for unsatisfiable ranges."),
    ("range-none", "Resume", "15", "Range unsupported (Accept-Ranges: none)",
     "/hls/range/none/playlist.m3u8",
     "Server states no range support and always answers 200 with the "
     "full body; resume must restart from byte 0."),
    ("range-ignored", "Resume", "15", "Advertises ranges but ignores Range",
     "/hls/range/ignored/playlist.m3u8",
     "Accept-Ranges: bytes yet every request returns 200 + full body "
     "and no Content-Range."),
    ("range-badtotal", "Resume", "15", "206 with a wrong total length",
     "/hls/range/badtotal/playlist.m3u8",
     "Content-Range total claims half the real size; trusting it "
     "corrupts the assembled file."),
    ("range-multipart", "Resume", "15", "Broken multi-range response",
     "/hls/range/multipart-broken/playlist.m3u8",
     "A bytes=a-b,c-d request returns only the first range as a plain "
     "206, without multipart/byteranges."),
    ("range-restarts", "Resume", "15", "Resume works once, then resets",
     "/hls/range/restarts/playlist.m3u8",
     "The first Range request per URL is honoured; later ones get a "
     "200 full body (progress from the previous attempt is lost)."),
    ("resume-triple", "Resume", "15", "Resume impossible: 3 mechanisms at once",
     "/hls/resume-triple/playlist.m3u8",
     "One-time segment URLs + an expiring key + one-shot Range. A correct "
     "downloader must detect that resuming is impossible and restart, not "
     "splice a corrupt file."),
    ("av-separate", "Container", "12", "Audio/video in separate renditions",
     "/hls/av-separate/master.m3u8",
     "EXT-X-MEDIA audio group + video-only variants + audio-only variant."),
    ("fmp4", "Container", "13", "fMP4 segments (EXT-X-MAP + .m4s)",
     "/hls/fmp4/playlist.m3u8",
     "Init segment must be fetched first (and remuxed with the segments)."),
    ("fmp4-byterange", "Container", "13", "fMP4 MAP + segments via byterange",
     "/hls/fmp4/byterange/playlist.m3u8",
     "Init and media segments are ranges of one combined.m4s file."),

    ("nested", "Playlist structure", "5", "Multi-level nested master (3 tiers)",
     "/hls/nested/master.m3u8",
     "master -> sub-master -> media. Sub-masters use relative, dot-segment "
     "and parent (../) URIs; a client that keeps the top-level base URL 404s."),
    ("variable", "Playlist structure", "17", "Variable EXTINF + TARGETDURATION",
     "/hls/variable/consistent/playlist.m3u8",
     "Segment durations span 0.8s..12.0s. /mismatch/ declares "
     "TARGETDURATION:6, below the real maximum of 12.0 (a common origin bug)."),
    ("fmp4-av", "Container", "13", "fMP4 with separate video + audio tracks",
     "/hls/fmp4/av/playlist.m3u8",
     "Init has two traks (avc1 video + mp4a audio); each .m4s moof holds two "
     "trafs (track_ID 1 and 2) with independent data offsets."),
    ("renditions", "Container", "12", "Multi-audio + subtitles + closed captions",
     "/hls/renditions/master.m3u8",
     "Two AUDIO groups (aud/comm), a SUBTITLES group (en/zh, with URIs) and a "
     "CLOSED-CAPTIONS group (no URI, INSTREAM-ID=CC1). Subtitle segments are "
     "WebVTT with X-TIMESTAMP-MAP."),
    ("define", "Playlist structure", "17+", "EXT-X-DEFINE variable URIs",
     "/hls/define/playlist.m3u8",
     "Segment URIs use {$segbase} substitution (v8 feature)."),
    ("malformed", "Playlist structure", "17", "Malformed playlists",
     "/hls/malformed/crlf/playlist.m3u8",
     "Variants: crlf bom no-version unknown-tags spacing lowercase "
     "no-extm3u bad-attrs all. Segments are fine; parsing is the test."),
    ("series", "Playlist structure", "17+", "Series / episodes (multi-episode VOD)",
     "/hls/series/master.m3u8",
     "3 episodes with unequal segment counts, one shared AES-128 key, and "
     "custom EXT-X-SERIES-PREV / EXT-X-SERIES-NEXT tags a client must ignore."),
]

MALFORMED_KINDS = ["crlf", "bom", "no-version", "unknown-tags", "spacing",
                   "lowercase", "no-extm3u", "bad-attrs", "all"]

CREDENTIALS = {
    "X-Api-Key (key-header)": API_KEY,
    "Referer (guard)": REFERER,
    "User-Agent prefix (guard)": UA_PREFIX,
    "Cookie (guard)": COOKIE_VALUE,
    "captcha code": CAPTCHA_CODE,
    "AES-128 key (hex)": keys.AES128_KEY.hex(),
    "AES-128 IV (hex)": IV.hex(),
}


# ---------------------------------------------------------------------------
# Micro framework / 微型框架
# ---------------------------------------------------------------------------

class Fail(Exception):
    """handler 里用 raise Fail(status, body) 直接结束请求（最短路径返回错误）。"""

    def __init__(self, status: int, body=b"", ctype="text/plain; charset=utf-8",
                 headers=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.ctype = ctype
        self.headers = headers or []


class Resp:
    """handler 的返回值：状态码 + 响应体 + Content-Type + 可选的额外头。"""

    def __init__(self, status=200, body=b"", ctype="text/plain; charset=utf-8",
                 headers=None, kbps: int | None = None, rmode: str | None = None):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.ctype = ctype
        self.headers = headers or []
        self.kbps = kbps
        # Range-request behaviour mode; see _range_response()
        # 区间请求行为模式；见 _range_response()
        self.rmode = rmode


class Req:
    """请求上下文，包装了 BaseHTTPRequestHandler 并补上便捷访问器。"""

    def __init__(self, handler, path: str, query: dict):
        self.h = handler
        self.path = path
        self.query = query
        self.ip = handler.client_address[0]

    def header(self, name: str, default: str = "") -> str:
        return self.h.headers.get(name, default)

    def q(self, name: str, default=None):
        """取查询参数（同名取最后一个值）。"""
        return self.query.get(name, default)

    def qint(self, name: str, default: int) -> int:
        """取整型查询参数；解析失败回落到默认值（不抛错）。"""
        try:
            return int(self.query.get(name, default))
        except (TypeError, ValueError):
            return default

    @property
    def base(self) -> str:
        """当前请求的源站（scheme://host），用于生成绝对 URI。"""
        host = self.header("Host", "127.0.0.1")
        return f"http://{host}"


def m3u8(text: str) -> Resp:
    """返回 200 + m3u8 文本。"""
    return Resp(200, text, M3U8)


def ts(data: bytes, rmode: str | None = None) -> Resp:
    """返回 200 + TS 分片（可用 rmode 指定区间请求行为）。"""
    return Resp(200, data, TS, rmode=rmode)


def key_resp(data: bytes) -> Resp:
    """返回 200 + 二进制密钥。"""
    return Resp(200, data, BIN)


def json_resp(obj, status=200, headers=None) -> Resp:
    """返回 JSON（2 空格缩进，末尾换行，便于人读）。"""
    return Resp(status, json.dumps(obj, indent=2) + "\n",
                "application/json", headers=headers)


# ---------------------------------------------------------------------------
# Handlers / 场景处理器
# ---------------------------------------------------------------------------

def _sample_panel_html(lang: str) -> str:
    """构造索引页顶部的「样例视频」面板（与 ffmpeg 横幅同款样式）。

    找到样例 -> 绿色面板 + 样例路径 + 当前缓存来源 + 「处理/重建」按钮；
    没找到 -> 琥珀色面板 + 放置位置提示。整行独占（.panels.sample）。
    """
    if MEDIA.sample:
        src = (i18n.ui("sample_active", lang) if MEDIA.using_sample
               else i18n.ui("sample_inactive", lang))
        body = (
            f'<b>{i18n.ui("sample_h", lang)}:</b> '
            f'{i18n.ui("sample_found", lang)}<br>'
            f'<span class="ffp">{i18n.ui("sample_path", lang)}: '
            f'<code>{MEDIA.sample}</code></span> '
            f'<span class="ffn">({src})</span>'
            f'<div><button class="clr" id="sample-rebuild" '
            f'data-busy="{i18n.ui("sample_busy", lang)}" '
            f'data-done="{i18n.ui("sample_done", lang)}" '
            f'data-fail="{i18n.ui("sample_fail", lang)}">'
            f'{i18n.ui("sample_btn", lang)}</button>'
            f'<span class="ffn" id="sample-msg"></span></div>')
        return f'<div class="ff ok" style="flex:1 1 100%">{body}</div>'
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hint = i18n.ui("sample_hint", lang).replace(
        "tools/sample/source.mp4",
        os.path.join(root, "tools", "sample", "source.mp4"))
    body = (
        f'<b>{i18n.ui("sample_h", lang)}:</b> '
        f'{i18n.ui("sample_none", lang)}<br>'
        f'<span class="ffn">{hint}</span>')
    return f'<div class="ff no" style="flex:1 1 100%">{body}</div>'


def h_index(req: Req) -> Resp:
    lang = i18n.resolve_lang(req)
    mode = i18n.ui("mode_real" if MEDIA.is_real else "mode_synthetic", lang)
    rows = []
    last_cat = None
    for sid, cat, n, title, path, expect in CATALOG:
        cat_l, title_l, expect_l = i18n.scenario(sid, cat, title, expect, lang)
        if cat != last_cat:
            rows.append(f'<tr class="cat"><td colspan="4">{cat_l}</td></tr>')
            last_cat = cat
        # 每行：编号 / 标题 / URL / 预期行为，外加一个「查看响应」按钮；
        # 详情行（class=detail，默认隐藏）用于承载 /preview/{id} 的内容。
        rows.append(
            f'<tr><td>#{n}</td><td>{title_l}</td>'
            f'<td><a href="{path}" title="{i18n.ui("url_tip", lang)}">'
            f'{req.base}{path}</a></td>'
            f'<td>{expect_l}</td>'
            f'<td><button class="pv" data-id="{sid}" '
            f'data-show="{i18n.ui("btn_show", lang)}" '
            f'data-hide="{i18n.ui("btn_hide", lang)}">'
            f'{i18n.ui("btn_show", lang)}</button></td></tr>')
        rows.append(
            f'<tr class="detail" id="d-{sid}" hidden>'
            f'<td colspan="5"><pre class="pvout"></pre></td></tr>')
    cred_rows = "".join(
        f"<tr><td>{k}</td><td><code>{v}</code></td></tr>"
        for k, v in CREDENTIALS.items())

    # Language switcher: same page, ?lang=… -- keeps the URL bookmarkable and
    # the choice independent of Accept-Language (which is the default).
    def lang_link(code: str) -> str:
        cls = ' class="on"' if code == lang else ""
        return (f'<a href="?lang={code}"{cls}>'
                f'{i18n.LANG_NAMES[code]}</a>')

    switcher = " &middot; ".join(lang_link(c) for c in i18n.LANGS)

    # ffmpeg status banner: 顶部显式提示是否找到 ffmpeg（决定能否生成真实视频）。
    # 找到 -> 显示解析到的路径；没找到 -> 列出查找位置与启用方式。
    if MEDIA.ffmpeg:
        ff_banner = (
            f'<div class="ff ok" id="ff-banner"><b>{i18n.ui("ff_h", lang)}:</b> '
            f'{i18n.ui("ff_ok", lang)}<br>'
            f'<span class="ffp">{i18n.ui("ff_ok_path", lang)}: '
            f'<code>{MEDIA.ffmpeg}</code></span><br>'
            f'<span class="ffn">{i18n.ui("ff_ok_note", lang)}</span></div>')
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        looked = " &middot; ".join(
            f"<code>{p}</code>" for p in (
                "HLS_FFMPEG", os.path.join(root, "tools", "ffmpeg"),
                os.path.join(root, "tools", "ffmpeg", "bin"), "PATH"))
        ff_banner = (
            f'<div class="ff no" id="ff-banner"><b>{i18n.ui("ff_h", lang)}:</b> '
            f'{i18n.ui("ff_no", lang)}<br>'
            f'<span class="ffn">{i18n.ui("ff_no_note", lang)}</span>'
            f'<ul class="ffl">'
            f'<li>{i18n.ui("ff_no_hint1", lang)}</li>'
            f'<li>{i18n.ui("ff_no_hint2", lang)}</li>'
            f'<li>{i18n.ui("ff_no_hint3", lang)}</li>'
            f'<li>{i18n.ui("ff_no_hint4", lang)}</li>'
            f'</ul>'
            f'<span class="ffn">{i18n.ui("ff_no_tail", lang)}'
            f' ({i18n.ui("ff_look", lang)}: {looked})</span></div>')

    # media cache panel: 显示真实内容缓存大小，并提供「清理」按钮（清完自动重建）。
    # 与 ffmpeg 横幅左右并排一行。
    cstats = MEDIA.cache_stats()
    cache_panel = (
        f'<div class="cache"><b>{i18n.ui("cache_h", lang)}:</b> '
        f'<span class="cs" id="cache-size">{media.human_size(cstats["bytes"])}</span> '
        f'<span class="ffn" id="cache-nfiles">('
        f'{cstats["files"]} {i18n.ui("cache_files", lang)})</span>'
        f'<div class="ffp" id="cache-dir">{i18n.ui("cache_dir", lang)}: '
        f'<code>{cstats["dir"] or "-"}</code></div>'
        f'<div class="ffn">{i18n.ui("cache_note", lang)}</div>'
        f'<button class="clr" id="cache-clear" '
        f'data-confirm="{i18n.ui("cache_confirm", lang)}" '
        f'data-busy="{i18n.ui("cache_busy", lang)}" '
        f'data-done="{i18n.ui("cache_done", lang)}" '
        f'data-partial="{i18n.ui("cache_partial", lang)}" '
        f'data-errs="{i18n.ui("cache_errs", lang)}" '
        f'data-fail="{i18n.ui("cache_fail", lang)}">'
        f'{i18n.ui("cache_btn", lang)}</button>'
        f'<span class="ffn" id="cache-msg"></span></div>')
    # 三个状态面板并排一行（ffmpeg : 缓存 : 样例 = 2 : 1 : 1）。
    # 样例面板用 id="sample-panel" 作稳定锚点（类名 .sample 可能与别处冲突）。
    panels = (f'<div class="panels">'
              f'{ff_banner}{cache_panel}'
              f'<div class="sample" id="sample-panel">'
              f'{_sample_panel_html(lang)}</div>'
              f'</div>')
    html = f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<title>{i18n.ui("title", lang)}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a2e; }}
 h1 {{ font-size: 1.4rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; font-size: 0.85rem; }}
 td, th {{ border: 1px solid #d0d0d8; padding: 6px 8px; vertical-align: top; text-align: left; }}
 tr.cat td {{ background: #eef0f6; font-weight: 700; }}
 code {{ background: #f4f4f8; padding: 1px 4px; }}
 .mode {{ display: inline-block; padding: 2px 8px; border-radius: 10px; background: #d1fae5; font-size: .8rem; }}
 .warn {{ background: #fff7e6; padding: 8px 12px; border-radius: 6px; }}
 .lang {{ float: right; font-size: .85rem; }}
 .lang a {{ color: #4b5563; text-decoration: none; padding: 1px 6px; }}
 .lang a.on {{ font-weight: 700; color: #111827; background: #e5e7eb; border-radius: 4px; }}
 table.pv {{ display: none; }}
 button.pv {{ font: inherit; font-size: .78rem; padding: 2px 8px; cursor: pointer;
              border: 1px solid #c3c7d1; border-radius: 4px; background: #f6f7fa; }}
 button.pv.on {{ background: #111827; color: #fff; border-color: #111827; }}
 tr.detail td {{ background: #fbfbfd; }}
 pre.pvout {{ margin: 0; padding: 6px 8px; max-height: 320px; overflow: auto;
              white-space: pre-wrap; word-break: break-all; font-size: .78rem;
              background: #f4f4f8; border-radius: 4px; }}
 .pvmeta {{ color: #4b5563; font-size: .78rem; margin-bottom: 4px; }}
 .pvnote {{ color: #92400e; background: #fff7e6; padding: 4px 6px;
            border-radius: 4px; margin-bottom: 4px; display: inline-block; }}
 .ff {{ padding: 10px 14px; border-radius: 6px; font-size: .85rem;
        border-left: 4px solid; box-sizing: border-box; }}
 .ff.ok {{ background: #ecfdf5; border-color: #10b981; }}
 .ff.no {{ background: #fffbeb; border-color: #f59e0b; }}
 .ffp {{ font-size: .8rem; color: #374151; }}
 .ffn {{ color: #4b5563; font-size: .8rem; }}
 .ffl {{ margin: 4px 0 4px 1.1rem; padding: 0; }}
 .ffl li {{ margin: 1px 0; font-size: .8rem; }}
 .panels {{ display: flex; gap: 10px; align-items: stretch; margin: .6rem 0 1rem; }}
 /* 三栏比例 ffmpeg : 缓存 : 样例 = 2 : 1 : 1（等高拉伸）。 */
 .panels > .ff {{ flex: 2 1 0; min-width: 0; margin: 0; }}
 .panels > .cache {{ flex: 1 1 0; min-width: 0; }}
 .panels > .sample {{ flex: 1 1 0; min-width: 0; display: flex; }}
 .panels > .sample > .ff {{ flex: 1 1 auto; margin: 0; }}
 .cache {{ padding: 10px 14px; border-radius: 6px;
           font-size: .85rem; border-left: 4px solid #6366f1; background: #eef2ff;
           box-sizing: border-box; }}
 .cache .cs {{ font-weight: 700; font-size: 1rem; }}
 button.clr {{ font: inherit; font-size: .8rem; margin-top: 6px; padding: 3px 10px;
               cursor: pointer; border: 1px solid #c7cad6; border-radius: 4px;
               background: #fff; }}
button.clr:hover {{ background: #f3f4f6; }}
button.clr:disabled {{ opacity: .55; cursor: default; }}
</style></head><body>
<div class="lang">{i18n.ui("lang_label", lang)}: {switcher}</div>
<h1>{i18n.ui("title", lang)}</h1>
{panels}
<p>{i18n.ui("mode_label", lang)}: <span class="mode">{mode}</span>
&middot; {i18n.ui("json_label", lang)}:
<a href="/scenarios.json?lang={lang}">/scenarios.json</a></p>
<p class="warn">{i18n.ui("warn", lang)}</p>
<table>
<tr><th>{i18n.ui("th_no", lang)}</th><th>{i18n.ui("th_scenario", lang)}</th>
<th>{i18n.ui("th_url", lang)}</th><th>{i18n.ui("th_expect", lang)}</th>
<th>{i18n.ui("btn_show", lang)}</th></tr>
{''.join(rows)}
</table>
<h2>{i18n.ui("h_credentials", lang)}</h2>
<table>
<tr><th>{i18n.ui("th_use", lang)}</th><th>{i18n.ui("th_value", lang)}</th></tr>
{cred_rows}
</table>
<h2>{i18n.ui("h_manual", lang)}</h2>
<pre>curl -s -o seg.ts {req.base}/hls/aes128/seg/0.ts
curl -s -o key.bin {req.base}/hls/aes128/key.bin
openssl enc -d -aes-128-cbc -K "$(xxd -p key.bin | tr -d '\\n')" \\
  -iv 000102030405060708090a0b0c0d0e0f -in seg.ts | xxd -l 1
{i18n.ui("manual_note", lang)}</pre>
<script>
// 每行「查看响应」：点击后拉 /preview/<id>，把状态码 + 关键头 + 正文填进详情行。
// 纯前端、无外部依赖；有状态场景服务端只返回说明，不会产生副作用。
document.querySelectorAll('button.pv').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    var id = btn.dataset.id;
    var row = document.getElementById('d-' + id);
    var out = row.querySelector('.pvout');
    if (!row.hidden) {{                 // 已展开 -> 收起
      row.hidden = true;
      btn.classList.remove('on');
      btn.textContent = btn.dataset.show;
      return;
    }}
    row.hidden = false;
    btn.classList.add('on');
    btn.textContent = btn.dataset.hide;
    out.textContent = {i18n.ui("ph_loading", lang)!r};
    fetch('/preview/' + encodeURIComponent(id) + '?lang=' + {lang!r})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        var meta = '';
        if (d.status !== null && d.status !== undefined) {{
          meta = 'HTTP ' + d.status;
          if (d.headers && d.headers['Content-Type'])
            meta += '  ' + d.headers['Content-Type'];
          if (d.content_length !== null && d.content_length !== undefined)
            meta += '  ' + d.content_length + ' B';
        }}
        var text = '';
        if (d.note) text += '[' + {i18n.ui("ph_note", lang)!r} + '] ' + d.note + '\\n\\n';
        if (meta) text += meta + '\\n\\n';
        text += d.body || '';
        out.textContent = text;
      }})
      .catch(function () {{
        out.textContent = {i18n.ui("ph_error", lang)!r};
      }});
  }});
}});

// 缓存面板「清理」按钮：POST /cache/clear，成功后刷新大小与文件数。
// 失败（部分删除/环境护栏）时如实显示已删数量与首个失败原因。
(function () {{
  var btn = document.getElementById('cache-clear');
  if (!btn) return;
  btn.addEventListener('click', function () {{
    if (!window.confirm(btn.dataset.confirm)) return;
    var msg = document.getElementById('cache-msg');
    btn.disabled = true;
    msg.textContent = ' ' + btn.dataset.busy;
    fetch('/cache/clear', {{ method: 'POST' }})
      .then(function (r) {{
        return r.json().catch(function () {{
          return {{ ok: false, error: 'HTTP ' + r.status }};
        }});
      }})
      .then(function (d) {{
        document.getElementById('cache-size').textContent = d.human;
        document.getElementById('cache-nfiles').textContent =
          '(' + d.files + ' ' + {i18n.ui("cache_files", lang)!r} + ')';
        var nerr = (d.errors && d.errors.length) ? d.errors.length : 0;
        if (d.ok === false || nerr) {{
          msg.textContent = ' ' + btn.dataset.partial + ' (' +
            d.removed + ' ' + {i18n.ui("cache_files", lang)!r} + ', ' +
            nerr + ' ' + btn.dataset.errs +
            (d.error ? ': ' + d.error : '') + ')';
        }} else {{
          msg.textContent = ' ' + btn.dataset.done + ' (' +
            d.removed + ' ' + {i18n.ui("cache_files", lang)!r} + ', ' +
            d.human_freed + ')';
        }}
      }})
      .catch(function () {{ msg.textContent = ' ' + btn.dataset.fail; }})
      .then(function () {{ btn.disabled = false; }});
  }});
}})();

// 样例面板「处理/重建」按钮：POST /sample/rebuild，成功后整页刷新以更新面板。
(function () {{
  var btn = document.getElementById('sample-rebuild');
  if (!btn) return;
  btn.addEventListener('click', function () {{
    var msg = document.getElementById('sample-msg');
    btn.disabled = true;
    msg.textContent = ' ' + btn.dataset.busy;
    fetch('/sample/rebuild', {{ method: 'POST' }})
      .then(function (r) {{
        return r.json().catch(function () {{
          return {{ ok: false, error: 'HTTP ' + r.status }};
        }});
      }})
      .then(function (d) {{
        if (d.ok) {{
          msg.textContent = ' ' + btn.dataset.done + ' (' +
            d.files + ' ' + {i18n.ui("cache_files", lang)!r} + ', ' +
            d.human + ')';
          location.reload();
        }} else {{
          var nerr = (d.errors && d.errors.length) ? d.errors.length : 0;
          msg.textContent = ' ' + btn.dataset.fail +
            (d.error ? ': ' + d.error : (nerr ? ' (' + nerr + ')' : ''));
        }}
      }})
      .catch(function () {{ msg.textContent = ' ' + btn.dataset.fail; }})
      .then(function () {{ btn.disabled = false; }});
  }});
}})();
</script>
</body></html>"""
    return Resp(200, html, "text/html; charset=utf-8")


def h_scenarios(req: Req) -> Resp:
    lang = i18n.resolve_lang(req)
    items = []
    for sid, cat, n, title, path, expect in CATALOG:
        cat_l, title_l, expect_l = i18n.scenario(sid, cat, title, expect, lang)
        items.append({
            "id": sid,
            # category/title/expect are localised; id/category_key/path/item are
            # language-independent and safe to key on.
            "category": cat_l,
            "category_key": cat,
            "item": n,
            "title": title_l,
            "path": path,
            "expect": expect_l,
        })
    return json_resp({
        "lang": lang,
        "mode": "real" if MEDIA.is_real else "synthetic",
        "credentials": CREDENTIALS,
        "scenarios": items,
    })


# ---------------------------------------------------------------------------
# Preview endpoint 预览端点 —— 索引页每行「查看响应」用
# ---------------------------------------------------------------------------
#
# 设计：只登记**无副作用**的场景 -> 生成器。预览走独立的只读路径，
# 完全不触碰 STATE，也不复用有状态的 handler，因此可以随便点。
#
# 有状态场景（一次性 token / 限流 / 封禁 / 验证码 / 会话 / 签名 / Key-TTL /
# 续传三重等）**不登记**，h_preview 会返回一段静态说明 + 示例 URL 模板，
# 在 note 里说明「此场景有状态，预览不执行」。
#
# 生成器签名统一是 fn(base) -> str|bytes，与 playlists.* 保持一致。

def _pv_vod(base: str) -> str:
    return playlists.vod(base)


def _pv_live(base: str) -> str:
    return playlists.live(base, LIVE_START)


def _pv_live_event(base: str) -> str:
    return playlists.live_event(base, EVENT_START)


def _pv_paths_master(base: str) -> str:
    return playlists.paths_master(base)


def _pv_paths_media(base: str) -> str:
    return playlists.paths_media(base)


def _pv_nested_master(base: str) -> str:
    return playlists.nested_master(base)


def _pv_nested_sub(base: str) -> str:
    return playlists.nested_sub(base, "v1")


def _pv_nested_media(base: str) -> str:
    return playlists.nested_media("v1")


def _pv_renditions(base: str) -> str:
    return playlists.renditions_master(base)


def _pv_av_separate(base: str) -> str:
    return playlists.av_separate_master(base)


def _pv_fmp4(base: str) -> str:
    return playlists.fmp4(base)


def _pv_fmp4_av(base: str) -> str:
    return playlists.media_playlist(
        [f"seg/{i}.m4s" for i in range(playlists.N_SEG)],
        version=7, extra_head=['#EXT-X-MAP:URI="init.mp4"'])


def _pv_variable(base: str) -> str:
    return playlists.variable_vod(base, consistent=True)


def _pv_define(base: str) -> str:
    return playlists.define(base)


def _pv_series(base: str) -> str:
    return playlists.series_master(base)


def _pv_aes128(base: str) -> str:
    return playlists.aes128(base)


def _pv_aes128_msn(base: str) -> str:
    return playlists.aes128(base, msn_iv=True)


def _pv_aes128_master(base: str) -> str:
    return playlists.aes128_master(base)


def _pv_fairplay(base: str) -> str:
    return playlists.fairplay(base)


def _pv_widevine(base: str) -> str:
    return playlists.widevine(base)


def _pv_sample_aes(base: str) -> str:
    return playlists.sample_aes(base)


def _pv_aes_fmp4(base: str) -> str:
    return playlists.aes_fmp4(base)


def _pv_aes_disc(base: str) -> str:
    return playlists.aes_disc(base)


def _pv_ads(base: str) -> str:
    return playlists.ads(base)


def _pv_gap(base: str) -> str:
    return playlists.gap(base)


def _pv_discontinuity(base: str) -> str:
    return playlists.discontinuity(base)


def _pv_broken(base: str) -> str:
    return playlists.broken(base)


# 场景 id -> 无副作用的生成器。未登记的 id 一律走静态说明分支。
PREVIEWABLE = {
    "vod": _pv_vod,
    "live": _pv_live,
    "live-event": _pv_live_event,
    "paths": _pv_paths_master,
    "nested": _pv_nested_master,
    "renditions": _pv_renditions,
    "av-separate": _pv_av_separate,
    "fmp4": _pv_fmp4,
    "fmp4-av": _pv_fmp4_av,
    "variable": _pv_variable,
    "define": _pv_define,
    "series": _pv_series,
    "aes128": _pv_aes128,
    "aes128-master": _pv_aes128_master,
    "aes128-msn": _pv_aes128_msn,
    "fairplay": _pv_fairplay,
    "widevine": _pv_widevine,
    "sample-aes": _pv_sample_aes,
    "aes-fmp4": _pv_aes_fmp4,
    "aes-disc": _pv_aes_disc,
    "ads": _pv_ads,
    "gap": _pv_gap,
    "discontinuity": _pv_discontinuity,
    "broken": _pv_broken,
    "broken-enc": _pv_broken,
    "byterange": _pv_paths_media,
    "malformed": lambda base: playlists.malformed("crlf").decode(),
    "throttle": _pv_vod,
}

# 有状态场景 -> 一行说明（为什么不执行）。
STATEFUL_NOTES = {
    "onetime": ("每次拉列表都会签发新 token；预览会消耗 token，故不执行。"
                "示例：GET /hls/onetime/playlist.m3u8"),
    "ratelimit": ("分片端点按窗口计数，预览会触发限流，故不执行。"
                  "示例：GET /hls/ratelimit/seg/0.ts?n=5"),
    "ban": ("分片端点超阈值会封禁 IP，预览会误封，故不执行。"
            "示例：GET /hls/ban/seg/0.ts?n=8"),
    "captcha": ("分片端点未验证时返回 403 + 人机验证页；预览跳过。"
                "示例：GET /hls/captcha/seg/0.ts"),
    "session": ("分片端点需要会话 token；预览不签发会话。"
                "示例：先 GET /hls/session/start 再带 Cookie 取分片。"),
    "sign": ("分片 URL 带 HMAC 签名与时效，需先由列表签发；预览取列表即会登录签名。"
             "示例：GET /hls/sign/playlist.m3u8?ttl=60"),
    "key-expiry": ("密钥端点带 TTL + 签名，预览会消耗时效窗口，故不执行。"
                   "示例：GET /hls/key-expiry/playlist.m3u8"),
    "key-header": ("密钥端点要求自定义请求头，预览不带请求头会 403；"
                   "示例：GET /hls/key-header/key.bin（需 Header）。"),
    "key-rotation": ("密钥按分片轮换，需按列表顺序取；预览只说明。"
                     "示例：GET /hls/key-rotation/key/0.bin"),
    "guard": ("请求头（Referer/UA/Cookie）校验场景，预览不带请求头会 403。"
              "示例：GET /hls/guard/playlist.m3u8?auth=all"),
    "resume-triple": ("一次性 URL + 限时密钥 + 一次性 Range 三重叠加，预览会消耗状态。"
                      "示例：GET /hls/resume-triple/playlist.m3u8"),
    "range-ok": ("Range 行为在分片端点体现，需带 Range 请求分片；预览只给列表。"
                 "示例：GET /hls/range/ok/seg/0.ts + Range: bytes=0-99"),
    "range-none": ("同上：Range 行为在分片端点。"
                   "示例：GET /hls/range/none/seg/0.ts + Range: bytes=0-99"),
    "range-ignored": ("同上：Range 行为在分片端点。"
                      "示例：GET /hls/range/ignored/seg/0.ts + Range: bytes=0-99"),
    "range-badtotal": ("同上：Range 行为在分片端点。"
                       "示例：GET /hls/range/badtotal/seg/0.ts + Range: bytes=0-99"),
    "range-multipart": ("同上：Range 行为在分片端点。"
                        "示例：GET /hls/range/multipart-broken/seg/0.ts + Range"),
    "range-restarts": ("同上：Range 行为在分片端点。"
                       "示例：GET /hls/range/restarts/seg/0.ts + Range"),
    "aes-byterange": ("区间索引的是密文长度，列表由已加密的分片长度生成；"
                      "预览需要加密全量分片（较慢），故只说明。"
                      "示例：GET /hls/aes-byterange/playlist.m3u8"),
    "fmp4-byterange": ("列表里的区间来自各 .m4s 真实长度；"
                       "示例：GET /hls/fmp4/byterange/playlist.m3u8"),
}


def h_preview(req: Req, sid: str) -> Resp:
    """返回某场景的响应预览（JSON）：状态码 + 关键头 + 正文。

    无副作用场景 -> 真实调用 builder 取文本；有状态场景 -> 静态说明，不碰 STATE。
    """
    lang = i18n.resolve_lang(req)
    meta = next((row for row in CATALOG if row[0] == sid), None)
    if meta is None:
        raise Fail(404, f'{{"error": "unknown scenario: {sid}"}}\n',
                   "application/json")

    _, cat, n, title, path, expect = meta
    cat_l, title_l, expect_l = i18n.scenario(sid, cat, title, expect, lang)
    ctype = M3U8 if path.endswith(".m3u8") else BIN

    if sid in PREVIEWABLE:
        text = PREVIEWABLE[sid](req.base)
        body = text if isinstance(text, str) else text.decode(errors="replace")
        return json_resp({
            "id": sid,
            "title": title_l,
            "path": path,
            "status": 200,
            "content_type": ctype,
            "content_length": len(body),
            "headers": {"Content-Type": ctype,
                        "Content-Length": str(len(body))},
            "body": body,
            "note": None,
        })

    # 有状态：不执行，只给说明与示例。
    note = STATEFUL_NOTES.get(
        sid, "该场景需要特定请求头/参数或会产生状态，预览不执行。")
    return json_resp({
        "id": sid,
        "title": title_l,
        "path": path,
        "status": None,
        "content_type": ctype,
        "content_length": None,
        "headers": {},
        "body": "",
        "note": note,
        "expect": expect_l,
    })


# ---------------------------------------------------------------------------
# Cache endpoints 缓存端点 —— 索引页顶部的 media 缓存面板用
# ---------------------------------------------------------------------------
#
# GET  /cache.json   -> 只读统计（目录 / 文件数 / 字节 / 可读大小）
# POST /cache/clear  -> 清空真实内容缓存（可选 ?regenerate=0 只清不重建）
#
# 只操作 media provider 的真实内容缓存目录（media_cache/real），不碰其它文件。

def h_cache_json(req: Req) -> Resp:
    """返回缓存统计的 JSON。只读、无副作用。"""
    stats = MEDIA.cache_stats()
    return json_resp({
        "dir": stats["dir"],
        "files": stats["files"],
        "bytes": stats["bytes"],
        "human": media.human_size(stats["bytes"]),
        "real": stats["real"],
    })


def h_cache_clear(req: Req) -> Resp:
    """清空真实内容缓存。默认清完立即重建，?regenerate=0 则只清不重建。

    逐文件容错：任何单个文件删不掉（被占用、或环境删除护栏）都会记进 ``errors``，
    不会中断整批。返回 ``ok`` 表示是否全部成功；有失败时前端据此显示真实原因。
    用 ``BaseException`` 兜底是因为某些环境在批量删除超限时会抛 ``SystemExit``
    （进程级信号），只兜 ``Exception`` 会漏掉它并把连接掐断。
    """
    keep = req.q("regenerate")
    regenerate = keep not in ("0", "false", "no")
    try:
        result = MEDIA.clear_cache(regenerate=regenerate)
    except BaseException as e:
        # 兜底：理论上 clear_cache 内部已逐文件容错，这里只防意外。
        result = {"removed": 0, "bytes_freed": 0, "regenerated": False,
                  "regen_skipped": True,
                  "errors": [{"file": "*", "error": f"{type(e).__name__}: {e}"}],
                  "stats": MEDIA.cache_stats()}
    stats = result["stats"]
    errors = result.get("errors", [])
    return json_resp({
        "ok": not errors,
        "removed": result["removed"],
        "bytes_freed": result["bytes_freed"],
        "human_freed": media.human_size(result["bytes_freed"]),
        "regenerated": result["regenerated"],
        "regen_skipped": result.get("regen_skipped", False),
        "errors": errors,
        "error": (errors[0]["error"] if errors else None),
        "files": stats["files"],
        "bytes": stats["bytes"],
        "human": media.human_size(stats["bytes"]),
        "real": stats["real"],
    })


# ---------------------------------------------------------------------------
# Sample-video endpoints 样例视频端点 —— 索引页顶部的「样例视频」面板用
# ---------------------------------------------------------------------------
#
# GET  /sample.json    -> 只读状态（是否找到 / 路径 / 缓存是否来自样例 / 是否过期）
# POST /sample/rebuild -> 清空缓存并用样例（或内置源）重建；无请求体
#
# 样例放在项目内 tools/sample/source.{mp4,mkv,mov,webm,ts}（或 $HLS_SAMPLE / --sample）。

def h_sample_json(req: Req) -> Resp:
    """返回样例视频状态 JSON。只读、无副作用。"""
    stats = MEDIA.cache_stats()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return json_resp({
        "found": bool(MEDIA.sample),
        "path": MEDIA.sample,
        "using_sample": MEDIA.using_sample,
        "stale": MEDIA.cache_is_stale(),
        "segments": 12 if MEDIA.is_real else 0,
        "files": stats["files"],
        "bytes": stats["bytes"],
        "human": media.human_size(stats["bytes"]),
        "real": stats["real"],
        "expected_dir": os.path.join(root, "tools", "sample"),
    })


def h_sample_rebuild(req: Req) -> Resp:
    """清空并用样例（或内置测试图）重建真实缓存。POST，无请求体。

    实质是 ``clear_cache(regenerate=True)``——它会先删旧缓存再用当前样例重建。
    有文件删不掉时（errors 非空）不重建，返回 ``ok:false`` 并如实上报。
    """
    try:
        result = MEDIA.clear_cache(regenerate=True)
    except BaseException as e:
        result = {"removed": 0, "bytes_freed": 0, "regenerated": False,
                  "regen_skipped": True,
                  "errors": [{"file": "*", "error": f"{type(e).__name__}: {e}"}],
                  "stats": MEDIA.cache_stats()}
    # 重建成功后刷新「当前缓存来源」标记（有样例才算来自样例）。
    if result["regenerated"]:
        MEDIA._using_sample = bool(MEDIA.sample)
    stats = result["stats"]
    errors = result.get("errors", [])
    return json_resp({
        "ok": result["regenerated"] and not errors,
        "using_sample": MEDIA.using_sample,
        "removed": result["removed"],
        "regenerated": result["regenerated"],
        "errors": errors,
        "error": (errors[0]["error"] if errors else None),
        "files": stats["files"],
        "bytes": stats["bytes"],
        "human": media.human_size(stats["bytes"]),
        "real": stats["real"],
    })


# --- 6. VOD / live ---- 普通 VOD、滑窗直播、Event 列表 ------------------------

def h_vod(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_vod_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("vod", int(i)))


def h_live(req: Req) -> Resp:
    return m3u8(playlists.live(req.base, LIVE_START))


def h_live_seg(req: Req, msn: str) -> Resp:
    return ts(MEDIA.segment("live", int(msn)))


def h_live_event(req: Req) -> Resp:
    return m3u8(playlists.live_event(req.base, EVENT_START))


def h_live_event_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("event", int(i)))


# --- 1. AES-128 ---- 标准与 MSN-IV 两种 IV 来源、SESSION-KEY、多码率 ----------

def h_aes128(req: Req) -> Resp:
    return m3u8(playlists.aes128(req.base))


def h_aes128_msn(req: Req) -> Resp:
    return m3u8(playlists.aes128(req.base, msn_iv=True))


def h_aes128_variant(req: Req, res: str) -> Resp:
    return m3u8(playlists.aes128(req.base))


def h_aes128_master(req: Req) -> Resp:
    return m3u8(playlists.aes128_master(req.base))


def h_aes128_key(req: Req) -> Resp:
    return key_resp(keys.AES128_KEY)


def h_aes128_seg(req: Req, i: str) -> Resp:
    return ts(enc_segment("vod", int(i), keys.AES128_KEY, IV))


def h_aes128_msn_seg(req: Req, i: str) -> Resp:
    n = int(i)
    return ts(enc_segment("vod", n, keys.AES128_KEY, n.to_bytes(16, "big")))


# --- 2. key expiry ---- 密钥 URL 带 TTL/签名，过期后 403 --------------------

def _exp_sig(req: Req, ttl_default: int, min_ttl: int) -> tuple[int, str]:
    """按 ?ttl= 计算过期时刻并返回 (exp, 签名)；ttl 不低于 min_ttl。"""
    ttl = max(min_ttl, req.qint("ttl", ttl_default))
    exp = int(time.time()) + ttl
    return exp, keys.sign(f"key:{exp}")


def h_key_expiry(req: Req) -> Resp:
    exp, sig = _exp_sig(req, 30, 5)
    return m3u8(playlists.key_expiry(req.base, exp, sig))


def h_key_expiry_key(req: Req) -> Resp:
    """密钥端点：签名错 403；已过期也 403（提示重拉列表）。"""
    exp = req.qint("exp", 0)
    sig = req.q("s", "")
    if not keys.verify(f"key:{exp}", sig):
        raise Fail(403, '{"error": "bad signature"}\n', "application/json")
    if exp < time.time():
        raise Fail(403, '{"error": "key expired, re-fetch the playlist"}\n',
                   "application/json")
    return key_resp(keys.AES128_KEY)


def h_key_expiry_seg(req: Req, i: str) -> Resp:
    return ts(enc_segment("vod", int(i), keys.AES128_KEY, IV))


# --- 3. SAMPLE-AES / DRM ---- identity / FairPlay / Widevine，均应被拒绝 -----
#
# 这三个场景的密钥端点即便能取到"密钥"，也不应是可用密钥：
# sample-aes 给一把假的；fairplay/widevine 根本没有对应的取密钥端点。

def h_sample_aes(req: Req) -> Resp:
    return m3u8(playlists.sample_aes(req.base))


def h_sample_aes_key(req: Req) -> Resp:
    return key_resp(keys.derive16("sample-aes-fake"))


def h_sample_aes_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("sampleaes", int(i)))


def h_fairplay(req: Req) -> Resp:
    return m3u8(playlists.fairplay(req.base))


def h_fairplay_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("fairplay", int(i)))


def h_widevine(req: Req) -> Resp:
    return m3u8(playlists.widevine(req.base))


def h_widevine_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("widevine", int(i)))


# --- 4. key needs header ---- 密钥端点要求自定义请求头 X-Api-Key -------------

def h_key_header(req: Req) -> Resp:
    return m3u8(playlists.key_header(req.base))


def h_key_header_key(req: Req) -> Resp:
    """缺少或不匹配 X-Api-Key 直接 403。"""
    if req.header("X-Api-Key") != API_KEY:
        raise Fail(403, '{"error": "missing X-Api-Key header"}\n',
                   "application/json")
    return key_resp(keys.AES128_KEY)


def h_key_header_seg(req: Req, i: str) -> Resp:
    return ts(enc_segment("vod", int(i), keys.AES128_KEY, IV))


# --- 15. key rotation ---- 每个分片前换一条 EXT-X-KEY（逐片轮换） -------------

def h_key_rotation(req: Req) -> Resp:
    return m3u8(playlists.key_rotation(req.base))


def h_key_rotation_key(req: Req, i: str) -> Resp:
    """第 i 片对应的轮换密钥（用 12 个密钥循环）。"""
    return key_resp(keys.ROT_KEYS[int(i) % len(keys.ROT_KEYS)])


def h_key_rotation_seg(req: Req, i: str) -> Resp:
    """用与第 i 片密钥端点**一致**的密钥加密该分片。"""
    k = keys.ROT_KEYS[int(i) % len(keys.ROT_KEYS)]
    return ts(enc_segment("vod", int(i), k, IV))


# --- 7. referer / UA / cookie ---- 分片请求的 Referer/UA/Cookie 校验 ----------
#
# ?auth=referer|ua|cookie|all 决定校验哪几项；缺任一被要求的项即 403。

def h_guard(req: Req) -> Resp:
    auth = req.q("auth", "all")
    if auth not in ("referer", "ua", "cookie", "all"):
        auth = "all"
    return m3u8(playlists.guard(req.base, auth=auth))


def h_guard_seg(req: Req, i: str) -> Resp:
    auth = req.q("auth", "all")
    if auth in ("referer", "all") and req.header("Referer") != REFERER:
        raise Fail(403, '{"error": "bad Referer"}\n', "application/json")
    if auth in ("ua", "all") and not req.header("User-Agent").startswith(UA_PREFIX):
        raise Fail(403, '{"error": "bad User-Agent"}\n', "application/json")
    if auth in ("cookie", "all") and COOKIE_VALUE not in req.header("Cookie"):
        raise Fail(403, '{"error": "missing cookie"}\n', "application/json")
    return ts(MEDIA.segment("guard", int(i)))


# --- 8. signed URLs ---- HMAC 签名的分片 URL + 时效 --------------------------
#
# 每片签名 = HMAC("seg:{i}:{exp}")，因此签名与分片号、过期时刻都绑定。

def h_sign(req: Req) -> Resp:
    ttl = max(5, req.qint("ttl", 60))
    exp = int(time.time()) + ttl
    sigs = [keys.sign(f"seg:{i}:{exp}") for i in range(playlists.N_SEG)]
    return m3u8(playlists.signed(req.base, exp, sigs))


def h_sign_seg(req: Req, i: str) -> Resp:
    exp = req.qint("exp", 0)
    sig = req.q("s", "")
    if not keys.verify(f"seg:{i}:{exp}", sig):
        raise Fail(403, '{"error": "bad signature"}\n', "application/json")
    if exp < time.time():
        raise Fail(403, '{"error": "URL expired, re-fetch the playlist"}\n',
                   "application/json")
    return ts(MEDIA.segment("sign", int(i)))


# --- 10. session ---- 必须先建会话，否则 401 --------------------------------
#
# 流程：GET /hls/session/start 签发 token（返回 JSON 并 Set-Cookie）→ 后续取分片
# 时带上 Cookie HLSSESS=<token> 或 ?session=<token>，否则 401。
# 列表本身不校验会话，只有分片端点校验（客户端常见的"能拉到列表却拉不到分片"）。

def h_session_start(req: Req) -> Resp:
    """签发会话 token：既写 Set-Cookie，也在 JSON 体里返回，方便脚本客户端。"""
    token = STATE.new_session()
    return json_resp({"session": token, "expires_in": 600,
                      "note": "pass as Cookie HLSSESS=<token> "
                              "or ?session=<token> on segments"},
                     headers=[("Set-Cookie", f"HLSSESS={token}; Path=/; Max-Age=600")])


def h_session(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_session_seg(req: Req, i: str) -> Resp:
    """分片端点：优先取 ?session=，其次取 Cookie HLSSESS；无效则 401。"""
    token = req.q("session") or _cookie(req, "HLSSESS")
    if not STATE.session_valid(token):
        raise Fail(401, '{"error": "no valid session; GET /hls/session/start"}\n',
                   "application/json")
    return ts(MEDIA.segment("session", int(i)))


def _cookie(req: Req, name: str) -> str | None:
    """从 Cookie 头里按名取一个值；不存在时返回 None（不做解码，够用即可）。"""
    for part in req.header("Cookie").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# --- 9. rate limit / throttle / ban / captcha ---- 限流、限速、封禁、验证码 ---
#
# 四个端点分别模拟 CDN/防盗链的四种"软性拒绝"：429 限流、低速限速、
# 403 封禁、403 人机验证。它们的列表端点都是正常 m3u8，考的是分片请求。

def h_ratelimit(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_ratelimit_seg(req: Req, i: str) -> Resp:
    """窗口 30s 内第 n+1 次请求起返回 429 + Retry-After（n 默认 5）。"""
    n = req.qint("n", 5)
    count = STATE.hit("ratelimit", req.ip, 30)
    if count > n:
        raise Fail(429, '{"error": "rate limited"}\n', "application/json",
                   headers=[("Retry-After", "5")])
    return ts(MEDIA.segment("ratelimit", int(i)))


def h_throttle(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_throttle_seg(req: Req, i: str) -> Resp:
    """以 kbps 限速分发分片（kbps 默认 64，越低传输越慢）。"""
    kbps = max(1, req.qint("kbps", 64))
    return Resp(200, MEDIA.segment("throttle", int(i)), TS, kbps=kbps)


def h_ban(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_ban_seg(req: Req, i: str) -> Resp:
    """窗口 60s 内超过 n 次（默认 8）即封禁该 IP 60s，期间一律 403。"""
    n = req.qint("n", 8)
    remaining = STATE.is_banned("ban", req.ip)
    if remaining > 0:
        raise Fail(403,
                   f'{{"error": "banned", "retry_after": {int(remaining)}}}\n',
                   "application/json")
    count = STATE.hit("ban", req.ip, 60)
    if count > n:
        STATE.ban("ban", req.ip, 60)
        raise Fail(403, '{"error": "banned", "retry_after": 60}\n',
                   "application/json")
    return ts(MEDIA.segment("ban", int(i)))


def h_captcha(req: Req) -> Resp:
    return m3u8(playlists.vod(req.base))


def h_captcha_seg(req: Req, i: str) -> Resp:
    """未通过验证时返回 403 + 一页 HTML 提示（含解题用的 code）。"""
    if STATE.captcha_ok(req.ip):
        return ts(MEDIA.segment("captcha", int(i)))
    STATE.bump("captcha_shown")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Verification required</title></head><body>
<h1>Human verification required</h1>
<p>To continue fetching segments, solve:</p>
<p style="font-size:2rem;font-family:monospace">7 + 3 = ?</p>
<p>Solve at: <code>GET /hls/captcha/solve?code={CAPTCHA_CODE}</code></p>
</body></html>"""
    raise Fail(403, html, "text/html; charset=utf-8")


def h_captcha_solve(req: Req) -> Resp:
    """提交正确 code 后，为当前 IP 放行（有效期由 state 决定，约 10 分钟）。"""
    if req.q("code") != CAPTCHA_CODE:
        raise Fail(403, "wrong code\n")
    STATE.solve_captcha(req.ip)
    return json_resp({"ok": True, "note": "captcha solved for this IP "
                                          "(10 min)"})


# --- 11. broken / gap ---- 缺失/500/乱码/截断分片，与 EXT-X-GAP ---------------
#
# 分片号约定（broken 场景）：2→404、4→500、6→同长度乱码、8→截断到一半，
# 其余正常。这样客户端必须能区分"网络错误"与"内容错误"。

def h_broken(req: Req) -> Resp:
    return m3u8(playlists.broken(req.base))


def h_broken_enc(req: Req) -> Resp:
    return m3u8(playlists.broken(req.base, encrypted=True))


def h_broken_seg(req: Req, i: str) -> Resp:
    n = int(i)
    if n == 2:
        raise Fail(404, "segment gone\n")                       # 缺失
    if n == 4:
        raise Fail(500, '{"error": "upstream exploded"}\n', "application/json")  # 源站错误
    if n == 6:
        plain = MEDIA.segment("broken", n)
        return ts(bytes(b ^ 0xFF for b in plain))  # 同长度乱码：字节翻转
    if n == 8:
        plain = MEDIA.segment("broken", n)
        return ts(plain[: len(plain) // 2])       # 截断：只回前一半
    return ts(MEDIA.segment("broken", n))


def h_broken_enc_seg(req: Req, i: str) -> Resp:
    """第 6 片故意用错误的密钥加密 —— 解密后得到乱码而非合法 TS。"""
    n = int(i)
    k = keys.WRONG_KEY if n == 6 else keys.AES128_KEY
    return ts(enc_segment("vod", n, k, IV))


def h_gap(req: Req) -> Resp:
    return m3u8(playlists.gap(req.base))


def h_gap_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("gap", int(i)))


# --- 14. discontinuity ---- PTS 跳变 + EXT-X-DISCONTINUITY -------------------
#
# clip A 走 vod 素材（PTS 正常递增），clip B 从 index 0 重新开始（PTS 回跳），
# 客户端必须靠 EXT-X-DISCONTINUITY 才能正确处理这条时间轴跳变。

def h_discontinuity(req: Req) -> Resp:
    return m3u8(playlists.discontinuity(req.base))


def h_disc_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("vod", int(i)))


def h_disc_clipb(req: Req, i: str) -> Resp:
    # 另一组素材从 index 0 起算 -> PTS 时间轴重开，相对 clip A 出现可见回跳
    return ts(MEDIA.segment("clipb", int(i)))


# --- 15. one-time URLs ---- 一次性分片 URL（每个 URL 只能成功一次） ----------
#
# 拉列表时签发 N 个 token，每个 token 用掉即作废；客户端若要续传必须重新拉列表。

def h_onetime(req: Req) -> Resp:
    tokens = [STATE.mint_onetime() for _ in range(playlists.N_SEG)]
    return m3u8(playlists.onetime(req.base, tokens))


def h_onetime_seg(req: Req, i: str) -> Resp:
    """token 无效或已用过则 403，必须重新拉列表拿新 URL。"""
    if not STATE.consume_onetime(req.q("t")):
        raise Fail(403,
                   '{"error": "one-time token invalid or already used; '
                   're-fetch the playlist"}\n', "application/json")
    return ts(MEDIA.segment("onetime", int(i)))


# --- 12. A/V separation ---- 音视频分离：EXT-X-MEDIA 音频组 + 纯视频/纯音频变体 -

def h_av_master(req: Req) -> Resp:
    return m3u8(playlists.av_separate_master(req.base))


def h_av_video_playlist(req: Req) -> Resp:
    return m3u8(playlists.media_playlist([f"seg/{i}.ts" for i in range(playlists.N_SEG)]))


def h_av_video_seg(req: Req, i: str) -> Resp:
    # 纯视频变体：分片里只有视频 PID
    return ts(MEDIA.segment("avsep", int(i), streams="v"))


def h_av_audio_playlist(req: Req) -> Resp:
    return m3u8(playlists.media_playlist([f"seg/{i}.ts" for i in range(playlists.N_SEG)]))


def h_av_audio_seg(req: Req, i: str) -> Resp:
    # 独立音频组：分片里只有音频 PID
    return ts(MEDIA.segment("audio", int(i), streams="a"))


# --- 13. fMP4 ---- EXT-X-MAP + .m4s，含双轨与 byterange 变体 -----------------

def h_fmp4(req: Req) -> Resp:
    return m3u8(playlists.fmp4(req.base))


def h_fmp4_init(req: Req) -> Resp:
    """初始化段 init.mp4（单轨视频 ftyp+moov）。"""
    return Resp(200, MEDIA.fmp4_init(), "video/mp4")


def h_fmp4_seg(req: Req, i: str) -> Resp:
    """媒体分片 seg/N.m4s（styp+moof+mdat）。"""
    return Resp(200, MEDIA.fmp4_segment(int(i)), MP4)


def h_fmp4_byterange(req: Req) -> Resp:
    """列表里的 byterange 长度来自各分片真实字节长度。"""
    init_len = len(MEDIA.fmp4_init())  # provider caches internally / provider 内部有缓存
    lens = big_file("fmp4_lens",
                    lambda: [len(MEDIA.fmp4_segment(i))
                             for i in range(playlists.N_SEG)])
    return m3u8(playlists.fmp4_byterange(req.base, init_len, lens))


def h_fmp4_combined(req: Req) -> Resp:
    """把 init + 全部分片拼成一个大文件，供 byterange 用 Range 取片。"""
    def build() -> bytes:
        return MEDIA.fmp4_init() + b"".join(
            MEDIA.fmp4_segment(i) for i in range(playlists.N_SEG))

    return Resp(200, big_file("fmp4_blob", build), MP4)


def h_fmp4_av(req: Req) -> Resp:
    """音视频合一的 fMP4 列表（单 init.mp4 + 双轨 .m4s）。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:6",
             "#EXT-X-INDEPENDENT-SEGMENTS", '#EXT-X-MAP:URI="init.mp4"']
    for i in range(playlists.N_SEG):
        lines.append(f"#EXTINF:{playlists.SEG_DUR:.3f},")
        lines.append(f"seg/{i}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return Resp(200, "\n".join(lines) + "\n", M3U8)


def h_fmp4_av_init(req: Req) -> Resp:
    """双轨（video+audio，tracks="va"）的 init 段。"""
    return Resp(200, MEDIA.fmp4_init(tracks="va"), MP4)


def h_fmp4_av_seg(req: Req, i: str) -> Resp:
    """双轨 fMP4 分片：moof 里含 video 与 audio 两个 traf。"""
    return Resp(200, MEDIA.fmp4_segment(int(i), tracks="va"), MP4)


# --- TS byterange ----------------------------------------------------------------------------------

def h_byterange(req: Req) -> Resp:
    """TS 分片用 byterange 索引：长度取自各分片真实字节数。"""
    lens = big_file("ts_lens",
                    lambda: [len(MEDIA.segment("vod", i))
                             for i in range(playlists.N_SEG)])
    return m3u8(playlists.byterange_ts(req.base, lens))


def h_byterange_big(req: Req) -> Resp:
    """把所有 TS 分片拼成一个大文件，供 Range 取片。"""
    def build() -> bytes:
        return b"".join(MEDIA.segment("vod", i) for i in range(playlists.N_SEG))

    return ts(big_file("ts_big", build))


# --- 15. Range requests / resume difficulty ---- 各种区间请求行为（见 rmode） -

# 六种区间行为模式：
#   ok               RFC 正确实现（206/416 + 正确的 Content-Range）
#   none             明确声明 Accept-Ranges: none，永远 200 全量
#   ignored          谎称支持，实际忽略 Range，永远 200 全量
#   badtotal         返回 206，但 Content-Range 的总长是假的（真值一半）
#   multipart-broken 多区间请求只回第一段，且不带 multipart/byteranges
#   restarts         首次 Range 生效，之后"忘记进度"退回 200 全量
RANGE_MODES = ["ok", "none", "ignored", "badtotal", "multipart-broken",
               "restarts"]


def _parse_range(rng: str | None, total: int):
    """Parse a Range header. Returns a list of inclusive (start, end),
    "ignore" (unusable -> 200 full body) or "unsat" (-> 416).
    解析 Range 头，返回闭区间 (start, end) 列表，或 "ignore"（不可用 -> 200 全量）
    或 "unsat"（无法满足 -> 416）。"""
    if not rng or not rng.strip().lower().startswith("bytes="):
        return "ignore"
    spec = rng.strip()[len("bytes="):]
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" not in part:
            return "ignore"
        head, _, tail = part.partition("-")
        try:
            if head == "":                       # suffix range: bytes=-N 后缀区间
                if tail == "":
                    return "ignore"
                n = int(tail)
                if n <= 0:
                    return "unsat"
                start, end = max(0, total - n), total - 1
            else:
                start = int(head)
                end = total - 1 if tail == "" else int(tail)
                if start > end or start >= total:
                    return "unsat"
                end = min(end, total - 1)        # 右边界裁剪到文件末尾
        except ValueError:
            return "ignore"
        ranges.append((start, end))
    return ranges or "ignore"


def _range_response(mode: str, rng: str | None, body: bytes, key: str,
                    state: State) -> tuple[int, bytes, list[tuple[str, str]]]:
    """Emulate the various ways real servers mishandle HTTP Range.
    模拟真实服务器处理 HTTP Range 时的各种错误姿势。

    返回 (status, body, extra_headers)，由 Handler._send 负责写回。
    """
    total = len(body)

    if mode == "none":                       # honest "I don't do ranges" 诚实声明
        return 200, body, [("Accept-Ranges", "none")]
    if mode == "ignored":                    # advertises bytes, ignores Range
        return 200, body, [("Accept-Ranges", "bytes")]

    if mode == "restarts" and not state.range_first_use(key):
        # resume works once, then the server "forgets" and resends from 0
        # 续传只生效一次，之后服务器"忘记进度"、从 0 重发
        return 200, body, [("Accept-Ranges", "bytes")]

    parsed = _parse_range(rng, total)
    if parsed == "ignore":
        return 200, body, [("Accept-Ranges", "bytes")]
    if parsed == "unsat":
        # 416 必须带 Content-Range: bytes */total
        return 416, b"", [("Accept-Ranges", "bytes"),
                          ("Content-Range", f"bytes */{total}")]

    start, end = parsed[0]                   # 多区间时只取第一段
    slice_ = body[start: end + 1]
    if mode == "badtotal":                   # 206 with a lying total length
        claimed = max(1, total // 2)         # 谎报总长（真值一半）
    else:
        claimed = total
    headers = [("Accept-Ranges", "bytes"),
               ("Content-Range", f"bytes {start}-{end}/{claimed}")]
    # multipart-broken: a multi-range request is answered with only the first
    # range and a single-part Content-Range (no multipart/byteranges wrapper)
    # multipart-broken：多区间请求只回第一段，且用单段 Content-Range 而非
    # multipart/byteranges 包装。
    return 206, slice_, headers


def h_range(req: Req, mode: str) -> Resp:
    """列表端点：mode 决定分片端点如何对待 Range（见上表）。"""
    if mode not in RANGE_MODES:
        raise Fail(404, f"unknown range mode; try one of: "
                        f"{', '.join(RANGE_MODES)}\n")
    return m3u8(playlists.media_playlist(
        [f"seg/{i}.ts" for i in range(playlists.N_SEG)]))


def h_range_seg(req: Req, mode: str, i: str) -> Resp:
    """分片端点：把 rmode 传给 _range_response，由它决定 Range 行为。"""
    if mode not in RANGE_MODES:
        raise Fail(404, f"unknown range mode; try one of: "
                        f"{', '.join(RANGE_MODES)}\n")
    return ts(MEDIA.segment("range", int(i)), rmode=mode)


# --- 5. paths / redirect ---- 混合 URI 形态与 302 重定向 ---------------------
#
# 列表里同时出现绝对路径、相对路径、上跳路径、查询串等多种 URI 形态，
# 以及"列表/分片都走 302"的间接分发，考验客户端的 URL 解析与重定向处理。

def h_paths_master(req: Req) -> Resp:
    return m3u8(playlists.paths_master(req.base))


def h_paths_media(req: Req, _prefix: str, res: str) -> Resp:
    """媒体列表端点；_prefix 是路径前缀组，仅占位，不参与判断。"""
    return m3u8(playlists.paths_media(req.base))


def h_paths_redir(req: Req) -> Resp:
    """302 跳到 360p 媒体列表（相对 base 拼绝对地址）。"""
    return Resp(302, b"", headers=[("Location", f"{req.base}/hls/paths/rel/360p.m3u8")])


def h_paths_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("paths", int(i)))


def h_paths_seg_redir(req: Req, i: str) -> Resp:
    """302 跳到一个不存在于列表里的真实分片地址。"""
    return Resp(302, b"", headers=[("Location", f"{req.base}/hls/paths/seg/{i}.ts")])


# --- 18. multi-level nested master ---- 多层嵌套 master（基准传递陷阱） ------
#
# 路由正则的捕获组数必须与 handler 形参一致：
#   /hls/nested/(v1|v2)/sub.m3u8                 -> 1 组 -> (req, tier)
#   /hls/nested/(v1|v2|shared)/(\d{3}p).m3u8     -> 2 组 -> (req, tier, _res)
# 注意 media 路由有个多余的分辨率组，因此 handler 用 _res 接收并忽略它。

def h_nested_master(req: Req) -> Resp:
    """顶层 master：指向下一层的 sub.m3u8（非 media，仍是 master）。"""
    return m3u8(playlists.nested_master(req.base))


def h_nested_sub(req: Req, tier: str) -> Resp:
    """中间层 master：按 tier 分叉，再指向更下一层的媒体列表。"""
    if tier not in playlists.NESTED_TIERS:
        raise Fail(404, f"unknown tier; try: {', '.join(playlists.NESTED_TIERS)}\n")
    return m3u8(playlists.nested_sub(req.base, tier))


def h_nested_media(req: Req, tier: str, _res: str) -> Resp:
    """tier 层级的 media 列表；_res 只是路由里的占位捕获组，不使用。"""
    if tier not in playlists.NESTED_TIERS:
        raise Fail(404, f"unknown tier; try: {', '.join(playlists.NESTED_TIERS)}\n")
    return m3u8(playlists.nested_media(tier))


def h_nested_seg(req: Req, tier: str, i: str) -> Resp:
    """分片：按 tier 分组内容，验证相对 base 是否正确传递到最深一层。"""
    if tier not in playlists.NESTED_TIERS:
        raise Fail(404, f"unknown tier; try: {', '.join(playlists.NESTED_TIERS)}\n")
    return ts(MEDIA.segment(f"nested-{tier}", int(i)))


# --- 24. series / episodes ---- 剧集：多集 + 上/下一集 + 共享密钥 -----------
#
# 布局见 playlists.series_*。三个 handler 的捕获组数与路由严格对应：
#   /hls/series/master.m3u8            -> 0 组 -> (req)
#   /hls/series/key.bin                -> 0 组 -> (req)
#   /hls/series/ep/(\d+)/playlist.m3u8 -> 1 组 -> (req, ep)
#   /hls/series/ep/(\d+)/seg/(\d+).ts  -> 2 组 -> (req, ep, i)

SERIES_KEY = keys.AES128_KEY   # 全集（含上/下一集）共用的一把 AES-128 密钥


def h_series_master(req: Req) -> Resp:
    """顶层 master：每集一个变体。"""
    return m3u8(playlists.series_master(req.base))


def h_series_key(req: Req) -> Resp:
    """全集共用的密钥端点（上/下一集都引用它）。"""
    return key_resp(SERIES_KEY)


def h_series_ep(req: Req, ep: str) -> Resp:
    """第 ep 集的媒体列表（含 EXT-X-KEY 与 PREV/NEXT 关系标签）。"""
    try:
        n = int(ep)
    except ValueError:
        raise Fail(404, "bad episode number\n")
    if n not in playlists.SERIES_EPISODES:
        raise Fail(404, f"unknown episode; try 1..{playlists.SERIES_EP_COUNT}\n")
    return m3u8(playlists.series_episode(req.base, n))


def h_series_seg(req: Req, ep: str, i: str) -> Resp:
    """第 ep 集的分片，用共享密钥加密；素材按集分组。"""
    try:
        n = int(ep)
    except ValueError:
        raise Fail(404, "bad episode number\n")
    if n not in playlists.SERIES_EPISODES:
        raise Fail(404, f"unknown episode; try 1..{playlists.SERIES_EP_COUNT}\n")
    return ts(enc_segment(f"series-ep{n}", int(i), SERIES_KEY, IV))


# --- 23. resume triple ---- 一次性 URL + 限时密钥 + 一次性 Range 三重叠加 ----
#
# Three independent reasons a mid-download resume cannot succeed, all active at
# once:
# 三个各自独立、足以让"中途续传"失败的原因，同时生效：
#   1. segment URLs are one-time tokens (re-fetching the playlist mints new
#      URLs, so a saved partial-download URL is dead);
#      分片 URL 是一次性 token（重拉列表会签发新 URL，之前保存的 URL 已失效）；
#   2. the key URL expires, so even a fully re-fetched playlist goes stale;
#      密钥 URL 会过期，所以即便重新拉列表也可能已陈旧；
#   3. Range is honoured only on the very first request for a URL, then the
#      server reverts to 200 + full body.
#      Range 只对某 URL 的第一次请求生效，其后退回 200 全量。
# A correct client must detect "resume is impossible" and restart the asset.
# 正确的客户端必须识别出"无法续传"，并重新整段下载。

def h_resume_triple(req: Req) -> Resp:
    """列表端点：签发一次性 token + 限时密钥签名。"""
    exp, sig = _exp_sig(req, 60, 5)
    tokens = [STATE.mint_onetime() for _ in range(playlists.N_SEG)]
    return m3u8(playlists.resume_triple(req.base, tokens, exp, sig))


def h_resume_triple_key(req: Req) -> Resp:
    """限时密钥端点：签名不对 403，过期也 403。"""
    exp = req.qint("exp", 0)
    sig = req.q("s", "")
    if not keys.verify(f"key:{exp}", sig):
        raise Fail(403, '{"error": "bad signature"}\n', "application/json")
    if exp < time.time():
        raise Fail(403, '{"error": "key expired; re-fetch the playlist"}\n',
                   "application/json")
    return key_resp(keys.AES128_KEY)


def h_resume_triple_seg(req: Req, i: str) -> Resp:
    """分片端点：三重机制叠加（一次性 URL + 限时密钥 + 一次性 Range）。"""
    n = int(i)
    # (1) 一次性 URL：同一 URL 第二次拉取直接拒绝，因此对已用过的 URL
    #     连 Range 请求都发不出来。
    if not STATE.consume_onetime(req.q("t")):
        raise Fail(403,
                   '{"error": "one-time token already used; re-fetch the '
                   'playlist for fresh URLs"}\n', "application/json")
    body = enc_segment("vod", n, keys.AES128_KEY, IV)
    # (3) 即便在第一次拉取中，Range 对每个 URL 也最多生效一次。
    return ts(body, rmode="restarts")


# --- 22. ad signalling ---- DATERANGE/SCTE35 + CUE-OUT/IN + GAP 三种标记 -----
#
# 广告插播的三种典型标记同时出现：DATERANGE（XML 时间）、SCTE-35（二进制 hex）、
# 以及 CUE-OUT/CUE-IN 对；广告素材走独立分组（内容与正片不同）。

def h_ads(req: Req) -> Resp:
    return m3u8(playlists.ads(req.base))


def h_ads_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("ads", int(i)))


def h_ads_ad(req: Req, i: str) -> Resp:
    # 广告素材来自另一分组（内容与正片不同）
    return ts(MEDIA.segment("adcre", int(i)))


# --- 21. encryption combinations ---- AES+fMP4 / AES+byterange / 换密钥 -----
#
# 三种"加密与其他特性叠加"的场景：加密的 fMP4、加密后仍按 byterange 索引、
# 在 EXT-X-DISCONTINUITY 处换密钥。

def h_aes_fmp4(req: Req) -> Resp:
    return m3u8(playlists.aes_fmp4(req.base))


def h_aes_fmp4_init(req: Req) -> Resp:
    # init 段是**明文**的（Apple 的做法：只加密 .m4s 分片，不加密 init）。
    return Resp(200, MEDIA.fmp4_init(tracks="va"), MP4)


def h_aes_fmp4_seg(req: Req, i: str) -> Resp:
    """fMP4 媒体分片：先取明文再整体 AES-128-CBC 加密。"""
    plain = MEDIA.fmp4_segment(int(i), tracks="va")
    return Resp(200, crypto.aes128_cbc_encrypt(keys.AES128_KEY, IV, plain), MP4)


AES_BR_N = 4          # byte-range combo uses a small segment count (pure-Python AES)
                      # byterange 组合用较少分片数，因为纯 Python AES 很慢


def _aes_br_parts() -> tuple[bytes, list[int]]:
    """Whole-file ciphertext plus the per-segment ciphertext lengths.
    整份密文 + 每个分片各自的密文长度。

    Uses a reduced segment count (AES_BR_N) and short 1s segments: pure-Python
    AES over the full 10x6s stream would dominate the verify run time, and the
    property under test (ranges index ciphertext) does not depend on volume.

    使用较少的分片数（AES_BR_N）和 1 秒短片：若对完整的 10×6s 流做纯 Python AES，
    会占掉 verify 绝大部分耗时；而被测属性（区间索引的是密文）与数据量无关。
    """
    def build() -> bytes:
        return b"".join(crypto.aes128_cbc_encrypt(
            keys.AES128_KEY, IV, MEDIA.segment("br", i, duration=1.0))
            for i in range(AES_BR_N))

    def lens() -> list[int]:
        return [len(crypto.aes128_cbc_encrypt(
            keys.AES128_KEY, IV, MEDIA.segment("br", i, duration=1.0)))
            for i in range(AES_BR_N)]

    return big_file("aes_br_blob", build), big_file("aes_br_lens", lens)


def h_aes_byterange(req: Req) -> Resp:
    """列表里的区间长度来自"各段独立加密后的密文长度"（不是明文长度）。"""
    _, lens = _aes_br_parts()
    return m3u8(playlists.aes_byterange(req.base, lens, duration=1.0))


def h_aes_byterange_big(req: Req) -> Resp:
    """整份密文容器；用 rmode="ok" 表示正确支持 Range。"""
    blob, _ = _aes_br_parts()
    return Resp(200, blob, BIN, rmode="ok")


def h_aes_disc(req: Req) -> Resp:
    return m3u8(playlists.aes_disc(req.base))


def h_aes_disc_key(req: Req, which: str) -> Resp:
    """a / b 两个密钥端点，分别在 DISCONTINUITY 前后使用。"""
    if which == "a":
        return key_resp(keys.ROT_KEYS[0])
    if which == "b":
        return key_resp(keys.ROT_KEYS[1])
    raise Fail(404, "unknown key id; try a or b\n")


def h_aes_disc_seg(req: Req, clip: str, i: str) -> Resp:
    n = int(i)
    if clip == "sega":
        return ts(enc_segment("disc-a", n, keys.ROT_KEYS[0], IV))
    if clip == "segb":
        # Clip B is a separately-encoded source: its PTS restarts near 0, which
        # is exactly the discontinuity a client must be told about (and it is,
        # via EXT-X-DISCONTINUITY). Also encrypted with a different key.
        # Clip B 是另一份独立编码的源：其 PTS 大致从 0 重新开始，这正是必须
        # 通过 EXT-X-DISCONTINUITY 告知客户端的不连续点；而且用的是另一个密钥。
        return ts(enc_segment("disc-b", n, keys.ROT_KEYS[1], IV,
                              pts_start=9000))
    raise Fail(404, "unknown clip; try sega or segb\n")


# --- 20. variable EXTINF durations ---- 变长时长 + TARGETDURATION 不符 ------
#
# consistent：各片时长不等，但 TARGETDURATION 取最大值（合规）。
# mismatch  ：TARGETDURATION 小于最长分片（违规），考验客户端容错。

def h_variable(req: Req, mode: str) -> Resp:
    if mode not in ("consistent", "mismatch"):
        raise Fail(404, "unknown variant; try consistent or mismatch\n")
    return m3u8(playlists.variable_vod(req.base,
                                       consistent=(mode == "consistent")))


def h_variable_seg(req: Req, mode: str, i: str) -> Resp:
    if mode not in ("consistent", "mismatch"):
        raise Fail(404, "unknown variant; try consistent or mismatch\n")
    return ts(MEDIA.segment("var", int(i)))


# --- 19. multiple audio tracks + subtitles ---- 多音轨/字幕/隐藏字幕 --------
#
# 路由捕获组 -> handler 形参对应关系（易错点）：
#   /hls/renditions/audio/(\w+)/playlist.m3u8   -> 1 组 -> (req, group)
#   /hls/renditions/audio/(\w+)/seg/(\d+).ts    -> 2 组 -> (req, group, i)
#   /hls/renditions/subs/(\w+)/(\d+).vtt        -> 2 组 -> (req, lang, i)

RENDITION_LANGS = {"en", "zh"}
VTT = "text/vtt"


def h_renditions_master(req: Req) -> Resp:
    """多音轨/字幕 master：含视频变体、AUDIO 组、SUBTITLES 组、CLOSED-CAPTIONS。"""
    return m3u8(playlists.renditions_master(req.base))


def h_renditions_video(req: Req, res: str) -> Resp:
    """视频变体列表；res 只允许 360p / 720p。"""
    if res not in ("360p", "720p"):
        raise Fail(404, "unknown resolution; try 360p or 720p\n")
    return m3u8(playlists.media_playlist(
        [f"seg/{i}.ts" for i in range(playlists.N_SEG)]))


def h_renditions_video_seg(req: Req, res: str, i: str) -> Resp:
    # 纯视频 rendition：流里没有音频 PID。
    return ts(MEDIA.segment("vid", int(i), streams="v"))


def h_renditions_audio(req: Req, group: str) -> Resp:
    """音频组列表；group 只允许 en / zh / comm。"""
    if group not in ("en", "zh", "comm"):
        raise Fail(404, "unknown audio group\n")
    return m3u8(playlists.media_playlist(
        [f"seg/{i}.ts" for i in range(playlists.N_SEG)]))


def h_renditions_audio_seg(req: Req, group: str, i: str) -> Resp:
    """纯音频 rendition 的分片：streams="a"，无视频 PID。"""
    if group not in ("en", "zh", "comm"):
        raise Fail(404, "unknown audio group\n")
    return ts(MEDIA.segment("aud", int(i), streams="a"))


def h_renditions_subs_playlist(req: Req, lang: str) -> Resp:
    """字幕（WebVTT）列表：分片是 .vtt 而非 .ts。"""
    if lang not in RENDITION_LANGS:
        raise Fail(404, "unknown subtitle language; try en or zh\n")
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(playlists.SEG_DUR)}",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(playlists.N_SEG):
        lines.append(f"#EXTINF:{playlists.SEG_DUR:.3f},")
        lines.append(f"{i}.vtt")
    lines.append("#EXT-X-ENDLIST")
    return Resp(200, "\n".join(lines) + "\n", M3U8)


def h_renditions_subs_seg(req: Req, lang: str, i: str) -> Resp:
    """单个 WebVTT 字幕分片（含 X-TIMESTAMP-MAP）。"""
    if lang not in RENDITION_LANGS:
        raise Fail(404, "unknown subtitle language; try en or zh\n")
    return Resp(200, playlists.webvtt(lang, int(i)), VTT)


# --- EXT-X-DEFINE ---- 变量替换场景 ------------------------------------------

def h_define(req: Req) -> Resp:
    return m3u8(playlists.define(req.base))


def h_define_seg(req: Req, i: str) -> Resp:
    return ts(MEDIA.segment("define", int(i)))


# --- 17. malformed ---- 各种不规范 m3u8（CRLF/BOM/大小写/未知标签/缺属性） --

def h_malformed(req: Req, kind: str) -> Resp:
    """畸形列表必须以 bytes 返回，才能精确保留 BOM / CRLF。"""
    if kind not in MALFORMED_KINDS:
        raise Fail(404, f"unknown variant; try one of: {', '.join(MALFORMED_KINDS)}\n")
    return Resp(200, playlists.malformed(kind), M3U8)


def h_malformed_seg(req: Req, kind: str, i: str) -> Resp:
    # 分片本身正常 —— 本场景考的是播放列表解析，不是分片。
    return ts(MEDIA.segment("vod", int(i)))


# ---------------------------------------------------------------------------
# Routing table / 路由表
# ---------------------------------------------------------------------------
#
# 每条路由是 (method, compiled_regex, handler)。路径已由 _route() 做过
# percent-decode，所以正则匹配的是解码后的路径。正则中的捕获组会**按顺序**
# 作为位置参数传给 handler（除 req 之外），因此组的数量必须与 handler 形参一致
# —— 这是最容易踩的坑（多一个组或少一个组都会 500）。

ROUTES: list[tuple[str, re.Pattern, object]] = []


def _r(method: str, pattern: str, fn) -> None:
    """注册一条路由（pattern 是正则字符串，编译后加入 ROUTES）。"""
    ROUTES.append((method, re.compile(pattern), fn))


_r("GET", r"^/$", h_index)
_r("GET", r"^/scenarios\.json$", h_scenarios)
_r("GET", r"^/preview/([\w-]+)$", h_preview)
_r("GET", r"^/cache\.json$", h_cache_json)
_r("POST", r"^/cache/clear$", h_cache_clear)
_r("GET", r"^/sample\.json$", h_sample_json)
_r("POST", r"^/sample/rebuild$", h_sample_rebuild)
# 6
_r("GET", r"^/hls/vod/playlist\.m3u8$", h_vod)
_r("GET", r"^/hls/vod/seg/(\d+)\.ts$", h_vod_seg)
_r("GET", r"^/hls/live/playlist\.m3u8$", h_live)
_r("GET", r"^/hls/live/seg/(\d+)\.ts$", h_live_seg)
_r("GET", r"^/hls/live/event\.m3u8$", h_live_event)
_r("GET", r"^/hls/live/event/seg/(\d+)\.ts$", h_live_event_seg)
# 1
_r("GET", r"^/hls/aes128/playlist\.m3u8$", h_aes128)
_r("GET", r"^/hls/aes128/msn-iv/playlist\.m3u8$", h_aes128_msn)
_r("GET", r"^/hls/aes128/(360p|720p)\.m3u8$", h_aes128_variant)
_r("GET", r"^/hls/aes128/master\.m3u8$", h_aes128_master)
_r("GET", r"^/hls/aes128/(?:msn-iv/)?key\.bin$", h_aes128_key)
_r("GET", r"^/hls/aes128/seg/(\d+)\.ts$", h_aes128_seg)
_r("GET", r"^/hls/aes128/msn-iv/seg/(\d+)\.ts$", h_aes128_msn_seg)
# 2
_r("GET", r"^/hls/key-expiry/playlist\.m3u8$", h_key_expiry)
_r("GET", r"^/hls/key-expiry/key\.bin$", h_key_expiry_key)
_r("GET", r"^/hls/key-expiry/seg/(\d+)\.ts$", h_key_expiry_seg)
# 3
_r("GET", r"^/hls/sample-aes/playlist\.m3u8$", h_sample_aes)
_r("GET", r"^/hls/sample-aes/key\.bin$", h_sample_aes_key)
_r("GET", r"^/hls/sample-aes/seg/(\d+)\.ts$", h_sample_aes_seg)
_r("GET", r"^/hls/fairplay/playlist\.m3u8$", h_fairplay)
_r("GET", r"^/hls/fairplay/seg/(\d+)\.ts$", h_fairplay_seg)
_r("GET", r"^/hls/widevine/playlist\.m3u8$", h_widevine)
_r("GET", r"^/hls/widevine/seg/(\d+)\.ts$", h_widevine_seg)
# 4
_r("GET", r"^/hls/key-header/playlist\.m3u8$", h_key_header)
_r("GET", r"^/hls/key-header/key\.bin$", h_key_header_key)
_r("GET", r"^/hls/key-header/seg/(\d+)\.ts$", h_key_header_seg)
# 15
_r("GET", r"^/hls/key-rotation/playlist\.m3u8$", h_key_rotation)
_r("GET", r"^/hls/key-rotation/key/(\d+)\.bin$", h_key_rotation_key)
_r("GET", r"^/hls/key-rotation/seg/(\d+)\.ts$", h_key_rotation_seg)
# 7
_r("GET", r"^/hls/guard/playlist\.m3u8$", h_guard)
_r("GET", r"^/hls/guard/seg/(\d+)\.ts$", h_guard_seg)
# 8
_r("GET", r"^/hls/sign/playlist\.m3u8$", h_sign)
_r("GET", r"^/hls/sign/seg/(\d+)\.ts$", h_sign_seg)
# 10
_r("GET", r"^/hls/session/start$", h_session_start)
_r("GET", r"^/hls/session/playlist\.m3u8$", h_session)
_r("GET", r"^/hls/session/seg/(\d+)\.ts$", h_session_seg)
# 9
_r("GET", r"^/hls/ratelimit/playlist\.m3u8$", h_ratelimit)
_r("GET", r"^/hls/ratelimit/seg/(\d+)\.ts$", h_ratelimit_seg)
_r("GET", r"^/hls/throttle/playlist\.m3u8$", h_throttle)
_r("GET", r"^/hls/throttle/seg/(\d+)\.ts$", h_throttle_seg)
_r("GET", r"^/hls/ban/playlist\.m3u8$", h_ban)
_r("GET", r"^/hls/ban/seg/(\d+)\.ts$", h_ban_seg)
_r("GET", r"^/hls/captcha/playlist\.m3u8$", h_captcha)
_r("GET", r"^/hls/captcha/seg/(\d+)\.ts$", h_captcha_seg)
_r("GET", r"^/hls/captcha/solve$", h_captcha_solve)
# 11
_r("GET", r"^/hls/broken/playlist\.m3u8$", h_broken)
_r("GET", r"^/hls/broken/seg/(\d+)\.ts$", h_broken_seg)
_r("GET", r"^/hls/broken-enc/playlist\.m3u8$", h_broken_enc)
_r("GET", r"^/hls/broken-enc/seg/(\d+)\.ts$", h_broken_enc_seg)
_r("GET", r"^/hls/gap/playlist\.m3u8$", h_gap)
_r("GET", r"^/hls/gap/seg/(\d+)\.ts$", h_gap_seg)
# 14
_r("GET", r"^/hls/discontinuity/playlist\.m3u8$", h_discontinuity)
_r("GET", r"^/hls/discontinuity/seg/(\d+)\.ts$", h_disc_seg)
_r("GET", r"^/hls/discontinuity/clipb/(\d+)\.ts$", h_disc_clipb)
# 15
_r("GET", r"^/hls/onetime/playlist\.m3u8$", h_onetime)
_r("GET", r"^/hls/onetime/seg/(\d+)\.ts$", h_onetime_seg)
# 12
_r("GET", r"^/hls/av-separate/master\.m3u8$", h_av_master)
_r("GET", r"^/hls/av-separate/video/\d+p\.m3u8$", h_av_video_playlist)
_r("GET", r"^/hls/av-separate/video/seg/(\d+)\.ts$", h_av_video_seg)
_r("GET", r"^/hls/av-separate/audio/playlist\.m3u8$", h_av_audio_playlist)
_r("GET", r"^/hls/av-separate/audio/seg/(\d+)\.ts$", h_av_audio_seg)
# 13
_r("GET", r"^/hls/fmp4/playlist\.m3u8$", h_fmp4)
_r("GET", r"^/hls/fmp4/init\.mp4$", h_fmp4_init)
_r("GET", r"^/hls/fmp4/seg/(\d+)\.m4s$", h_fmp4_seg)
_r("GET", r"^/hls/fmp4/byterange/playlist\.m3u8$", h_fmp4_byterange)
_r("GET", r"^/hls/fmp4/byterange/combined\.m4s$", h_fmp4_combined)
_r("GET", r"^/hls/fmp4/av/playlist\.m3u8$", h_fmp4_av)
_r("GET", r"^/hls/fmp4/av/init\.mp4$", h_fmp4_av_init)
_r("GET", r"^/hls/fmp4/av/seg/(\d+)\.m4s$", h_fmp4_av_seg)
# 5
_r("GET", r"^/hls/byterange/playlist\.m3u8$", h_byterange)
_r("GET", r"^/hls/byterange/big\.ts$", h_byterange_big)
_r("GET", r"^/hls/range/([\w-]+)/playlist\.m3u8$", h_range)
_r("GET", r"^/hls/range/([\w-]+)/seg/(\d+)\.ts$", h_range_seg)

_r("GET", r"^/hls/paths/master\.m3u8$", h_paths_master)
_r("GET", r"^/hls/paths/redir\.m3u8$", h_paths_redir)
_r("GET", r"^/hls/paths/(.+)/(\d{3}p)\.m3u8$", h_paths_media)
_r("GET", r"^/hls/paths/(?:.*/)?seg/(\d+)\.ts$", h_paths_seg)
_r("GET", r"^/hls/paths/(?:.*/)?seg-redir/(\d+)\.ts$", h_paths_seg_redir)
# 18
_r("GET", r"^/hls/nested/master\.m3u8$", h_nested_master)
_r("GET", r"^/hls/nested/(v1|v2)/sub\.m3u8$", h_nested_sub)
_r("GET", r"^/hls/nested/(v1|v2|shared)/(\d{3}p)\.m3u8$", h_nested_media)
_r("GET", r"^/hls/nested/(v1|v2|shared)/seg/(\d+)\.ts$", h_nested_seg)
# 23
_r("GET", r"^/hls/resume-triple/playlist\.m3u8$", h_resume_triple)
_r("GET", r"^/hls/resume-triple/key\.bin$", h_resume_triple_key)
_r("GET", r"^/hls/resume-triple/seg/(\d+)\.ts$", h_resume_triple_seg)
# 24
_r("GET", r"^/hls/series/master\.m3u8$", h_series_master)
_r("GET", r"^/hls/series/key\.bin$", h_series_key)
_r("GET", r"^/hls/series/ep/(\d+)/playlist\.m3u8$", h_series_ep)
_r("GET", r"^/hls/series/ep/(\d+)/seg/(\d+)\.ts$", h_series_seg)
# 22
_r("GET", r"^/hls/ads/playlist\.m3u8$", h_ads)
_r("GET", r"^/hls/ads/seg/(\d+)\.ts$", h_ads_seg)
_r("GET", r"^/hls/ads/ad/(\d+)\.ts$", h_ads_ad)
# 21
_r("GET", r"^/hls/aes-fmp4/playlist\.m3u8$", h_aes_fmp4)
_r("GET", r"^/hls/aes-fmp4/init\.mp4$", h_aes_fmp4_init)
_r("GET", r"^/hls/aes-fmp4/key\.bin$", h_aes128_key)
_r("GET", r"^/hls/aes-fmp4/seg/(\d+)\.m4s$", h_aes_fmp4_seg)
_r("GET", r"^/hls/aes-byterange/playlist\.m3u8$", h_aes_byterange)
_r("GET", r"^/hls/aes-byterange/big\.enc$", h_aes_byterange_big)
_r("GET", r"^/hls/aes-byterange/key\.bin$", h_aes128_key)
_r("GET", r"^/hls/aes-disc/playlist\.m3u8$", h_aes_disc)
_r("GET", r"^/hls/aes-disc/key/(a|b)\.bin$", h_aes_disc_key)
_r("GET", r"^/hls/aes-disc/(sega|segb)/(\d+)\.ts$", h_aes_disc_seg)
# 20
_r("GET", r"^/hls/variable/(consistent|mismatch)/playlist\.m3u8$", h_variable)
_r("GET", r"^/hls/variable/(consistent|mismatch)/seg/(\d+)\.ts$", h_variable_seg)
# 19
_r("GET", r"^/hls/renditions/master\.m3u8$", h_renditions_master)
_r("GET", r"^/hls/renditions/video/(\d{3}p)\.m3u8$", h_renditions_video)
_r("GET", r"^/hls/renditions/video/(\d{3}p)/seg/(\d+)\.ts$", h_renditions_video_seg)
_r("GET", r"^/hls/renditions/audio/(\w+)/playlist\.m3u8$", h_renditions_audio)
_r("GET", r"^/hls/renditions/audio/(\w+)/seg/(\d+)\.ts$", h_renditions_audio_seg)
_r("GET", r"^/hls/renditions/subs/(\w+)/playlist\.m3u8$", h_renditions_subs_playlist)
_r("GET", r"^/hls/renditions/subs/(\w+)/(\d+)\.vtt$", h_renditions_subs_seg)
# define
_r("GET", r"^/hls/define/playlist\.m3u8$", h_define)
_r("GET", r"^/hls/define/segments/seg_(\d+)\.ts$", h_define_seg)
# 17
_r("GET", r"^/hls/malformed/([\w-]+)/playlist\.m3u8$", h_malformed)
_r("GET", r"^/hls/malformed/([\w-]+)/seg/(\d+)\.ts$", h_malformed_seg)


# ---------------------------------------------------------------------------
# HTTP handler / HTTP 处理器
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HLS-Test/1.0"
    protocol_version = "HTTP/1.1"   # 保持连接，便于客户端复用 TCP

    def log_message(self, fmt, *args):  # compact one-line access log
        if QUIET:
            return
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def do_OPTIONS(self):
        """OPTIONS：返回 CORS 预检所需头（浏览器播放器会发）。"""
        self.send_response(204)
        self._cors()
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors(self):
        """放开 CORS，方便浏览器里的 hls.js 等客户端直接访问。"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")

    def _route(self, method: str) -> None:
        raw = self.path
        # RFC 7230 absolute-form request target (e.g. curl following a
        # redirect on a kept-alive connection): strip scheme + authority.
        # RFC 7230 的 absolute-form 请求目标（如 curl 在复用连接上跟随重定向）：
        # 去掉 scheme 与 authority，只留 path?query。
        if raw.startswith("http://") or raw.startswith("https://"):
            sp = urllib.parse.urlsplit(raw)
            raw = sp.path or "/"
            if sp.query:
                raw += "?" + sp.query
        path, _, qstr = raw.partition("?")
        path = urllib.parse.unquote(path)   # 百分号解码后再交给路由正则
        query = {k: v[-1] for k, v in
                 urllib.parse.parse_qs(qstr, keep_blank_values=True).items()}
        req = Req(self, path, query)
        # RFC 7231: HEAD must be answered exactly like GET minus the body,
        # so it matches the GET routes (downloaders probe sizes with it).
        # RFC 7231：HEAD 必须与 GET 同路由、只是不带响应体
        # （下载器常用它探测文件大小）。
        match_method = "GET" if method == "HEAD" else method
        for m, rx, fn in ROUTES:
            if m != match_method:
                continue
            match = rx.match(path)
            if match:
                try:
                    # 正则捕获组按顺序作为位置参数传给 handler。
                    resp = fn(req, *match.groups())
                except Fail as f:
                    # handler 主动抛出的错误响应。
                    resp = Resp(f.status, f.body, f.ctype, headers=f.headers)
                except (KeyboardInterrupt, SystemExit):
                    # 进程级信号（如环境删除护栏抛的 SystemExit）：若放任它穿透
                    # BaseHTTPRequestHandler，socket 会被直接掐断，客户端只看到
                    # HTTP 000。这里转成一个明确的 503，至少让响应写出去。
                    traceback.print_exc()
                    resp = Resp(503, "aborted by environment control\n")
                except Exception as e:
                    # 未预期的异常：打日志并回 500，便于排查场景实现 bug。
                    traceback.print_exc()
                    resp = Resp(500, f"internal error: {e}\n")
                self._send(resp, method)
                return
        self._send(Resp(404, f"not found: {path}\n"), method)

    def _send(self, resp: Resp, method: str) -> None:
        """统一发送响应：CORS 头、Range 处理、限速下发、HEAD 省略响应体。"""
        try:
            status, body = resp.status, resp.body
            headers = list(resp.headers)
            if resp.rmode:
                # 由 rmode 决定的区间请求行为（206/416/200 等），见 _range_response。
                status, body, extra = _range_response(
                    resp.rmode, self.headers.get("Range"), resp.body,
                    self.path, STATE)
                headers += extra
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", resp.ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method == "HEAD":
                return   # HEAD 到此为止，不写 body
            if resp.kbps:
                # 按 kbps 分块下发，块间 sleep —— 用于 throttle 场景。
                chunk = 16 * 1024
                for off in range(0, len(body), chunk):
                    self.wfile.write(body[off: off + chunk])
                    time.sleep(chunk * 8 / (resp.kbps * 1024))
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（下载器取消），属正常情况，静默忽略。
            pass


def make_server(bind: str, port: int) -> ThreadingHTTPServer:
    """创建一个多线程 HTTP 服务器（未启动；调用方自行 serve_forever）。"""
    return ThreadingHTTPServer((bind, port), Handler)
