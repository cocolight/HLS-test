"""Playlist builders — pure functions producing m3u8 text.
播放列表构造器——纯函数，只产出 m3u8 文本。

Each builder returns ``str`` (playlists) or ``bytes`` (malformed variants that
need exotic encodings). ``base`` is the absolute origin, e.g.
``http://127.0.0.1:8080``, used wherever absolute or protocol-relative URIs
are part of the test.

每个构造器返回 ``str``（普通播放列表）或 ``bytes``（需要特殊编码的畸形变体）。
``base`` 是绝对源站（如 ``http://127.0.0.1:8080``），仅在测试涉及绝对 URI 或
协议相对 URI 时才会被用到。

约定：所有 builder 的第一个参数统一是 ``base``，即使某些场景用不到——
这样 app.py 里的 handler 可以无条件地把它传进去，保持接口一致。
"""

from __future__ import annotations

import time

from . import keys

N_SEG = 10            # 标准 VOD 长度：10 片 × 6s
SEG_DUR = 6.0


def _head(version: int = 3, target: int = 6, msn: int | None = None,
          independent: bool = False) -> list[str]:
    """构造播放列表公共头部（EXTM3U / VERSION / TARGETDURATION / …）。

    msn 不为 None 才写 EXT-X-MEDIA-SEQUENCE（VOD 通常不需要）。
    """
    lines = ["#EXTM3U"]
    if version is not None:
        lines.append(f"#EXT-X-VERSION:{version}")
    lines.append(f"#EXT-X-TARGETDURATION:{target}")
    if msn is not None:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{msn}")
    if independent:
        lines.append("#EXT-X-INDEPENDENT-SEGMENTS")
    return lines


def media_playlist(uris: list[str], duration: float | list[float] = SEG_DUR,
                   version: int = 3, msn: int | None = None,
                   endlist: bool = True, extra_head: list[str] | None = None,
                   prelude: list[str] | None = None,
                   target: int | None = None) -> str:
    """Generic media playlist: every URI gets one #EXTINF.
    通用 media 播放列表：每个 URI 前挂一条 #EXTINF。

    ``duration`` may be a scalar (repeated for every segment) or a per-segment
    list. ``target`` optionally overrides #EXT-X-TARGETDURATION; when omitted
    the historical default (6) is kept for backward compatibility.

    ``duration`` 可以是标量（对所有分片重复）或逐片列表。
    ``target`` 可覆盖 TARGETDURATION；为 None 时沿用历史默认 6，保证向后兼容。
    ``extra_head`` 追加在头部之后（放 EXT-X-KEY / EXT-X-MAP 等）；
    ``prelude(i)`` 在每个 #EXTINF 之前插入逐片标签（如逐片轮换的 KEY）。
    """
    durations = (duration if isinstance(duration, (list, tuple))
                 else [duration] * len(uris))
    if len(durations) != len(uris):
        raise ValueError("duration list length must match uri count")
    lines = _head(version=version, msn=msn,
                  target=target if target is not None else 6)
    if extra_head:
        lines += extra_head
    for i, uri in enumerate(uris):
        if prelude:
            lines += prelude(i)
        lines.append(f"#EXTINF:{durations[i]:.3f},")
        lines.append(uri)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 6. VOD / live
# ---------------------------------------------------------------------------

def vod(base: str, prefix: str = "vod", n: int = N_SEG) -> str:
    """最普通的 VOD：n 片，全部带 #EXT-X-ENDLIST。"""
    return media_playlist([f"seg/{i}.ts" for i in range(n)])


def live(base: str, start: float, window: int = 6,
         now: float | None = None) -> str:
    """直播滑窗：只暴露最近 window 片，无 ENDLIST，MEDIA-SEQUENCE 递增。

    start 是这次"直播"的起始时间戳；用 now 做可注入的时间基准（便于测试）。
    """
    now = now if now is not None else time.time()
    done = max(1, int((now - start) / SEG_DUR))  # 已完成（已生成）的分片数
    first = max(0, done - window)                # 滑窗左边界
    lines = _head(version=3, msn=first)
    for msn in range(first, done):
        pdt = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start + msn * SEG_DUR))
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}")
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{msn}.ts")
    return "\n".join(lines) + "\n"


def live_event(base: str, start: float, cap: int = 12,
               now: float | None = None) -> str:
    """Event playlist: grows over time, becomes VOD after `cap` segments.
    Event 列表：随时间长高，累计到 cap 片后追加 ENDLIST 转为 VOD。"""
    now = now if now is not None else time.time()
    done = min(int((now - start) / SEG_DUR), cap)
    lines = _head(version=3, msn=0)
    for msn in range(done):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"event/seg/{msn}.ts")
    if done >= cap:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 1. AES-128
# ---------------------------------------------------------------------------

def aes128(base: str, msn_iv: bool = False, n: int = N_SEG) -> str:
    """标准 AES-128：显式 IV；msn_iv=True 时不写 IV（客户端须用媒体序列号当 IV）。"""
    # 标准场景用显式非零 IV；MSN-IV 场景省略属性
    # （此时客户端必须把媒体序列号当作 IV）。
    iv = "" if msn_iv else ",IV=0x000102030405060708090a0b0c0d0e0f"
    key_line = f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"{iv}'
    return media_playlist([f"seg/{i}.ts" for i in range(n)],
                          version=2, extra_head=[key_line])


def aes128_master(base: str) -> str:
    """master 里同时声明 EXT-X-SESSION-KEY（会话级密钥提示）。"""
    key_uri = f"{base}/hls/aes128/key.bin"
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f'#EXT-X-SESSION-KEY:METHOD=AES-128,URI="{key_uri}",IV=0x000102030405060708090a0b0c0d0e0f',
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2"',
        "360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"',
        "720p.m3u8",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 2. key expiry / 4. key header / 15. rotation
# ---------------------------------------------------------------------------

def key_expiry(base: str, exp: int, sig: str, n: int = N_SEG) -> str:
    """密钥 URL 带 exp / sig：过期后 key 端点应返回 403。"""
    key_line = (f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin?exp={exp}&s={sig}",'
                'IV=0x000102030405060708090a0b0c0d0e0f')
    return media_playlist([f"seg/{i}.ts" for i in range(n)], extra_head=[key_line])


def key_header(base: str, n: int = N_SEG) -> str:
    """密钥端点要求自定义请求头 X-Api-Key（播放列表本身看不出区别）。"""
    key_line = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f'
    return media_playlist([f"seg/{i}.ts" for i in range(n)], extra_head=[key_line])


def key_rotation(base: str, n: int = N_SEG) -> str:
    """A new EXT-X-KEY line before every segment.
    每个分片前都插一条新的 EXT-X-KEY（密钥逐片轮换）。"""
    uris = [f"seg/{i}.ts" for i in range(n)]

    def prelude(i: int) -> list[str]:
        return [f'#EXT-X-KEY:METHOD=AES-128,URI="key/{i}.bin",'
                'IV=0x000102030405060708090a0b0c0d0e0f']

    return media_playlist(uris, extra_head=None, prelude=prelude)


# ---------------------------------------------------------------------------
# 3. SAMPLE-AES / DRM
# ---------------------------------------------------------------------------

def sample_aes(base: str, n: int = N_SEG) -> str:
    """SAMPLE-AES（identity）：合法语法但下载器应拒绝解密。"""
    key_line = ('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin",'
                'IV=0x000102030405060708090a0b0c0d0e0f,'
                'KEYFORMAT="identity",KEYFORMATVERSIONS="1"')
    return media_playlist([f"seg/{i}.ts" for i in range(n)], version=5,
                          extra_head=[key_line])


def fairplay(base: str, n: int = N_SEG) -> str:
    """FairPlay：URI 用 skd:// 方案，属于 DRM 而非可直接取用的密钥。"""
    key_line = ('#EXT-X-KEY:METHOD=SAMPLE-AES,'
                'URI="skd://fairplay-test-stream",'
                'KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"')
    return media_playlist([f"seg/{i}.ts" for i in range(n)], version=5,
                          extra_head=[key_line])


def widevine(base: str, n: int = N_SEG) -> str:
    """Widevine：URI 是 base64 编码的 pssh box（data: URI），不是密钥。"""
    # pssh box (base64) as a data: URI -- classic Widevine signalling
    pssh_b64 = ("AAAAPnBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABQaCxQaHDILBQo="
                "BmlkZW50aXR5Zm9yZGVtbw==")
    key_line = ('#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,'
                f'URI="data:text/plain;base64,{pssh_b64}",'
                'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",'
                'KEYFORMATVERSIONS="1"')
    return media_playlist([f"seg/{i}.ts" for i in range(n)], version=5,
                          extra_head=[key_line])


# ---------------------------------------------------------------------------
# 7. Referer / UA / Cookie
# ---------------------------------------------------------------------------

def guard(base: str, auth: str = "all", n: int = N_SEG) -> str:
    """分片 URL 带 ?auth=…，服务端据此校验 Referer / UA / Cookie。"""
    uris = [f"seg/{i}.ts?auth={auth}" for i in range(n)]
    return media_playlist(uris)


# ---------------------------------------------------------------------------
# 8. signed URLs
# ---------------------------------------------------------------------------

def signed(base: str, exp: int, sigs: list[str], n: int = N_SEG) -> str:
    """分片 URL 带 HMAC 签名的 ?exp&s；每片签名不同。"""
    uris = [f"seg/{i}.ts?exp={exp}&s={sigs[i]}" for i in range(n)]
    return media_playlist(uris)


# ---------------------------------------------------------------------------
# 11. broken segments
# ---------------------------------------------------------------------------

def broken(base: str, n: int = N_SEG, encrypted: bool = False) -> str:
    """列表本身正常，但部分分片响应异常（404/500/乱码/截断）。"""
    extra = None
    if encrypted:
        extra = ['#EXT-X-KEY:METHOD=AES-128,URI="key.bin",'
                 'IV=0x000102030405060708090a0b0c0d0e0f']
    return media_playlist([f"seg/{i}.ts" for i in range(n)], extra_head=extra)


def gap(base: str, n: int = N_SEG) -> str:
    """Segments 3 and 4 are missing/unavailable, marked with EXT-X-GAP.
    第 3、4 片标记 EXT-X-GAP（不可用，客户端应跳过而非请求）。"""
    lines = _head()
    for i in range(n):
        if i in (3, 4):
            lines.append("#EXT-X-GAP")
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 14. discontinuity
# ---------------------------------------------------------------------------

def discontinuity(base: str, n_a: int = 5, n_b: int = 2) -> str:
    """PTS 跳变：clip A 之后用 EXT-X-DISCONTINUITY 切换到 PTS 重启的 clip B。"""
    lines = _head(version=3)
    for i in range(n_a):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.ts")
    lines.append("#EXT-X-DISCONTINUITY")
    for i in range(n_b):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"clipb/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 15. one-time URLs (hard resume)
# ---------------------------------------------------------------------------

def onetime(base: str, tokens: list[str], n: int = N_SEG) -> str:
    """每片 URL 带一次性 token；每个 URL 只能成功拉取一次。"""
    uris = [f"seg/{i}.ts?t={tokens[i]}" for i in range(n)]
    return media_playlist(uris)


# ---------------------------------------------------------------------------
# 12. audio/video separation
# ---------------------------------------------------------------------------

def av_separate_master(base: str) -> str:
    """音视频分离的 master：EXT-X-MEDIA 音频组 + 纯视频变体 + 纯音频变体。"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",'
        'DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",URI="audio/playlist.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=280000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"',
        "video/360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"',
        "video/720p.m3u8",
        # 纯音频变体：无 RESOLUTION，只有 CODECS 与 AUDIO 组引用
        '#EXT-X-STREAM-INF:BANDWIDTH=96000,CODECS="mp4a.40.2",AUDIO="aud"',
        "audio/playlist.m3u8",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 13. fMP4
# ---------------------------------------------------------------------------

def fmp4(base: str, n: int = N_SEG) -> str:
    """标准 fMP4：EXT-X-MAP 声明 init 段，媒体分片是 .m4s。"""
    lines = _head(version=7, independent=True)
    lines.append('#EXT-X-MAP:URI="init.mp4"')
    for i in range(n):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def fmp4_byterange(base: str, init_len: int, seg_lens: list[int]) -> str:
    """init 与媒体分片都是同一个 combined.m4s 的字节区间。"""
    lines = _head(version=7, independent=True)
    lines.append(f'#EXT-X-MAP:URI="combined.m4s",BYTERANGE="{init_len}@0"')
    off = init_len
    for i, ln in enumerate(seg_lens):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{ln}@{off}")
        lines.append("combined.m4s")
        off += ln
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def byterange_ts(base: str, seg_lens: list[int]) -> str:
    """所有 TS 分片都是同一个 big.ts 的字节区间。"""
    lines = _head(version=4)
    off = 0
    for i, ln in enumerate(seg_lens):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{ln}@{off}")
        lines.append("big.ts")
        off += ln
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 5. path & URI resolution
# ---------------------------------------------------------------------------

def paths_master(base: str) -> str:
    """master：变体 URI 故意混用各种形态（相对/点段/百分号/查询/绝对/协议相对）。"""
    host = base.split("://", 1)[1]
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "rel/360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "./dot/360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "a/b/../../up/360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "my%20dir/360p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "query/360p.m3u8?token=abc&cache=1",
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360',
        f"{base}/hls/paths/abs/720p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360',
        f"//{host}/hls/paths/proto/720p.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234',
        "redir.m3u8",   # 该变体播放列表会返回 302
    ]
    return "\n".join(lines) + "\n"


def paths_media(base: str, n: int = N_SEG) -> str:
    """Segment URIs themselves use mixed forms, incl. one redirecting URL.
    分片 URI 同样混用各种形态，其中一片走 302 重定向。"""
    host = base.split("://", 1)[1]
    uris = []
    for i in range(n):
        if i == 0:
            uris.append(f"seg/{i}.ts")
        elif i == 1:
            uris.append(f"{base}/hls/paths/seg/{i}.ts")
        elif i == 2:
            uris.append(f"//{host}/hls/paths/seg/{i}.ts")
        elif i == 3:
            uris.append(f"seg/{i}.ts?token=xyz&x=1")
        elif i == 4:
            uris.append(f"seg-redir/{i}.ts")
        elif i == 5:
            uris.append(f"../seg/{i}.ts")
        else:
            uris.append(f"seg/{i}.ts")
    return media_playlist(uris, version=4)


def nested_media(tier: str, n: int = N_SEG) -> str:
    """Media playlist for a nested tier; segments resolve against the tier dir.
    嵌套某一层的 media 列表；seg/ URI 相对该层目录解析。"""
    if tier not in NESTED_TIERS:
        raise ValueError(f"unknown nested tier: {tier}")
    return media_playlist([f"seg/{i}.ts" for i in range(n)], version=4)


# ---------------------------------------------------------------------------
# 18. multi-level nested master (master -> sub-master -> media)
#   多层嵌套 master（master -> sub-master -> media）
# ---------------------------------------------------------------------------
#
# Layout (all paths relative to /hls/nested/):
# 布局（所有路径都相对 /hls/nested/）：
#
#   master.m3u8          -> "v1/sub.m3u8"        (plain relative 纯相对)
#                        -> "./v2/sub.m3u8"      (dot segment 点段)
#   v1/sub.m3u8          -> "360p.m3u8"
#                        -> "../shared/720p.m3u8"  (climbs one level 上跳一级)
#   v2/sub.m3u8          -> "a/../360p.m3u8"      (inner normalisation 内部规范化)
#   v1/360p.m3u8         -> media, "seg/{i}.ts"   (relative to v1/ 相对 v1/)
#   v2/360p.m3u8         -> media, "seg/{i}.ts"   (relative to v2/ 相对 v2/)
#   shared/720p.m3u8     -> media, "seg/{i}.ts"   (relative to shared/)
#
# The anchor trap: a client that keeps the *first* base URL (the top-level
# master) will resolve "../shared/720p.m3u8" to "/hls/shared/720p.m3u8" and
# 404. The correct base is the sub-master that contained the URI.
#
# 陷阱所在：相对 URI 必须相对"包含它的那份文档"解析。若客户端一直沿用顶层
# master 的 base，会把 "../shared/720p.m3u8" 解析成 /hls/shared/720p.m3u8 而 404。
# 正确做法是把 base 更新为当前 sub-master 所在目录。

NESTED_TIERS = {
    "v1": {"dir": "v1", "res": "360p", "bandwidth": 300000, "resolution": "416x234"},
    "v2": {"dir": "v2", "res": "360p", "bandwidth": 320000, "resolution": "416x234"},
    "shared": {"dir": "shared", "res": "720p", "bandwidth": 800000, "resolution": "640x360"},
}


def nested_master(base: str) -> str:
    """Top-level master pointing at two sub-masters via different URI forms.
    顶层 master：用两种不同 URI 形态指向两个 sub-master。"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2"',
        "v1/sub.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=320000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2"',
        "./v2/sub.m3u8",
    ]
    return "\n".join(lines) + "\n"


def nested_sub(base: str, tier: str) -> str:
    """Second-level master. `tier` is one of NESTED_TIERS.
    第二层 master。`tier` 取 NESTED_TIERS 中的键。

    v1 内混用纯相对与父级（../）URI；v2 用带点段的 URI 考验内部规范化。
    """
    if tier == "v1":
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2"',
            "360p.m3u8",
            '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"',
            "../shared/720p.m3u8",
        ]
    elif tier == "v2":
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            '#EXT-X-STREAM-INF:BANDWIDTH=320000,RESOLUTION=416x234,CODECS="avc1.64001e,mp4a.40.2"',
            "a/../360p.m3u8",
        ]
    else:
        raise ValueError(f"unknown nested tier: {tier}")
    return "\n".join(lines) + "\n"


def nested_tier(tier: str) -> dict:
    """Metadata for one nested tier (used by handlers and the catalog).
    某一嵌套层的元数据（handler 与目录页共用）。"""
    if tier not in NESTED_TIERS:
        raise ValueError(f"unknown nested tier: {tier}")
    return NESTED_TIERS[tier]


# ---------------------------------------------------------------------------
# 23. resume triple: one-time URLs + expiring key + one-shot Range
#     续传三重叠加：一次性 URL + 限时密钥 + 一次性 Range
# ---------------------------------------------------------------------------
#
# All three "you cannot just continue where you left off" mechanisms at once.
# A correct downloader must detect that resuming is impossible and re-download
# the whole asset; a naive one splices a corrupt file.
#
# 三种"无法接着上次继续"的机制同时出现。正确的下载器应识别出无法续传、
# 重新整段下载；天真的实现会拼出一个损坏的文件。

def resume_triple(base: str, tokens: list[str], exp: int, sig: str,
                  n: int = N_SEG) -> str:
    key_line = (f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin?exp={exp}&s={sig}",'
                'IV=0x000102030405060708090a0b0c0d0e0f')
    uris = [f"seg/{i}.ts?t={tokens[i]}" for i in range(n)]
    return media_playlist(uris, extra_head=[key_line])


# ---------------------------------------------------------------------------
# 22. ad signalling (DATERANGE / CUE-OUT / GAP)
#     广告信令（DATERANGE / CUE-OUT / GAP）
# ---------------------------------------------------------------------------
#
# Three mechanisms that can mark an ad break, all present at once so a client's
# handling of each can be observed independently:
# 三种可标记广告区间的手段同时出现，便于分别观察客户端对每种的处理：
#   * EXT-X-DATERANGE with SCTE35-OUT / SCTE35-IN  -- the attribute values are
#     HEX-ENCODED BINARY splice_info_section() payloads, NOT base64. There is
#     no "SCTE-35=" attribute name (a common mistake).
#     SCTE35-OUT / SCTE35-IN 的值是十六进制编码的二进制 splice_info_section()，
#     不是 base64；也不存在名为 "SCTE-35=" 的属性（这是常见误解）。
#   * EXT-X-CUE-OUT / EXT-X-CUE-IN  -- the legacy marker pair.
#     旧式的 CUE-OUT / CUE-IN 标记对。
#   * EXT-X-GAP on the ad segments themselves.
#     广告分片自身标 EXT-X-GAP。
#
# Timeline: 3 content segments, 2 ad segments (a break), then 3 content.
# 时间轴：3 个正片分片 -> 2 个广告分片（一个 break）-> 3 个正片分片。

_AD_BREAK_FIRST = 3   # 第 3 片之后插广告
_AD_BREAK_LEN = 2     # 广告占 2 片


def _splice_hex() -> str:
    """A minimal splice_info_section(): table_id 0xFC, short section.
    最小可用的 splice_info_section()：table_id 0xFC，短段格式。"""
    # table_id(0xFC) + section_syntax/flags + length + protocol_version +
    # encrypted_packet/flags + pts_adjustment(5) + cw_index + tier + splices
    body = bytes([
        0xFC, 0x30, 0x11,        # table_id, syntax+private, section_length
        0x00,                    # protocol_version
        0x00,                    # encrypted_packet=0, flags
        0x00, 0x00, 0x00, 0x00, 0x00,   # pts_adjustment
        0x00,                    # cw_index
        0x0F, 0xFF,              # splice_command_length, splice_command_type
        0x00, 0x00, 0x00, 0x00,  # descriptor_loop_length + CRC_32 placeholder
    ])
    return body.hex()


def ads(base: str, n_content: int = 6) -> str:
    """VOD with one ad break, signalled three different ways.
    含一个广告区间的 VOD，同时用三种方式标记该区间。"""
    lines = _head(version=6)
    lines.append(f'#EXT-X-CUE-OUT:DURATION={_AD_BREAK_LEN * SEG_DUR:.1f}')
    lines.append(f'#EXT-X-DATERANGE:ID="adbreak-1",CLASS="com.example.ads",'
                 f'START-DATE="2024-01-01T00:00:18.000Z",'
                 f'DURATION={_AD_BREAK_LEN * SEG_DUR:.1f},'
                 f'PLANNED-DURATION={_AD_BREAK_LEN * SEG_DUR:.1f},'
                 f'SCTE35-OUT=0x{_splice_hex()}')
    for i in range(_AD_BREAK_FIRST):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.ts")
    for j in range(_AD_BREAK_LEN):
        lines.append("#EXT-X-GAP")     # 广告分片同时标 GAP
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"ad/{j}.ts")
    lines.append("#EXT-X-CUE-IN")
    lines.append('#EXT-X-DATERANGE:ID="adbreak-1",'
                 'END-DATE="2024-01-01T00:00:30.000Z",'
                 f'SCTE35-IN=0x{_splice_hex()}')
    for i in range(_AD_BREAK_FIRST, n_content):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 21. encryption combinations / 加密组合
# ---------------------------------------------------------------------------
#
# Three combinations that each exercise a distinct failure point:
# 三种组合，各考验一个不同的失败点：
#   aes-fmp4 : EXT-X-KEY (AES-128) + EXT-X-MAP (clear init) + encrypted .m4s
#              EXT-X-KEY + EXT-X-MAP（init 明文）+ 加密的 .m4s
#   aes-byterange : whole stream encrypted, then sliced on CIPHERTEXT length
#                   整体加密后再按密文长度切片
#   aes-disc : the key changes at an EXT-X-DISCONTINUITY boundary
#              密钥在 EXT-X-DISCONTINUITY 边界处更换

def aes_fmp4(base: str, n: int = N_SEG) -> str:
    lines = _head(version=7, independent=True)
    # EXT-X-KEY must precede EXT-X-MAP: the init segment is NOT encrypted,
    # only the media segments are.
    # EXT-X-KEY 必须写在 EXT-X-MAP 之前：init 段不加密，只有媒体分片加密。
    lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin",'
                 'IV=0x000102030405060708090a0b0c0d0e0f')
    lines.append('#EXT-X-MAP:URI="init.mp4"')
    for i in range(n):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def aes_byterange(base: str, seg_lens: list[int],
                  duration: float = SEG_DUR) -> str:
    """Ranges are over ciphertext; each range is an independently padded
    AES-128-CBC block sequence, so a client cannot merge them before decrypt.
    区间索引的是密文；每段都是独立做 PKCS7 填充的 AES-128-CBC 块序列，
    所以客户端不能先把这些区间合并成一个整体再解密。

    duration 可传更短的时长（分片时长与密文长度无关，缩短只为加快测试）。
    """
    lines = _head(version=4)
    lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin",'
                 'IV=0x000102030405060708090a0b0c0d0e0f')
    off = 0
    for ln in seg_lens:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{ln}@{off}")
        lines.append("big.enc")
        off += ln
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def aes_disc(base: str, n_a: int = 4, n_b: int = 3) -> str:
    """Clip A encrypted with key A; at the discontinuity the key changes to B.
    Clip A 用密钥 A 加密；到 EXT-X-DISCONTINUITY 处密钥切换为 B。"""
    lines = _head(version=3)
    lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key/a.bin",'
                 'IV=0x000102030405060708090a0b0c0d0e0f')
    for i in range(n_a):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"sega/{i}.ts")
    lines.append("#EXT-X-DISCONTINUITY")
    lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key/b.bin",'
                 'IV=0x000102030405060708090a0b0c0d0e0f')
    for i in range(n_b):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"segb/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 20. variable EXTINF durations + TARGETDURATION mismatch
#     变长 EXTINF + TARGETDURATION 不一致
# ---------------------------------------------------------------------------
#
# Real CDNs produce segment durations that vary (keyframe-aligned encoders,
# ad breaks, concatenated sources). The spec requires TARGETDURATION >= the
# rounded maximum EXTINF; several origins get this wrong, so both a correct
# and a deliberately-inconsistent variant are offered.
#
# 真实 CDN 的分片时长往往不等（关键帧对齐、插播广告、多源拼接等）。
# 规范要求 TARGETDURATION >= 最大的 EXTINF 取整值；但不少源站写错，
# 因此这里同时提供"正确"和"故意不一致"两种变体。

VARIABLE_DURATIONS = [6.0, 1.5, 12.0, 4.2, 6.0, 6.0, 0.8, 9.5, 6.0, 2.3]


def variable_vod(base: str, consistent: bool = True,
                 n: int | None = None) -> str:
    """VOD whose segments have wildly different durations.
    分片时长差异巨大的 VOD。

    consistent=True  -> TARGETDURATION:12 (>= the 12.0s maximum, legal)
                        合法：TARGETDURATION 12 >= 最大 12.0
    consistent=False -> TARGETDURATION:6  (less than the 12.0s max, a bug)
                        有缺陷：TARGETDURATION 6 < 最大 12.0（常见源端 bug）
    """
    durs = VARIABLE_DURATIONS if n is None else VARIABLE_DURATIONS[:n]
    target = 12 if consistent else 6
    return media_playlist([f"seg/{i}.ts" for i in range(len(durs))],
                          duration=durs, target=target)


# ---------------------------------------------------------------------------
# 19. multiple audio tracks + subtitles + closed captions
#     多音轨 + 字幕 + 隐藏字幕
# ---------------------------------------------------------------------------
#
# Two independent AUDIO groups ("aud" = main mix, "comm" = commentary), one
# SUBTITLES group ("subs") with per-language URIs, and one CLOSED-CAPTIONS
# group ("cc"), which by specification must NOT carry a URI attribute.
#
# 两个独立的 AUDIO 组（"aud" 主混音、"comm" 解说音轨）、一个 SUBTITLES 组
# （"subs"，按语言给 URI）、一个 CLOSED-CAPTIONS 组（"cc"，按规范不能带 URI）。
#
# Spec traps exercised here / 这里埋的规范陷阱:
#   * a variant advertises AUDIO="aud" -- a client that grabs the wrong group
#     (or mixes groups of different lengths) desynchronises.
#     变体声明 AUDIO="aud"；客户端若取错组、或把不同长度的组混在一起，就会失步。
#   * DEFAULT=YES on two renditions of DIFFERENT groups is legal; DEFAULT=YES
#     on two of the SAME group is not.
#     不同组各有一个 DEFAULT=YES 是合法的；同一组里出现两个 DEFAULT=YES 则不合法。
#   * CLOSED-CAPTIONS=NONE on a variant means "no captions", distinct from
#     omitting the attribute.
#     变体写 CLOSED-CAPTIONS=NONE 表示"无隐藏字幕"，与省略该属性含义不同。

RENDITION_AUDIO = [
    ("aud", "English", "en", "YES", "YES"),
    ("aud", "中文", "zh", "NO", "NO"),
    ("comm", "Commentary", "en", "YES", "NO"),
]

RENDITION_SUBS = [
    ("en", "English", "YES"),
    ("zh", "中文", "NO"),
]


def renditions_master(base: str) -> str:
    """多音轨 + 字幕 + 隐藏字幕的 master 列表。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",'
                 'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",'
                 'URI="audio/en/playlist.m3u8"')
    lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="中文",'
                 'LANGUAGE="zh",DEFAULT=NO,AUTOSELECT=NO,CHANNELS="2",'
                 'URI="audio/zh/playlist.m3u8"')
    lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="comm",NAME="Commentary",'
                 'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=NO,CHANNELS="2",'
                 'URI="audio/comm/playlist.m3u8"')
    lines.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",'
                 'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,'
                 'URI="subs/en/playlist.m3u8"')
    lines.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",'
                 'LANGUAGE="zh",DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,'
                 'URI="subs/zh/playlist.m3u8"')
    # CLOSED-CAPTIONS renditions must omit URI (they are in-band in the video).
    # CLOSED-CAPTIONS 的 rendition 必须省略 URI（隐藏字幕随视频带内传输）。
    lines.append('#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="CC1",'
                 'LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,INSTREAM-ID="CC1"')
    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=280000,RESOLUTION=416x234,'
                 'CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud",'
                 'SUBTITLES="subs",CLOSED-CAPTIONS="cc"')
    lines.append("video/360p.m3u8")
    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=640x360,'
                 'CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud",'
                 'SUBTITLES="subs",CLOSED-CAPTIONS="cc"')
    lines.append("video/720p.m3u8")
    return "\n".join(lines) + "\n"


def webvtt(lang: str, index: int, duration: float = SEG_DUR) -> str:
    """A valid WebVTT subtitle segment carrying an X-TIMESTAMP-MAP.
    一个合法的 WebVTT 字幕分片，带 X-TIMESTAMP-MAP。

    The timestamp map aligns the cue timeline with the MPEG-TS PCR clock;
    parsers that ignore it place subtitles at the wrong time. MPEGTS=0 is
    paired with LOCAL starting at 0 for the first segment.
    该时间戳映射把字幕时间轴对齐到 MPEG-TS 的 PCR 时钟；忽略它的解析器会把
    字幕放错时间点。第一片用 MPEGTS=0 搭配从 0 开始的 LOCAL。
    """
    start = index * duration
    end = start + duration

    def stamp(sec: float) -> str:
        """把秒数格式化成 WebVTT 的 HH:MM:SS.mmm。"""
        h = int(sec // 3600)
        m = int(sec % 3600 // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    lines = [
        "WEBVTT",
        "X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000",
        "",
        f"cue-{index}",
        f"{stamp(start)} --> {stamp(end)}",
        f"[{lang}] subtitle line {index}",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# bonus: EXT-X-DEFINE variable substitution
# 附加：EXT-X-DEFINE 变量替换
# ---------------------------------------------------------------------------

def define(base: str, n: int = N_SEG) -> str:
    """分片 URI 用 {$segbase} 变量（v8 特性）。"""
    lines = _head(version=8)
    lines.append(f'#EXT-X-DEFINE:NAME="segbase",VALUE="{base}/hls/define/segments/"')
    for i in range(n):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"{{$segbase}}seg_{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 17. malformed playlists / 不规范播放列表
# ---------------------------------------------------------------------------

def _plain_media(n: int = 6) -> list[str]:
    """构造一份"干净"的基础 media 列表，供各畸形变体改造。"""
    lines = _head()
    lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f')
    for i in range(n):
        lines.append(f"#EXTINF:{SEG_DUR:.3f},")
        lines.append(f"seg/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return lines


def malformed(kind: str) -> bytes:
    """返回某个畸形变体的原始字节（返回 bytes 以便控制 BOM / CRLF）。
    分片本身是正常的——这里考的是解析器的健壮性。"""
    lines = _plain_media()
    if kind == "crlf":
        # 全 CRLF 行尾（合法，但有些解析器只认 LF）
        return ("\r\n".join(lines) + "\r\n").encode()
    if kind == "bom":
        # 文件头带 UTF-8 BOM
        return b"\xef\xbb\xbf" + ("\r\n".join(lines) + "\r\n").encode()
    if kind == "no-version":
        # 缺 #EXT-X-VERSION
        lines = [l for l in lines if not l.startswith("#EXT-X-VERSION")]
        return ("\n".join(lines) + "\n").encode()
    if kind == "unknown-tags":
        # 插入若干未知标签（CDN 自定义 / 广告），解析器应忽略
        out = []
        for l in lines:
            out.append(l)
            if l == "#EXTM3U":
                out += ["#EXT-X-SOME-CDN-TAG:v=3", "#EXT-CUSTOM-AD:slot=pre"]
            if l.startswith("seg/"):
                out += ["#EXT-X-UNKNOWN-BEFORE-SEG:yes"]
        return ("\n".join(out) + "\n").encode()
    if kind == "spacing":
        # 多余空白、制表符、空行
        out = []
        for i, l in enumerate(lines):
            if l.startswith("seg/"):
                out.append("\t" + l + "   ")
            elif l.startswith("#EXTINF"):
                out.append(l + " ")
            else:
                out.append(l)
                out.append("")  # 标签后插空行
        return ("\n".join(out) + "\n\n").encode()
    if kind == "lowercase":
        # 标签全部小写（严格解析器可能拒绝）
        out = [l.lower() if l.startswith("#") else l for l in lines]
        return ("\n".join(out) + "\n").encode()
    if kind == "no-extm3u":
        # 缺首行 #EXTM3U
        return ("\n".join(lines[1:]) + "\n").encode()
    if kind == "bad-attrs":
        # #EXTINF 缺时长；IV 是奇数长度 hex
        out = [l for l in lines]
        out[2] = "#EXTINF:,"  # 缺时长
        out[3] = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x123'  # 奇数 hex
        return ("\n".join(out) + "\n").encode()
    if kind == "all":
        # 以上问题叠加：小写 + 未知标签 + CRLF + BOM
        out = []
        for l in lines:
            out.append(l.lower() if l.startswith("#") else l)
        out.insert(1, "#EXT-X-UNKNOWN:v=9")
        text = "\r\n".join(out) + "\r\n"
        return b"\xef\xbb\xbf" + text.encode()
    raise ValueError(f"unknown malformed kind: {kind}")


# ---------------------------------------------------------------------------
# 24. series / episodes -- 剧集：多集 + 上/下一集关系 + 全集共享密钥
#   剧集：多集 + 上/下一集关系 + 全集共享密钥
# ---------------------------------------------------------------------------
#
# 布局（全部相对 /hls/series/）：
# 布局（全部相对 /hls/series/）：
#
#   master.m3u8              -> 每集一个 EXT-X-STREAM-INF -> ep/{n}/playlist.m3u8
#   key.bin                  全集共用的 AES-128 密钥（上/下一集共享同一把）
#   ep/{n}/playlist.m3u8     第 n 集媒体列表（加密，含 PREV/NEXT 关系标签）
#   ep/{n}/seg/{i}.ts        第 n 集分片（AES-128 加密）
#
# 设计要点：
# - 上/下一集关系用自定义标签 #EXT-X-SERIES-PREV / #EXT-X-SERIES-NEXT 表达
#   （HLS 没有原生先/后集标签）。这本身即真实场景：**客户端必须能忽略未知
#   的 EXT-X-* 标签并继续下载**，而不是遇到不认识的标签就报错。
# - 全集（含上/下一集）共用一把 AES-128 密钥 —— 客户端只需取一次 key.bin。
# - 每集分片数不同，考研客户端对 EXTINF 累加与片数变化的处理。

# ep -> {"name": 集标题, "n": 该集分片数}
# 第 1 集 4 片、第 2 集 6 片、第 3 集 5 片（刻意不等，避免"片数固定"的假设）。
SERIES_EPISODES = {
    1: {"name": "S01E01 Pilot", "n": 4},
    2: {"name": "S01E02 The Long Segment", "n": 6},
    3: {"name": "S01E03 Finale", "n": 5},
}
SERIES_EP_COUNT = len(SERIES_EPISODES)


def series_ep_ids() -> list[int]:
    """有序的集号列表（升序）。"""
    return sorted(SERIES_EPISODES)


def series_master(base: str) -> str:
    """顶层 master：每集一个变体，URI 形如 ep/{n}/playlist.m3u8。

    注意 base 在此仅作为接口约定保留（URI 全部用相对路径，才能考验客户端
    相对"包含它的文档"逐层解析的能力）。
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    for ep in series_ep_ids():
        # 用 #EXT-X-STREAM-INF 的 NAME 属性携带集标题（便于人读）。
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,'
            f'CODECS="avc1.64001e,mp4a.40.2",NAME="{SERIES_EPISODES[ep]["name"]}"')
        lines.append(f"ep/{ep}/playlist.m3u8")
    return "\n".join(lines) + "\n"


def series_episode(base: str, ep: int) -> str:
    """第 ep 集的媒体列表：AES-128 加密 + PREV/NEXT 关系标签。

    key URL 用相对路径 ../key.bin（相对 ep/{n}/ 目录）。
    """
    if ep not in SERIES_EPISODES:
        raise ValueError(f"unknown series episode: {ep}")
    n = SERIES_EPISODES[ep]["n"]
    uris = [f"seg/{i}.ts" for i in range(n)]
    # 上/下一集关系（自定义标签，客户端应忽略未知标签继续）。
    relations = []
    if ep - 1 in SERIES_EPISODES:
        relations.append(f'#EXT-X-SERIES-PREV:URI="../{ep - 1}/playlist.m3u8"')
    if ep + 1 in SERIES_EPISODES:
        relations.append(f'#EXT-X-SERIES-NEXT:URI="../{ep + 1}/playlist.m3u8"')
    extra_head = [
        '#EXT-X-KEY:METHOD=AES-128,URI="../key.bin",IV=0x'
        "000102030405060708090a0b0c0d0e0f",
        *relations,
    ]
    return media_playlist(uris, version=3, extra_head=extra_head)

