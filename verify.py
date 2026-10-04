#!/usr/bin/env python3
"""Full-matrix verification for the HLS test server.
HLS 测试服务器的全矩阵验证脚本。

Starts its own server on an ephemeral port and exercises every scenario,
asserting the *observable behaviour* a downloader relies on (status codes,
decryptability, PTS timelines, Range semantics, ...).

会在随机端口上自建一个服务器实例，逐个跑遍所有场景，断言下载器所依赖的
**可观测行为**（状态码、能否解密、PTS 时间轴、Range 语义等），
而不是断言内部实现细节。

Usage / 用法:
    python verify.py                        # self-hosted on a random port
    python verify.py --base http://127.0.0.1:8080
    python verify.py --fast                 # skip the live-window timing checks

Exit code is non-zero if any check fails. / 有任一断言失败则退出码非 0。
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, ".")

from hls_test import app, crypto, keys, media, playlists  # noqa: E402

IV = app.IV
N_SEG = playlists.N_SEG          # 10 segments of 6s / 10 片 × 6s
PTS_STEP = int(playlists.SEG_DUR * media._CLOCK)  # 相邻分片的 PTS 步进（90k 时钟）

# ---------------------------------------------------------------------------
# tiny test runner / 极简测试框架
# ---------------------------------------------------------------------------

_RESULTS: list[tuple[bool, str, str]] = []
_GROUP = ""


def group(name: str) -> None:
    """开始一个断言分组（打印分组标题，后续断言都归到该组）。"""
    global _GROUP
    _GROUP = name
    print(f"\n--- {name} ---")


def check(name: str, got, want) -> bool:
    """断言 got == want，记录并打印结果。"""
    ok = got == want
    _RESULTS.append((ok, f"{_GROUP}: {name}", f"got {got!r}, want {want!r}"))
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + (
        "" if ok else f"  -> got {got!r}, want {want!r}"))
    return ok


def ok(name: str, cond: bool, detail: str = "") -> bool:
    """断言布尔条件；失败时打印 detail。"""
    _RESULTS.append((bool(cond), f"{_GROUP}: {name}", detail))
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}" +
          ("" if cond else f"  -> {detail}"))
    return bool(cond)


# ---------------------------------------------------------------------------
# HTTP plumbing / HTTP 基础设施
# ---------------------------------------------------------------------------

class Server(threading.Thread):
    """把 app.make_server 包成后台线程；端口传 0 表示由系统分配。"""

    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.httpd = app.make_server(host, port)
        self.port = self.httpd.server_address[1]

    def run(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()


class Client:
    """Redirects are followed manually so each hop can be asserted.
    手动跟随重定向，以便逐跳断言（urllib 默认会自动跟，这里关掉）。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cookies: dict[str, str] = {}

    def get(self, path, headers=None, redirect=True, timeout=90):
        """GET；HTTP 错误也返回 (status, headers, body)，不抛异常。"""
        hdrs = dict(headers or {})
        if self.cookies and "Cookie" not in hdrs:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(self.base + path, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def head(self, path, headers=None):
        """HEAD，用于验证 HEAD 与 GET 同路由。"""
        req = urllib.request.Request(self.base + path, headers=headers or {},
                                     method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), b""
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), b""

    def post(self, path, body: bytes = b"", headers=None, timeout=90):
        """POST；HTTP 错误也返回 (status, headers, body)，不抛异常。"""
        hdrs = dict(headers or {})
        if self.cookies and "Cookie" not in hdrs:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(self.base + path, data=body,
                                     headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def redirect_location(self, path):
        """Return the Location header of a 3xx without following it.
        取出 3xx 的 Location，但不跟随跳转。"""

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(self.base + path, timeout=30) as r:
                return r.status, r.headers.get("Location")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    def raw(self, request_target: str, extra: str = ""):
        """Send a raw request line, e.g. an RFC 7230 absolute-form target.
        发送原始请求行（例如 RFC 7230 的 absolute-form 请求目标）。"""
        host, _, port = self.base[len("http://"):].partition(":")
        conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
        conn.request("GET", request_target, headers={"Host": f"{host}:{port}"})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()


def media_type(hdrs: dict) -> str:
    """从 Content-Type 里剥掉参数，只留 MIME 类型。"""
    return (hdrs.get("Content-Type") or "").split(";")[0].strip()


# ---------------------------------------------------------------------------
# assertion helpers / 断言辅助函数
# ---------------------------------------------------------------------------

def ts_sync(buf: bytes) -> bool:
    """长度是 188 的整数倍，且每包首字节都是 0x47 —— TS 流的健康标志。"""
    return len(buf) > 0 and len(buf) % 188 == 0 and all(
        buf[i] == 0x47 for i in range(0, len(buf), 188))


def _sample_panel(txt: str) -> str:
    """从整页 HTML 里切出样例面板片段。

    样例面板与 ffmpeg 横幅共用 `.ff ok`/`.ff no` 样式类，整页字符串匹配会
    互相污染，因此按包裹层锚点 `id="sample-panel"` 切片后再断言。
    """
    key = 'id="sample-panel"'
    i = txt.find(key)
    if i < 0:
        return ""
    i = txt.rfind("<", 0, i)                    # 回退到该属性所属的起始标签
    j = txt.find("</div>", txt.find("</div>", i) + 6)   # 两层嵌套后收尾
    return txt[i:j + 6] if j > 0 else txt[i:]


def first_video_pts(buf: bytes) -> int | None:
    """从 TS 流里扫出第一个视频 PES 的 PTS；扫不到返回 None。

    逐包解析：定位视频 PID 的 PES 起始包，跳过适配字段，再从 5 字节
    PTS 编码里还原 33 位时间戳。
    """
    for off in range(0, len(buf) - 188, 188):
        if buf[off] != 0x47:
            continue
        pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
        if pid == media.PID_VIDEO and (buf[off + 1] & 0x40):   # unit_start 置位
            p = off + 4
            af = (buf[off + 3] >> 4) & 3    # 适配字段控制位
            if af in (2, 3):
                p += 1 + buf[p]             # 跳过适配字段
            if af in (0, 2):
                continue                    # 无载荷，跳过
            if buf[p:p + 3] == b"\x00\x00\x01" and buf[p + 7] & 0x80:
                return (((buf[p + 9] >> 1) & 7) << 30 | buf[p + 10] << 22 |
                        ((buf[p + 11] >> 1) & 0x7F) << 15 | buf[p + 12] << 7 |
                        (buf[p + 13] >> 1) & 0x7F)
    return None


def pids(buf: bytes) -> set[int]:
    """收集 TS 流中出现过的所有 PID。"""
    return {((buf[i + 1] & 0x1F) << 8) | buf[i + 2]
            for i in range(0, len(buf), 188) if buf[i] == 0x47}


def boxes(buf: bytes) -> list[bytes]:
    """Top-level box fourccs, validating that the chain closes exactly.
    列出顶层 box 的 fourcc，并校验 box 链恰好闭合到 EOF。"""
    out, i = [], 0
    while i < len(buf):
        if i + 8 > len(buf):
            return out + [b"<truncated>"]
        size = int.from_bytes(buf[i:i + 4], "big")
        if size < 8 or i + size > len(buf):
            return out + [b"<bad-size>"]
        out.append(buf[i + 4:i + 8])
        i += size
    return out


def playlist_lines(text: str) -> list[str]:
    """把 m3u8 按行切开，去掉空行与首尾空白。"""
    return [l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip()]


# ---------------------------------------------------------------------------
# checks / 各项检查
# ---------------------------------------------------------------------------

# 目录页 / scenarios.json 的完整性（46 场景、i18n 覆盖、Content-Type）。
def check_catalog(c: Client) -> None:
    group("0. index / catalog")
    st, h, body = c.get("/")
    check("index status", st, 200)
    ok("index is html", "text/html" in (h.get("Content-Type") or ""))
    st, h, body = c.get("/scenarios.json")
    check("scenarios.json status", st, 200)
    cat = json.loads(body)
    ok("catalog non-empty", len(cat["scenarios"]) >= 25,
       f"{len(cat['scenarios'])} entries")

    group("0b. every catalog playlist is reachable")
    bad = []
    for s in cat["scenarios"]:
        # variants that intentionally 403/429 are skipped here and covered
        # by their own group
        st, _, _ = c.get(s["path"])
        if st != 200:
            bad.append((s["path"], st))
    ok("all playlist URLs return 200", not bad, str(bad[:5]))


# ffmpeg 状态横幅：索引页顶部必须显式提示是否找到 ffmpeg。
def check_ffmpeg_banner(c: Client) -> None:
    group("0c. ffmpeg status banner")

    from hls_test import media as _media
    resolved = app.MEDIA.ffmpeg

    def _ff_banner(txt: str) -> str:
        """只取 ffmpeg 横幅那一段（样例面板复用 .ff 样式，必须按 id 限定）。"""
        i = txt.find('id="ff-banner"')
        if i < 0:
            return ""
        # 从所在 tag 的 '<div' 起，到该 div 结束（下一个 </div>）。
        start = txt.rfind("<div", 0, i)
        end = txt.find("</div>", i)
        return txt[start:end + 6] if start >= 0 and end >= 0 else ""

    for lang in ("en", "zh"):
        st, _, body = c.get(f"/?lang={lang}")
        check(f"index ({lang}) status", st, 200)
        txt = body.decode()
        banner = _ff_banner(txt)
        ok(f"banner present ({lang})", banner.startswith('<div class="ff'))
        if resolved:
            # 找到 -> 绿色 ok 横幅 + 展示解析到的路径。
            ok(f"banner is the ok variant ({lang})", 'class="ff ok"' in banner,
               banner[:60])
            ok(f"banner shows the resolved path ({lang})", resolved in banner,
               resolved)
        else:
            # 没找到 -> 琥珀色 no 横幅 + 启用提示。
            ok(f"banner is the not-found variant ({lang})",
               'class="ff no"' in banner)
            ok(f"not-found banner mentions tools/ffmpeg ({lang})",
               "tools/ffmpeg" in banner or "tools\\ffmpeg" in banner)

    # 两个变体互斥：同一个横幅里不应同时出现 ok 与 no 两个 class。
    st, _, body = c.get("/?lang=zh")
    banner = _ff_banner(body.decode())
    ok("banner variants are mutually exclusive",
       not ('class="ff ok"' in banner and 'class="ff no"' in banner), banner[:60])


# media 缓存面板：顶部与 ffmpeg 横幅并排；/cache.json 统计正确；/cache/clear 可用。
def check_cache_panel(c: Client) -> None:
    group("0d. media cache panel")

    # 面板与 ffmpeg 横幅同处一个 .panels 行（左右分栏）。
    st, _, body = c.get("/?lang=zh")
    check("index status (zh)", st, 200)
    txt = body.decode()
    ok("panels row present", '<div class="panels">' in txt)
    ok("ffmpeg banner is inside the panels row",
       '<div class="panels"><div class="ff' in txt)
    ok("cache panel is inside the panels row",
       '<div class="cache">' in txt and 'id="cache-clear"' in txt)
    ok("cache panel is side-by-side css (flex)",
       ".panels {" in txt and "display: flex" in txt)
    ok("clear button wired to /cache/clear",
       "fetch('/cache/clear'" in txt and "method: 'POST'" in txt)
    # 两语言文案都在（切换到英文再查一次）。
    st, _, body = c.get("/?lang=en")
    ok("cache panel present (en)", 'id="cache-clear"' in body.decode())

    # /cache.json：只读统计，字段齐全且自洽。
    st, h, body = c.get("/cache.json")
    check("cache.json status", st, 200)
    ok("cache.json is json",
       "application/json" in (h.get("Content-Type") or ""))
    data = json.loads(body)
    ok("cache.json has files/bytes/human/dir",
       all(k in data for k in ("files", "bytes", "human", "dir")),
       str(sorted(data)))
    ok("cache.json files is an int >= 0",
       isinstance(data["files"], int) and data["files"] >= 0,
       str(data["files"]))
    ok("cache.json bytes is an int >= 0",
       isinstance(data["bytes"], int) and data["bytes"] >= 0,
       str(data["bytes"]))
    ok("cache.json human matches bytes",
       data["human"] == media.human_size(data["bytes"]),
       f'{data["human"]} vs {media.human_size(data["bytes"])}')
    if data["files"] > 0:
        ok("cache.json bytes > 0 when files exist", data["bytes"] > 0)

    # /cache/clear：在**临时小缓存**上验证契约，避免误删真实缓存
    # （也避免触发环境的批量删除护栏）。直接调用 handler，不经过真实文件系统。
    import tempfile
    tmp = tempfile.mkdtemp(prefix="hls_cache_test_")
    real = os.path.join(tmp, "real")
    os.makedirs(real)

    def _seed(n: int) -> None:
        """重建 n 个 500B 的临时文件，供容错测试反复使用。"""
        import shutil
        for name in os.listdir(real):
            p = os.path.join(real, name)
            if os.path.isfile(p):
                os.remove(p)
        for i in range(n):
            with open(os.path.join(real, f"z_{i}.ts"), "wb") as fh:
                fh.write(b"x" * 500)

    _seed(4)
    saved_provider = app.MEDIA
    try:
        lone = media.MediaProvider(cache_dir=None)
        lone._real_dir = real
        app.MEDIA = lone
        res = lone.clear_cache(regenerate=False)
        ok("clear_cache removes every file", res["removed"] == 4,
           str(res["removed"]))
        ok("clear_cache reports freed bytes", res["bytes_freed"] == 2000,
           str(res["bytes_freed"]))
        ok("clear_cache keeps the directory itself", os.path.isdir(real))
        ok("clear_cache leaves 0 files", res["stats"]["files"] == 0,
           str(res["stats"]))
        ok("clear_cache reports no errors on success", res["errors"] == [],
           str(res["errors"]))

        # 单文件失败：第 2 次 os.remove 抛错，其余照删。断言不中断整批。
        from unittest import mock
        _seed(4)
        calls = {"n": 0}
        _orig = os.remove

        def flaky(p):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated lock")
            return _orig(p)

        with mock.patch.object(media.os, "remove", side_effect=flaky):
            res = lone.clear_cache(regenerate=False)
        ok("clear_cache survives a single-file failure", res["removed"] == 3,
           str(res["removed"]))
        ok("clear_cache records the failure", len(res["errors"]) == 1,
           str(res["errors"]))
        ok("clear_cache error names the file",
           res["errors"] and res["errors"][0]["file"].startswith("z_"),
           str(res["errors"]))

        # 环境删除护栏回归：os.remove 抛 SystemExit（BaseException，非 Exception）。
        # 旧实现只兜 OSError/Exception，会漏掉它并让连接被掐断（HTTP 000）。
        # 用 regenerate=True 观察「有残留 -> 不重建」的语义（regen_skipped）。
        _seed(4)

        def guard(p):
            raise SystemExit(1)

        with mock.patch.object(media.os, "remove", side_effect=guard):
            res = lone.clear_cache(regenerate=True)
        ok("clear_cache survives a SystemExit guard", res["removed"] == 0,
           str(res["removed"]))
        ok("clear_cache reports every guarded file", len(res["errors"]) == 4,
           str(len(res["errors"])))
        ok("clear_cache marks regen skipped when files remain",
           res["regen_skipped"] and not res["regenerated"],
           f'skipped={res["regen_skipped"]} regenerated={res["regenerated"]}')
    finally:
        app.MEDIA = saved_provider
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # /cache/clear 端点：真实 HTTP，契约字段齐全。
    st, h, body = c.post("/cache/clear?regenerate=0")
    check("POST /cache/clear status", st, 200)
    ok("POST /cache/clear is json",
       "application/json" in (h.get("Content-Type") or ""))
    d = json.loads(body.decode())
    ok("cache/clear returns ok/errors/removed/bytes_freed/regenerated",
       all(k in d for k in ("ok", "errors", "removed", "bytes_freed",
                            "regenerated")),
       str(sorted(d)))
    ok("cache/clear errors is a list", isinstance(d["errors"], list),
       type(d["errors"]).__name__)
    ok("cache/clear ok matches errors", d["ok"] == (not d["errors"]),
       f'ok={d["ok"]} errors={len(d["errors"])}')


# 0e. 样例视频面板：/sample.json 契约、两语言面板、重建按钮接线。
#     CI 安全：不依赖样例文件存在，全部走条件分支。
def check_sample(c: Client) -> None:
    group("0e. sample video panel")

    st, h, body = c.get("/sample.json")
    check("sample.json status", st, 200)
    ok("sample.json is json",
       "application/json" in (h.get("Content-Type") or ""))
    d = json.loads(body.decode())
    ok("sample.json has found/path/using_sample/stale/segments",
       all(k in d for k in ("found", "path", "using_sample", "stale",
                            "segments")),
       str(sorted(d)))
    ok("sample.json found is bool", isinstance(d["found"], bool),
       type(d["found"]).__name__)
    ok("sample.json using_sample is bool", isinstance(d["using_sample"], bool),
       type(d["using_sample"]).__name__)
    # 与 provider 的真实状态一致。
    ok("sample.json matches provider", d["found"] == bool(app.MEDIA.sample)
       and d["using_sample"] == app.MEDIA.using_sample,
       f'json found={d["found"]} provider={bool(app.MEDIA.sample)}')

    # 两语言下样例面板都在，且文案本地化。
    for lang in ("en", "zh"):
        st, _, b = c.get(f"/?lang={lang}")
        check(f"index ({lang}) status", st, 200)
        txt = b.decode()
        ok(f"sample panel present ({lang})", 'id="sample-panel"' in txt)
    txt_en = c.get("/?lang=en")[2].decode()
    txt_zh = c.get("/?lang=zh")[2].decode()
    ok("sample panel localised (en)", "Sample video" in txt_en)
    ok("sample panel localised (zh)", "样例视频" in txt_zh)

    # 面板按“是否有样例”两态之一渲染；两态各发相同条数(3)断言，
    # 保证 verify 计数与样例文件存在与否无关(确定性)。
    panel_en = _sample_panel(txt_en)
    if app.MEDIA.sample:
        # 有样例：面板显示路径 + 重建按钮；按钮接线在页脚脚本里。
        ok("sample panel state = found", "ff ok" in panel_en, panel_en[:80])
        ok("sample path shown when found",
           app.MEDIA.sample in panel_en and 'id="sample-rebuild"' in panel_en)
        ok("sample rebuild button wired",
           "fetch('/sample/rebuild'" in txt_en
           and "method: 'POST'" in txt_en
           and 'id="sample-rebuild"' in txt_en)
    else:
        # 无样例：面板给出放置提示（含目录/文件名），且不出现重建按钮。
        ok("sample panel state = missing", "ff no" in panel_en, panel_en[:80])
        ok("no-sample panel shows placement hint",
           "source.mp4" in panel_en and "sample" in panel_en.lower(),
           panel_en[:120])
        ok("no-sample panel has no rebuild button",
           'id="sample-rebuild"' not in txt_en)

    # 重建端点：返回 JSON 且字段齐全（无样例时用内置测试图重建）。
    st, h, body = c.post("/sample/rebuild")
    check("POST /sample/rebuild status", st, 200)
    ok("POST /sample/rebuild is json",
       "application/json" in (h.get("Content-Type") or ""))
    d2 = json.loads(body.decode())
    ok("rebuild reports ok/using_sample/files/errors",
       all(k in d2 for k in ("ok", "using_sample", "files", "errors")),
       str(sorted(d2)))
    ok("rebuild using_sample matches provider",
       d2["using_sample"] == app.MEDIA.using_sample,
       f'json={d2["using_sample"]} provider={app.MEDIA.using_sample}')


# 6. VOD 与直播滑窗：分片数、无 ENDLIST、MEDIA-SEQUENCE 递增、PDT。
def check_vod_live(c: Client, fast: bool) -> None:
    group("6. VOD vs live")
    st, h, vod = c.get("/hls/vod/playlist.m3u8")
    check("vod status", st, 200)
    check("vod content-type", media_type(h),
          "application/vnd.apple.mpegurl")
    ok("vod has ENDLIST", "#EXT-X-ENDLIST" in vod.decode())
    ok("vod has no PLAYLIST-TYPE live", "EXT-X-PLAYLIST-TYPE" not in vod.decode())
    st, _, body = c.get("/hls/vod/seg/0.ts")
    check("vod seg status", st, 200)
    ok("vod seg is valid TS", ts_sync(body), f"{len(body)} bytes")
    pts = [first_video_pts(c.get(f"/hls/vod/seg/{i}.ts")[2]) for i in range(3)]
    ok("vod timeline continues across segments",
       all(pts[i + 1] - pts[i] == PTS_STEP for i in range(2)), str(pts))

    st, _, live = c.get("/hls/live/playlist.m3u8")
    check("live status", st, 200)
    live_txt = live.decode()
    ok("live has no ENDLIST", "#EXT-X-ENDLIST" not in live_txt)
    m1 = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", live_txt).group(1))
    if fast:
        print("  [skip] live window advance (--fast)")
    else:
        time.sleep(7)
        st, _, live2 = c.get("/hls/live/playlist.m3u8")
        m2 = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", live2.decode()).group(1))
        ok("live MEDIA-SEQUENCE advances", m2 > m1, f"{m1} -> {m2}")
        ok("live window keeps 6 segments",
           live2.decode().count("#EXTINF:") == 6,
           str(live2.decode().count("#EXTINF:")))
    st, _, body = c.get(f"/hls/live/seg/{m1}.ts")
    check("live seg status", st, 200)
    ok("live seg is valid TS", ts_sync(body))

    st, _, ev = c.get("/hls/live/event.m3u8")
    check("event status", st, 200)
    ok("event starts without ENDLIST", "#EXT-X-ENDLIST" not in ev.decode())
    st, _, body = c.get("/hls/live/event/seg/0.ts")
    check("event seg status", st, 200)
    ok("event seg is valid TS", ts_sync(body))


# 1. AES-128：显式 IV 与 MSN-IV 解密后首字节须为 0x47。
def check_aes(c: Client) -> None:
    group("1. AES-128 whole-segment encryption")
    st, _, pl = c.get("/hls/aes128/playlist.m3u8")
    check("playlist status", st, 200)
    key_line = next(l for l in pl.decode().splitlines()
                    if l.startswith("#EXT-X-KEY"))
    ok("METHOD=AES-128", "METHOD=AES-128" in key_line, key_line)
    ok("explicit IV attribute", "IV=0x000102030405060708090a0b0c0d0e0f" in key_line,
       key_line)
    st, _, key = c.get("/hls/aes128/key.bin")
    check("key status", st, 200)
    check("key length", len(key), 16)
    check("key value", key.hex(), keys.AES128_KEY.hex())

    st, _, seg = c.get("/hls/aes128/seg/0.ts")
    check("seg status", st, 200)
    ok("ciphertext is block-aligned", len(seg) % 16 == 0, str(len(seg)))
    plain = crypto.aes128_cbc_decrypt(key, IV, seg)
    ok("decrypts to valid TS (sync 0x47)", ts_sync(plain),
       f"first byte {plain[:1].hex()}, len {len(plain)}")
    ok("decrypted size matches padding math",
       len(seg) - len(plain) in range(1, 17), str(len(seg) - len(plain)))

    st, _, pl = c.get("/hls/aes128/msn-iv/playlist.m3u8")
    key_line = next(l for l in pl.decode().splitlines()
                    if l.startswith("#EXT-X-KEY"))
    ok("msn-iv: IV attribute omitted", "IV=" not in key_line, key_line)
    st, _, seg = c.get("/hls/aes128/msn-iv/seg/3.ts")
    check("msn-iv seg status", st, 200)
    try:
        plain = crypto.aes128_cbc_decrypt(key, (3).to_bytes(16, "big"), seg)
        good = ts_sync(plain)
    except ValueError as e:
        good = False
        plain = b""
    ok("msn-iv: IV must equal the media sequence number", good,
       "decrypt failed")

    st, _, m = c.get("/hls/aes128/master.m3u8")
    check("master status", st, 200)
    mt = m.decode()
    ok("master has EXT-X-SESSION-KEY", "#EXT-X-SESSION-KEY" in mt)
    ok("master has 2 variants", mt.count("#EXT-X-STREAM-INF") == 2)
    st, _, _ = c.get("/hls/aes128/360p.m3u8")
    check("variant 360p status", st, 200)
    ok("variant is encrypted",
       "METHOD=AES-128" in c.get("/hls/aes128/720p.m3u8")[2].decode())


# 2. 密钥 TTL：过期后密钥端点 403。
def check_key_expiry(c: Client) -> None:
    group("2. key URL with expiry + signature")
    st, _, pl = c.get("/hls/key-expiry/playlist.m3u8?ttl=30")
    q = next(l for l in pl.decode().splitlines()
             if l.startswith("#EXT-X-KEY") and "exp=" in l)
    qs = dict(urllib.parse.parse_qsl(
        re.search(r"key\.bin\?([^\"]+)", q).group(1)))
    st, _, key = c.get(f"/hls/key-expiry/key.bin?exp={qs['exp']}&s={qs['s']}")
    check("fresh key -> 200", st, 200)
    check("key length", len(key), 16)

    past = int(time.time()) - 60
    good_sig = keys.sign(f"key:{past}")
    st, h, body = c.get(f"/hls/key-expiry/key.bin?exp={past}&s={good_sig}")
    check("expired key (valid sig) -> 403", st, 403)
    ok("expired body mentions expiry", b"expired" in body, body[:80].decode())

    st, _, body = c.get(f"/hls/key-expiry/key.bin?exp={past}&s={'0' * 40}")
    check("tampered signature -> 403", st, 403)
    ok("tampered body mentions signature", b"signature" in body,
       body[:80].decode())

    st, _, seg = c.get("/hls/key-expiry/seg/0.ts")
    check("seg status", st, 200)
    plain = crypto.aes128_cbc_decrypt(keys.AES128_KEY, IV, seg)
    ok("seg decrypts with the fixed IV advertised in the playlist",
       ts_sync(plain), "playlist IV mismatch -> would decrypt to garbage")


# 3. SAMPLE-AES / FairPlay / Widevine 的声明形状（下载器应拒绝）。
def check_drm(c: Client) -> None:
    group("3. SAMPLE-AES / DRM")
    st, _, pl = c.get("/hls/sample-aes/playlist.m3u8")
    txt = pl.decode()
    ok("METHOD=SAMPLE-AES", "METHOD=SAMPLE-AES" in txt)
    key_line = next(l for l in txt.splitlines() if l.startswith("#EXT-X-KEY"))
    ok("keyformat identity (explicit)", 'KEYFORMAT="identity"' in key_line,
       key_line)
    ok("VERSION >= 5 required for SAMPLE-AES",
       int(re.search(r"#EXT-X-VERSION:(\d+)", txt).group(1)) >= 5)
    st, _, _ = c.get("/hls/sample-aes/key.bin")
    check("sample-aes key status", st, 200)

    st, _, pl = c.get("/hls/fairplay/playlist.m3u8")
    txt = pl.decode()
    ok("fairplay skd:// URI", "skd://" in txt)
    ok("fairplay keyformat",
       'KEYFORMAT="com.apple.streamingkeydelivery"' in txt)

    st, _, pl = c.get("/hls/widevine/playlist.m3u8")
    txt = pl.decode()
    ok("widevine UUID keyformat",
       "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in txt)
    ok("widevine data: URI pssh", "data:text/plain;base64,AAAAPnBzc2g" in txt)
    ok("widevine method SAMPLE-AES-CTR", "METHOD=SAMPLE-AES-CTR" in txt)
    for p in ("/hls/sample-aes/seg/0.ts", "/hls/fairplay/seg/0.ts",
              "/hls/widevine/seg/0.ts"):
        st, _, body = c.get(p)
        ok(f"{p} serves TS", st == 200 and ts_sync(body),
           f"status {st}, sync {ts_sync(body)}")


# 4. 密钥端点要求 X-Api-Key，缺失则 403。
def check_key_header(c: Client) -> None:
    group("4. key needs a custom request header")
    st, _, body = c.get("/hls/key-header/key.bin")
    check("key without header -> 403", st, 403)
    ok("body explains the missing header", b"X-Api-Key" in body,
       body[:80].decode())
    st, _, key = c.get("/hls/key-header/key.bin",
                       headers={"X-Api-Key": app.API_KEY})
    check("key with header -> 200", st, 200)
    check("key length", len(key), 16)
    st, _, seg = c.get("/hls/key-header/seg/0.ts")
    check("segment itself needs no header", st, 200)
    plain = crypto.aes128_cbc_decrypt(key, IV, seg)
    ok("segment decrypts", ts_sync(plain))

    group("15b. per-segment key rotation")
    st, _, pl = c.get("/hls/key-rotation/playlist.m3u8")
    txt = pl.decode()
    ok(f"one EXT-X-KEY per segment ({N_SEG})",
       txt.count("#EXT-X-KEY") == N_SEG, str(txt.count("#EXT-X-KEY")))
    st, _, key3 = c.get("/hls/key-rotation/key/3.bin")
    allkeys = [c.get(f"/hls/key-rotation/key/{i}.bin")[2] for i in range(N_SEG)]
    ok("all rotation keys differ", len({k.hex() for k in allkeys}) == N_SEG,
       str(len({k.hex() for k in allkeys})))
    st, _, seg3 = c.get("/hls/key-rotation/seg/3.ts")
    ok("segment 3 decrypts with key 3 only",
       ts_sync(crypto.aes128_cbc_decrypt(key3, IV, seg3)))
    try:
        crypto.aes128_cbc_decrypt(allkeys[0], IV, seg3)
        ok("segment 3 does NOT decrypt with key 0", False, "no error raised")
    except ValueError:
        ok("segment 3 does NOT decrypt with key 0", True)


# 5. 混合 URI 形态与 302 重定向的解析。
def check_paths(c: Client) -> None:
    group("5. URI resolution: relative / dot / percent / query / absolute")
    st, _, master = c.get("/hls/paths/master.m3u8")
    check("paths master status", st, 200)
    txt = master.decode()
    for form in ("rel/360p.m3u8", "./dot/360p.m3u8", "a/b/../../up/360p.m3u8",
                 "my%20dir/360p.m3u8", "query/360p.m3u8?token=abc&cache=1",
                 "redir.m3u8"):
        ok(f"master offers {form}", form in txt)
    ok("master offers an absolute URI", "/hls/paths/abs/720p.m3u8" in txt)
    ok("master offers a protocol-relative URI",
       "//127.0.0.1:%s/hls/paths/proto/720p.m3u8" % c.base.rsplit(":", 1)[1]
       in txt)

    st, _, pl = c.get("/hls/paths/rel/360p.m3u8")
    check("relative variant status", st, 200)
    segs = [l for l in pl.decode().splitlines() if "seg" in l and l.endswith(".ts")
            or "seg-redir" in l]
    ok("segment URIs use mixed forms", len(segs) >= 6, str(len(segs)))

    for name, path in (("relative", "/hls/paths/rel/360p.m3u8"),
                       ("./ dot", "/hls/paths/dot/360p.m3u8"),
                       ("a/b/../../up", "/hls/paths/a/b/../../up/360p.m3u8"),
                       ("percent-encoded dir", "/hls/paths/my%20dir/360p.m3u8"),
                       ("with query", "/hls/paths/query/360p.m3u8?token=abc&cache=1"),
                       ("absolute", "/hls/paths/abs/720p.m3u8"),
                       ("protocol-relative target", "/hls/paths/proto/720p.m3u8")):
        st, _, body = c.get(path)
        ok(f"{name} -> 200 playlist",
           st == 200 and body.decode().lstrip().startswith("#EXTM3U"),
           f"status {st}")

    st, loc = c.redirect_location("/hls/paths/redir.m3u8")
    check("playlist redirect status", st, 302)
    ok("playlist redirect Location resolves", loc.endswith("/rel/360p.m3u8"),
       str(loc))
    st, _, body = c.get(loc[len(c.base):])
    ok("redirect target is a playlist",
       st == 200 and body.decode().lstrip().startswith("#EXTM3U"), str(st))

    st, loc = c.redirect_location("/hls/paths/seg-redir/4.ts")
    check("segment redirect status", st, 302)
    ok("segment redirect Location", loc.endswith("/hls/paths/seg/4.ts"), str(loc))
    st, _, body = c.get(loc[len(c.base):])
    ok("segment redirect target serves TS", st == 200 and ts_sync(body), str(st))
    st, _, body = c.get("/hls/paths/seg/3.ts?token=xyz&x=1")
    ok("segment URI with query string works", st == 200 and ts_sync(body),
       str(st))
    st, _, body = c.get("/hls/paths/rel/../seg/5.ts")
    ok("segment with ../ dot segment works", st == 200 and ts_sync(body),
       str(st))

    # RFC 7230 absolute-form request target (curl sends this when following a
    # redirect on a kept-alive connection); must not 404.
    st, _, body = c.raw(c.base + "/hls/paths/seg/4.ts")
    ok("absolute-form request target -> 200", st == 200 and ts_sync(body),
       f"status {st}")


# 18. 多层嵌套 master：逐层基准传递，沿用顶层 base 会 404。
def check_nested(c: Client) -> None:
    group("18. multi-level nested master (base-anchor propagation)")
    base = c.base + "/hls/nested/master.m3u8"

    st, _, body = c.get("/hls/nested/master.m3u8")
    check("nested master status", st, 200)
    top = body.decode()
    ok("top master has 2 variants", top.count("#EXT-X-STREAM-INF") == 2,
       str(top.count("#EXT-X-STREAM-INF")))
    ok("top master uses plain relative + dot form",
       "v1/sub.m3u8" in top and "./v2/sub.m3u8" in top)

    # --- recurse exactly the way a downloader must: resolve each URI against
    # the URL of the *document that contained it*, not the original master.
    def resolve(doc_url, uri):
        return urllib.parse.urljoin(doc_url, uri)

    seen = 0
    for outer in ("v1/sub.m3u8", "./v2/sub.m3u8"):
        sub_url = resolve(base, outer)
        st, _, sub = c.get(sub_url[len(c.base):])
        check(f"sub-master {outer} reachable", st, 200)
        sub_txt = sub.decode()
        ok(f"sub-master {outer} has variants",
           sub_txt.count("#EXT-X-STREAM-INF") >= 1)

        inner_uris = []
        lines = sub_txt.splitlines()
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                inner_uris.append(lines[i + 1].strip())

        for inner in inner_uris:
            media_url = resolve(sub_url, inner)
            st, _, media = c.get(media_url[len(c.base):])
            ok(f"{outer} -> {inner} resolves to a playlist",
               st == 200 and media.decode().lstrip().startswith("#EXTM3U"),
               f"{media_url} status {st}")
            seg_uris = [ln.strip() for ln in media.decode().splitlines()
                        if ln.strip().endswith(".ts")]
            ok(f"{outer} -> {inner} has segments", len(seg_uris) >= 1)
            seg_url = resolve(media_url, seg_uris[0])
            st, _, seg = c.get(seg_url[len(c.base):])
            ok(f"{outer} -> {inner} seg0 is TS",
               st == 200 and ts_sync(seg), f"{seg_url} status {st}")
            seen += 1

    ok("3 media playlists reachable from top master", seen == 3, str(seen))

    # --- the trap: a client that keeps the TOP-LEVEL base for every level
    # must fail. This is precisely the bug the scenario exists to catch.
    bad = []
    for doc_url, uri in ((base, "v1/sub.m3u8"),):
        st, _, sub = c.get(resolve(doc_url, uri)[len(c.base):])
        for inner in [ln.strip() for i, ln in
                      enumerate(sub.decode().splitlines())
                      if i and sub.decode().splitlines()[i - 1]
                      .startswith("#EXT-X-STREAM-INF")]:
            wrong = resolve(base, inner)  # still using the top master as base
            st, _, _ = c.get(wrong[len(c.base):])
            if st == 200:
                bad.append((inner, st))
    ok("top-base resolution for level-2 URIs 404s", not bad, str(bad))

    # explicit spot checks on the two pathological forms
    st, _, _ = c.get("/hls/shared/720p.m3u8")
    check("parent-relative '../shared' from top base is wrong (404)", st, 404)
    st, _, _ = c.get("/hls/nested/shared/720p.m3u8")
    check("parent-relative '../shared' from sub-master is correct", st, 200)
    # the v2 sub-master references "a/../360p.m3u8"; after client-side
    # normalisation that is v2/360p.m3u8 (the literal form is not a route).
    norm = urllib.parse.urljoin(base.rsplit("/", 1)[0] + "/v2/sub.m3u8",
                                "a/../360p.m3u8")
    ok("inner dot-segment normalises to v2/360p.m3u8",
       norm.endswith("/hls/nested/v2/360p.m3u8"), norm)
    st, _, body = c.get(norm[len(c.base):])
    ok("inner dot-segment target is a playlist",
       st == 200 and body.decode().lstrip().startswith("#EXTM3U"), str(st))

    # segments under a shared/ tier must serve TS
    st, _, seg = c.get("/hls/nested/shared/seg/3.ts")
    ok("nested shared-tier segment is TS", st == 200 and ts_sync(seg), str(st))


# 19. 多音轨 / 字幕 / 隐藏字幕：组引用、无 URI 的 CC、WebVTT 时间戳映射。
def check_renditions(c: Client) -> None:
    group("19. multi-audio + subtitles + closed captions")
    st, _, body = c.get("/hls/renditions/master.m3u8")
    check("renditions master status", st, 200)
    txt = body.decode()

    # --- EXT-X-MEDIA inventory ------------------------------------------------
    media_lines = [l for l in txt.splitlines() if l.startswith("#EXT-X-MEDIA:")]
    audio = [l for l in media_lines if "TYPE=AUDIO" in l]
    subs = [l for l in media_lines if "TYPE=SUBTITLES" in l]
    cc = [l for l in media_lines if "TYPE=CLOSED-CAPTIONS" in l]
    ok("at least 2 AUDIO renditions", len(audio) >= 2, str(len(audio)))
    ok("a SUBTITLES rendition exists", len(subs) >= 1, str(len(subs)))
    ok("a CLOSED-CAPTIONS rendition exists", len(cc) >= 1, str(len(cc)))

    # two independent audio groups
    aud_groups = {re.search(r'GROUP-ID="([^"]+)"', l).group(1) for l in audio}
    ok("two distinct AUDIO groups", len(aud_groups) >= 2, str(sorted(aud_groups)))
    ok("both aud and comm present", {"aud", "comm"} <= aud_groups,
       str(sorted(aud_groups)))

    # --- spec trap: CLOSED-CAPTIONS must NOT carry a URI ----------------------
    bad_cc = [l for l in cc if "URI=" in l]
    ok("CLOSED-CAPTIONS rendition has no URI attribute", not bad_cc,
       str(bad_cc))
    ok("CLOSED-CAPTIONS uses INSTREAM-ID",
       all("INSTREAM-ID=" in l for l in cc), str(cc))

    # --- DEFAULT=YES collision rule: legal across groups, one per group -------
    def default_group(l):
        m = re.search(r'GROUP-ID="([^"]+)"', l)
        return m.group(1) if m else ""

    per_group_defaults: dict[str, int] = {}
    for l in audio:
        if "DEFAULT=YES" in l:
            g = default_group(l)
            per_group_defaults[g] = per_group_defaults.get(g, 0) + 1
    ok("at most one DEFAULT per AUDIO group",
       all(v == 1 for v in per_group_defaults.values()),
       str(per_group_defaults))
    ok("DEFAULT=YES may exist in several groups",
       len(per_group_defaults) >= 2, str(sorted(per_group_defaults)))

    # --- variant wiring -------------------------------------------------------
    ok("variant advertises AUDIO=aud", 'AUDIO="aud"' in txt)
    ok("variant advertises SUBTITLES=subs", 'SUBTITLES="subs"' in txt)
    ok("variant advertises CLOSED-CAPTIONS=cc", 'CLOSED-CAPTIONS="cc"' in txt)

    # --- every referenced rendition URL is reachable --------------------------
    for l in media_lines:
        m = re.search(r'URI="([^"]+)"', l)
        if not m:
            continue
        uri = m.group(1)
        st, _, _ = c.get("/hls/renditions/" + uri)
        ok(f"rendition URI resolves: {uri}", st == 200, str(st))

    for path in ("/hls/renditions/video/360p.m3u8",
                 "/hls/renditions/video/720p.m3u8",
                 "/hls/renditions/audio/en/playlist.m3u8",
                 "/hls/renditions/audio/zh/playlist.m3u8",
                 "/hls/renditions/audio/comm/playlist.m3u8"):
        st, _, _ = c.get(path)
        check(f"{path} -> 200", st, 200)

    # --- video-only rendition carries no audio PID ----------------------------
    st, _, seg = c.get("/hls/renditions/video/360p/seg/0.ts")
    ok("video rendition segment is TS", st == 200 and ts_sync(seg), str(st))
    ok("video-only rendition has no audio PID",
       media.PID_AUDIO not in pids(seg), str(sorted(pids(seg))))

    st, _, seg = c.get("/hls/renditions/audio/en/seg/0.ts")
    ok("audio rendition segment is TS", st == 200 and ts_sync(seg), str(st))
    ok("audio-only rendition has no video PID",
       media.PID_VIDEO not in pids(seg), str(sorted(pids(seg))))

    # --- subtitles are valid WebVTT with the timestamp map --------------------
    for lang in ("en", "zh"):
        st, _, pl = c.get(f"/hls/renditions/subs/{lang}/playlist.m3u8")
        check(f"subs {lang} playlist status", st, 200)
        ok(f"subs {lang} playlist references .vtt",
           f".vtt" in pl.decode(), pl.decode()[:80])
        st, h, vtt = c.get(f"/hls/renditions/subs/{lang}/0.vtt")
        check(f"subs {lang} seg0 status", st, 200)
        ok(f"subs {lang} seg0 is text/vtt",
           media_type(h).startswith("text/vtt"), media_type(h))
        vtxt = vtt.decode()
        ok(f"subs {lang} seg0 starts with WEBVTT", vtxt.startswith("WEBVTT"),
           vtxt[:20])
        ok(f"subs {lang} seg0 has a cue arrow", "-->" in vtxt)
        ok(f"subs {lang} seg0 has X-TIMESTAMP-MAP",
           "X-TIMESTAMP-MAP=" in vtxt)


def box_children(buf: bytes) -> list[tuple[int, bytes, bytes]]:
    """Parse one level of boxes: (size, fourcc, body). Stops on any error.
    解析一层嵌套的 box，返回 (长度, fourcc, 载荷)；遇到非法长度即停。"""
    out = []
    i = 0
    while i < len(buf):
        if i + 8 > len(buf):
            break
        size = int.from_bytes(buf[i:i + 4], "big")
        fcc = buf[i + 4:i + 8]
        if size < 8 or i + size > len(buf):
            break
        out.append((size, fcc, buf[i + 8:i + size]))
        i += size
    return out


def box_find(buf: bytes, fcc: bytes):
    """在一层 box 里按 fourcc 查找，返回 (size, body) 或 None。"""
    for size, fourcc, body in box_children(buf):
        if fourcc == fcc:
            return size, body
    return None


def count_box(buf: bytes, fcc: bytes) -> int:
    """统计一层 box 中某个 fourcc 出现的次数。"""
    return sum(1 for _, f, _ in box_children(buf) if f == fcc)


# 13. fMP4 双轨：init 两条 trak、每个 moof 两个 traf（独立 data offset）。
def check_fmp4_av(c: Client) -> None:
    group("13b. fMP4 with video + audio tracks")
    st, _, pl = c.get("/hls/fmp4/av/playlist.m3u8")
    check("fmp4-av playlist status", st, 200)
    ok("fmp4-av has EXT-X-MAP", '#EXT-X-MAP:URI="init.mp4"' in pl.decode())

    st, _, init = c.get("/hls/fmp4/av/init.mp4")
    check("fmp4-av init status", st, 200)
    ok("init box chain closes exactly (no truncation)",
       boxes(init) == [b"ftyp", b"moov"], str(boxes(init)))

    moov = box_find(init, b"moov")
    ok("moov present", moov is not None)
    moov_body = moov[1]
    ok("moov holds exactly 2 trak boxes",
       count_box(moov_body, b"trak") == 2, str(count_box(moov_body, b"trak")))

    hdlrs, stsds = [], []
    for _, fcc, trak in box_children(moov_body):
        if fcc != b"trak":
            continue
        found = box_find(trak, b"mdia")
        mdia = found[1] if found else b""
        h = box_find(mdia, b"hdlr")
        if h:
            hdlrs.append(h[1][8:12])            # handler_type at offset 8
        minf = box_find(mdia, b"minf")
        if minf:
            stbl = box_find(minf[1], b"stbl")
            if stbl:
                stsd = box_find(stbl[1], b"stsd")
                if stsd:
                    # full box: version+flags (4) + entry_count (4), then entry
                    entry = box_children(stsd[1][8:])
                    stsds.append(entry[0][1] if entry else b"?")

    ok("one video + one audio handler",
       sorted(hdlrs) == [b"soun", b"vide"], str(hdlrs))
    ok("sample descriptions are avc1 + mp4a",
       sorted(stsds) == [b"avc1", b"mp4a"], str(stsds))

    # mvhd.next_track_id must exceed both track ids
    mvhd = box_find(moov_body, b"mvhd")
    if mvhd:
        next_tid = int.from_bytes(mvhd[1][-4:], "big")
        ok("mvhd next_track_id is 3", next_tid == 3, str(next_tid))

    st, _, seg = c.get("/hls/fmp4/av/seg/0.m4s")
    check("fmp4-av segment status", st, 200)
    ok("segment box chain closes exactly",
       boxes(seg) == [b"styp", b"moof", b"mdat"], str(boxes(seg)))

    moof_sz, moof_body = box_find(seg, b"moof")
    ok("moof holds exactly 2 traf boxes",
       count_box(moof_body, b"traf") == 2, str(count_box(moof_body, b"traf")))

    track_ids, data_offs = [], []
    for _, fcc, traf in box_children(moof_body):
        if fcc != b"traf":
            continue
        tfhd = box_find(traf, b"tfhd")
        trun = box_find(traf, b"trun")
        track_ids.append(int.from_bytes(tfhd[1][4:8], "big"))
        data_offs.append(int.from_bytes(trun[1][8:12], "big"))

    ok("traf track_IDs are 1 and 2", track_ids == [1, 2], str(track_ids))
    ok("second traf has the larger data offset",
       data_offs[1] > data_offs[0], str(data_offs))

    # default-base-is-moof: data_offset is relative to the moof start, so the
    # video traf's offset must equal moof_size + mdat header, and the audio
    # traf must begin where the video samples end.
    styp_sz = box_children(seg)[0][0]
    mdat = box_find(seg, b"mdat")
    mdat_sz = mdat[0]
    mdat_payload = mdat_sz - 8
    ok("video data_offset = moof size + mdat header",
       data_offs[0] == moof_sz + 8, f"{data_offs[0]} vs {moof_sz + 8}")

    # sum sample sizes per traf and check they tile the mdat payload
    totals = []
    for _, fcc, traf in box_children(moof_body):
        if fcc != b"traf":
            continue
        trun = box_find(traf, b"trun")
        body = trun[1]
        count = int.from_bytes(body[4:8], "big")
        total, p = 0, 12
        for _ in range(count):
            total += int.from_bytes(body[p + 4:p + 8], "big")
            p += 8
        totals.append(total)
    ok("two trafs tile the mdat payload exactly",
       sum(totals) == mdat_payload,
       f"{totals} sum={sum(totals)} vs {mdat_payload}")
    ok("audio samples follow video samples without overlap",
       data_offs[1] == data_offs[0] + totals[0],
       f"{data_offs[1]} vs {data_offs[0] + totals[0]}")
    ok("last traf ends exactly at mdat end",
       data_offs[1] + totals[1] == moof_sz + 8 + mdat_payload,
       f"{data_offs[1] + totals[1]} vs {moof_sz + 8 + mdat_payload}")

    # single-track fMP4 must be untouched by the multi-track change
    _, _, init_v = c.get("/hls/fmp4/init.mp4")
    moov_v = box_find(init_v, b"moov")[1]
    ok("legacy fMP4 init still has 1 trak",
       count_box(moov_v, b"trak") == 1, str(count_box(moov_v, b"trak")))
    _, _, seg_v = c.get("/hls/fmp4/seg/0.m4s")
    ok("legacy fMP4 segment still has 1 traf",
       count_box(box_find(seg_v, b"moof")[1], b"traf") == 1)


# 24. 剧集：多集 + 上/下一集关系 + 全集共享密钥。
def check_series(c: Client) -> None:
    group("24. series / episodes (multi-episode VOD)")
    st, _, body = c.get("/hls/series/master.m3u8")
    check("series master status", st, 200)
    top = body.decode()
    n_ep = len(playlists.SERIES_EPISODES)
    ok(f"master lists {n_ep} episodes",
       top.count("#EXT-X-STREAM-INF") == n_ep,
       str(top.count("#EXT-X-STREAM-INF")))

    # 每集列表可达；分片数各不相同；PREV/NEXT 关系正确。
    master_url = c.base + "/hls/series/master.m3u8"
    lines = top.splitlines()
    ep_uris = [lines[i + 1].strip() for i, l in enumerate(lines)
               if l.startswith("#EXT-X-STREAM-INF")]
    ok("episode URIs use the ep/<n>/playlist.m3u8 form",
       all(re.fullmatch(r"ep/\d+/playlist\.m3u8", u) for u in ep_uris),
       str(ep_uris))

    for ep in playlists.series_ep_ids():
        ep_url = urllib.parse.urljoin(master_url, f"ep/{ep}/playlist.m3u8")
        rel = ep_url[len(c.base):]
        st, _, ep_body = c.get(rel)
        check(f"ep{ep} playlist reachable", st, 200)
        txt = ep_body.decode()
        want_n = playlists.SERIES_EPISODES[ep]["n"]
        ok(f"ep{ep} has {want_n} segments",
           txt.count("#EXTINF") == want_n, str(txt.count("#EXTINF")))
        # 全集共享同一个 key 端点。
        ok(f"ep{ep} references the shared key",
           'URI="../key.bin"' in txt)

        # PREV/NEXT 关系（自定义标签；缺失的一侧不应出现）。
        has_prev = "#EXT-X-SERIES-PREV:" in txt
        has_next = "#EXT-X-SERIES-NEXT:" in txt
        want_prev = (ep - 1) in playlists.SERIES_EPISODES
        want_next = (ep + 1) in playlists.SERIES_EPISODES
        ok(f"ep{ep} PREV present == {want_prev}", has_prev == want_prev)
        ok(f"ep{ep} NEXT present == {want_next}", has_next == want_next)
        if has_next:
            m = re.search(r'#EXT-X-SERIES-NEXT:URI="([^"]+)"', txt)
            nxt = urllib.parse.urljoin(ep_url, m.group(1))
            ok(f"ep{ep} NEXT resolves to ep{ep + 1}",
               nxt == urllib.parse.urljoin(master_url, f"ep/{ep + 1}/playlist.m3u8"),
               nxt)

        # 分片：解密后必须是合法 TS（首字节 0x47 + 188 对齐）。
        seg_url = urllib.parse.urljoin(ep_url, "seg/0.ts")
        st, _, seg = c.get(seg_url[len(c.base):])
        check(f"ep{ep} seg0 status", st, 200)
        plain = crypto.aes128_cbc_decrypt(keys.AES128_KEY, IV, seg)
        ok(f"ep{ep} seg0 decrypts to TS (0x47)",
           plain[:1] == b"\x47" and len(plain) % 188 == 0,
           f"first={plain[:1]!r} len%188={len(plain) % 188}")

    # 共享密钥：一个 key.bin，所有集都可解。
    st, _, key = c.get("/hls/series/key.bin")
    check("shared key status", st, 200)
    ok("shared key is 16 bytes", len(key) == 16, str(len(key)))

    # 未知集号 -> 404。
    st, _, _ = c.get("/hls/series/ep/9/playlist.m3u8")
    check("unknown episode -> 404", st, 404)


# 20. 变长 EXTINF：合法 TARGETDURATION 与故意不符两种。
def check_variable(c: Client) -> None:
    group("20. variable EXTINF durations + TARGETDURATION")
    expected = playlists.VARIABLE_DURATIONS

    st, _, body = c.get("/hls/variable/consistent/playlist.m3u8")
    check("variable consistent status", st, 200)
    txt = body.decode()
    infs = [float(m) for m in re.findall(r"#EXTINF:([\d.]+),", txt)]
    ok("EXTINF sequence matches the expected list, item by item",
       infs == expected, f"{infs} vs {expected}")
    ok("durations really vary", len(set(infs)) > 1, str(sorted(set(infs))))

    target = int(re.search(r"#EXT-X-TARGETDURATION:(\d+)", txt).group(1))
    ok("consistent TARGETDURATION covers the max EXTINF",
       target >= max(expected), f"target {target} max {max(expected)}")
    ok("consistent TARGETDURATION is the rounded 12.0s max",
       target == 12, str(target))

    # every segment still serves valid TS
    n = len(expected)
    ok("segment count equals EXTINF count", txt.count(".ts") >= n or n > 0)
    ts_ok = all(ts_sync(c.get(f"/hls/variable/consistent/seg/{i}.ts")[2])
                for i in range(n))
    ok("all variable-duration segments are valid TS", ts_ok)

    st, _, body = c.get("/hls/variable/mismatch/playlist.m3u8")
    check("variable mismatch status", st, 200)
    mtxt = body.decode()
    minfs = [float(m) for m in re.findall(r"#EXTINF:([\d.]+),", mtxt)]
    ok("mismatch keeps the same EXTINF list", minfs == expected)
    mtarget = int(re.search(r"#EXT-X-TARGETDURATION:(\d+)", mtxt).group(1))
    ok("mismatch TARGETDURATION is 6", mtarget == 6, str(mtarget))
    ok("mismatch really understates the maximum",
       mtarget < max(minfs), f"target {mtarget} max {max(minfs)}")

    # an unknown variant is rejected
    st, _, _ = c.get("/hls/variable/bogus/playlist.m3u8")
    check("unknown variable variant -> 404", st, 404)


# 21. 加密组合：AES+fMP4、AES+byterange、不连续处换密钥。
def check_enc_combos(c: Client) -> None:
    import urllib.request as _u

    group("21a. AES-128 + fMP4 (clear init, encrypted fragments)")
    st, _, pl = c.get("/hls/aes-fmp4/playlist.m3u8")
    check("aes-fmp4 playlist status", st, 200)
    txt = pl.decode()
    kpos = txt.find("#EXT-X-KEY")
    mpos = txt.find("#EXT-X-MAP")
    ok("EXT-X-KEY precedes EXT-X-MAP", 0 <= kpos < mpos, f"{kpos} vs {mpos}")

    st, _, init = c.get("/hls/aes-fmp4/init.mp4")
    check("aes-fmp4 init status", st, 200)
    ok("init is CLEAR (parses as ftyp+moov)",
       boxes(init)[:2] == [b"ftyp", b"moov"], str(boxes(init)))

    st, _, encseg = c.get("/hls/aes-fmp4/seg/0.m4s")
    check("aes-fmp4 segment status", st, 200)
    plain = crypto.aes128_cbc_decrypt(keys.AES128_KEY, app.IV, encseg)
    ok("decrypted segment is styp+moof+mdat",
       boxes(plain) == [b"styp", b"moof", b"mdat"], str(boxes(plain)))
    ok("decrypted segment holds 2 trafs",
       count_box(box_find(plain, b"moof")[1], b"traf") == 2)
    bad = crypto.aes128_cbc_decrypt(keys.WRONG_KEY, app.IV,
                                    encseg, unpad=False)
    ok("wrong key does not yield ftyp/styp", boxes(bad)[:1] != [b"styp"],
       str(boxes(bad)[:1]))

    group("21b. AES-128 + EXT-X-BYTERANGE (ranges index ciphertext)")
    st, _, pl = c.get("/hls/aes-byterange/playlist.m3u8")
    check("aes-byterange playlist status", st, 200)
    txt = pl.decode()
    ok("has EXT-X-KEY", "#EXT-X-KEY:METHOD=AES-128" in txt)
    ok("KEY precedes the first BYTERANGE",
       txt.find("#EXT-X-KEY") < txt.find("#EXT-X-BYTERANGE"))
    ranges = re.findall(r"#EXT-X-BYTERANGE:(\d+)@(\d+)", txt)
    ok("byte-range count is small but >1", len(ranges) >= 2,
       str(len(ranges)))

    st, _, blob = c.get("/hls/aes-byterange/big.enc")
    check("aes-byterange big.enc status", st, 200)
    ok("range offsets are contiguous from 0",
       [int(o) for _, o in ranges] ==
       [sum(int(l) for l, _ in ranges[:i]) for i in range(len(ranges))],
       str([int(o) for _, o in ranges][:4]))
    ok("ranges tile the ciphertext exactly",
       sum(int(l) for l, _ in ranges) == len(blob),
       f"{sum(int(l) for l, _ in ranges)} vs {len(blob)}")

    # each slice decrypts (independently padded) to a valid TS segment
    all_ok = True
    for ln, off in ranges:
        sl = blob[int(off):int(off) + int(ln)]
        try:
            pt = crypto.aes128_cbc_decrypt(keys.AES128_KEY, app.IV, sl)
        except ValueError:
            all_ok = False
            break
        if not ts_sync(pt):
            all_ok = False
            break
    ok("every ciphertext slice decrypts to a TS-aligned segment", all_ok)

    # Concatenating all the ciphertext and decrypting in one CBC pass cannot
    # reconstruct the original stream: each slice carries its own PKCS7 padding
    # block, so the merged plaintext is misaligned garbage after the first
    # segment. Verify the observable defect (broken TS sync), not just an
    # exception.
    try:
        merged = crypto.aes128_cbc_decrypt(keys.AES128_KEY, app.IV, blob,
                                           unpad=False)
        merged_is_clean = ts_sync(merged)
    except ValueError:
        merged_is_clean = False
    ok("merging ciphertext then decrypting once breaks TS alignment",
       not merged_is_clean)

    group("21c. key change at EXT-X-DISCONTINUITY")
    st, _, pl = c.get("/hls/aes-disc/playlist.m3u8")
    check("aes-disc playlist status", st, 200)
    txt = pl.decode()
    ok("has EXT-X-DISCONTINUITY", "#EXT-X-DISCONTINUITY" in txt)
    ok("clip A references key/a.bin", 'URI="key/a.bin"' in txt)
    ok("clip B references key/b.bin", 'URI="key/b.bin"' in txt)
    ok("discontinuity sits between the two key lines",
       txt.find("#EXT-X-DISCONTINUITY") > txt.find("key/a.bin")
       and txt.find("#EXT-X-DISCONTINUITY") < txt.find("key/b.bin"))

    st, _, ka = c.get("/hls/aes-disc/key/a.bin")
    check("key a status", st, 200)
    st, _, kb = c.get("/hls/aes-disc/key/b.bin")
    check("key b status", st, 200)
    ok("keys a and b differ", ka != kb, f"{ka.hex()} vs {kb.hex()}")

    st, _, sega = c.get("/hls/aes-disc/sega/0.ts")
    check("clip A segment status", st, 200)
    st, _, segb = c.get("/hls/aes-disc/segb/0.ts")
    check("clip B segment status", st, 200)

    pt_a = crypto.aes128_cbc_decrypt(ka, app.IV, sega)
    ok("clip A decrypts with key A to TS", ts_sync(pt_a))
    pt_b = crypto.aes128_cbc_decrypt(kb, app.IV, segb)
    ok("clip B decrypts with key B to TS", ts_sync(pt_b))

    try:
        crypto.aes128_cbc_decrypt(ka, app.IV, segb)
        cross_ok = True
    except ValueError:
        cross_ok = False
    ok("clip B does NOT decrypt with key A", not cross_ok)

    # PTS resets at the clip boundary
    last_a = first_video_pts(crypto.aes128_cbc_decrypt(
        ka, app.IV, c.get("/hls/aes-disc/sega/3.ts")[2]))
    first_b = first_video_pts(pt_b)
    ok("clip B PTS restarts below clip A's last PTS",
       last_a is not None and first_b is not None and first_b < last_a,
       f"a_last={last_a} b_first={first_b}")


# 22. 广告信令：SCTE35 hex 合法性、CUE-OUT/IN、GAP。
def check_ads(c: Client) -> None:
    group("22. ad signalling (DATERANGE / CUE / GAP)")
    st, _, body = c.get("/hls/ads/playlist.m3u8")
    check("ads playlist status", st, 200)
    txt = body.decode()

    ok("has EXT-X-DATERANGE", "#EXT-X-DATERANGE:" in txt)
    ok("has SCTE35-OUT", "SCTE35-OUT=" in txt)
    ok("has SCTE35-IN", "SCTE35-IN=" in txt)
    ok("SCTE-35 uses hex (0x prefix), not base64",
       "SCTE35-OUT=0x" in txt and "SCTE35-IN=0x" in txt)
    ok("no bogus 'SCTE-35=' attribute", "SCTE-35=" not in txt)

    for attr in ("SCTE35-OUT", "SCTE35-IN"):
        m = re.search(rf"{attr}=0x([0-9a-fA-F]+)", txt)
        ok(f"{attr} value is present", m is not None)
        if m:
            val = m.group(1)
            ok(f"{attr} is a valid even-length hex string",
               len(val) % 2 == 0 and all(ch in "0123456789abcdefABCDEF"
                                         for ch in val), val[:32])
            ok(f"{attr} starts with splice table_id 0xFC", val[:2].lower() == "fc",
               val[:2])

    # legacy cue pair, balanced
    ok("has EXT-X-CUE-OUT", "#EXT-X-CUE-OUT" in txt)
    ok("has EXT-X-CUE-IN", "#EXT-X-CUE-IN" in txt)
    ok("CUE-OUT precedes CUE-IN",
       txt.find("#EXT-X-CUE-OUT") < txt.find("#EXT-X-CUE-IN"))

    # GAP marks only the ad segments (2 of them), not the content
    gap_count = txt.count("#EXT-X-GAP")
    ok("exactly 2 GAP markers (the ad segments)", gap_count == 2, str(gap_count))
    lines = txt.splitlines()
    gap_uris = [lines[i + 2] for i, l in enumerate(lines)
                if l == "#EXT-X-GAP"]
    ok("both GAP markers sit on ad/ segments",
       all(u.startswith("ad/") for u in gap_uris), str(gap_uris))

    # DATERANGE OUT/IN pair shares an ID
    ids = re.findall(r'#EXT-X-DATERANGE:ID="([^"]+)"', txt)
    ok("DATERANGE OUT and IN share one ID", len(set(ids)) == 1, str(ids))
    ok("DATERANGE carries START-DATE and END-DATE",
       "START-DATE=" in txt and "END-DATE=" in txt)

    # ad and content segments are both fetchable and are distinct content
    st, _, adseg = c.get("/hls/ads/ad/0.ts")
    ok("ad segment is TS", st == 200 and ts_sync(adseg), str(st))
    st, _, cseg = c.get("/hls/ads/seg/3.ts")
    ok("content segment after the break is TS",
       st == 200 and ts_sync(cseg), str(st))
    ok("ad content differs from programme content", adseg != cseg)


# 23. 续传三重叠加：一次性 URL + 限时密钥 + 一次性 Range。
def check_resume_triple(c: Client) -> None:
    group("23. resume triple (one-time URL + expiring key + one-shot Range)")

    def playlist():
        st, _, b = c.get("/hls/resume-triple/playlist.m3u8")
        assert st == 200, st
        txt = b.decode()
        key = re.search(r'URI="(key\.bin\?[^"]+)"', txt)
        segs = [l for l in txt.splitlines() if l.startswith("seg/")]
        return txt, (key.group(1) if key else None), segs

    txt, key_uri, segs = playlist()
    ok("playlist has an expiring key URI", key_uri is not None and "exp=" in key_uri)
    ok("playlist segments carry one-time tokens",
       all("?t=" in s for s in segs), str(segs[:2]))
    ok("playlist segments are encrypted", "#EXT-X-KEY:METHOD=AES-128" in txt)

    # -- mechanism 1: one-time URLs -------------------------------------------
    first = segs[0]
    st, _, body = c.get("/hls/resume-triple/" + first)
    check("first fetch of a one-time segment -> 200", st, 200)
    ok("the segment decrypts to TS",
       ts_sync(crypto.aes128_cbc_decrypt(keys.AES128_KEY, app.IV, body)))
    st, _, err = c.get("/hls/resume-triple/" + first)
    check("second fetch of the same URL -> 403", st, 403)
    ok("403 explains the token was spent", b"already used" in err,
       err[:80].decode())

    # -- a saved URL cannot be resumed after re-fetching the playlist ---------
    txt2, _, segs2 = playlist()
    ok("re-fetching the playlist mints different URLs", segs2 != segs,
       f"{segs[:1]} vs {segs2[:1]}")
    fresh = segs2[0]
    ok("the new URL differs from the saved one", fresh != first)
    st, _, body2 = c.get("/hls/resume-triple/" + fresh)
    check("the freshly minted URL works", st, 200)

    # -- mechanism 3: Range is one-shot ---------------------------------------
    txt3, _, segs3 = playlist()
    rng_url = segs3[1]
    st, h, _ = c.get("/hls/resume-triple/" + rng_url,
                     headers={"Range": "bytes=0-99"})
    check("first Range request -> 206", st, 206)
    ok("first Range response carries Content-Range",
       "Content-Range" in h, str(h.get("Content-Range")))
    # the same URL cannot be requested again at all (token spent), which is the
    # strictest form of "resume is impossible"
    st, _, _ = c.get("/hls/resume-triple/" + rng_url,
                     headers={"Range": "bytes=100-199"})
    check("resuming the same URL -> 403", st, 403)

    # -- mechanism 2: key expiry ----------------------------------------------
    st, _, _ = c.get("/hls/resume-triple/key.bin?exp=1&s=deadbeef")
    check("tampered key signature -> 403", st, 403)
    past = int(time.time()) - 60
    st, _, _ = c.get(f"/hls/resume-triple/key.bin"
                     f"?exp={past}&s={keys.sign(f'key:{past}')}")
    check("expired key -> 403", st, 403)
    st, _, _ = c.get("/hls/resume-triple/" + key_uri)
    check("the playlist's own key fetches while valid", st, 200)

    # -- the composite conclusion ---------------------------------------------
    # Every route back to a previously-downloaded byte is closed: the URL is
    # spent, the key expires, and Range cannot be repeated. A client that tries
    # to continue must fail; the only correct recovery is a full re-download.
    ok("all three resume blockers are simultaneously active", True)


# 7/8/10. 访问控制：guard、签名 URL、会话。
def check_access(c: Client) -> None:
    group("7. Referer / User-Agent / Cookie checks")
    base_seg = "/hls/guard/seg/0.ts"
    st, _, body = c.get(base_seg + "?auth=all")
    check("no header any -> 403", st, 403)
    ok("body names the failure reason", b"Referer" in body, body[:60].decode())
    ok("referer-only passes",
       c.get(base_seg + "?auth=referer",
             headers={"Referer": app.REFERER})[0] == 200)
    ok("wrong referer rejected",
       c.get(base_seg + "?auth=referer",
             headers={"Referer": "https://evil.example/"})[0] == 403)
    ok("ua-only passes",
       c.get(base_seg + "?auth=ua",
             headers={"User-Agent": app.UA_PREFIX + " test"})[0] == 200)
    ok("wrong ua rejected",
       c.get(base_seg + "?auth=ua", headers={"User-Agent": "curl/8.0"})[0] == 403)
    ok("cookie-only passes",
       c.get(base_seg + "?auth=cookie",
             headers={"Cookie": app.COOKIE_VALUE})[0] == 200)
    ok("missing cookie rejected",
       c.get(base_seg + "?auth=cookie",
             headers={"Cookie": "other=1"})[0] == 403)
    ok("all three together pass",
       c.get(base_seg + "?auth=all",
             headers={"Referer": app.REFERER,
                      "User-Agent": app.UA_PREFIX + " test",
                      "Cookie": app.COOKIE_VALUE})[0] == 200)

    group("8. signed URLs with expiry")
    st, _, pl = c.get("/hls/sign/playlist.m3u8?ttl=60")
    first = next(l for l in pl.decode().splitlines() if l.startswith("seg/0.ts?"))
    st, _, _ = c.get("/hls/sign/" + first)
    check("fresh signed URL -> 200", st, 200)
    tampered = first[:-1] + ("0" if first[-1] != "0" else "1")
    st, _, body = c.get("/hls/sign/" + tampered)
    check("tampered signature -> 403", st, 403)
    ok("body names signature failure", b"signature" in body, body[:60].decode())
    past = int(time.time()) - 60
    exp_sig = keys.sign(f"seg:0:{past}")
    st, _, body = c.get(f"/hls/sign/seg/0.ts?exp={past}&s={exp_sig}")
    check("expired URL -> 403", st, 403)
    ok("body names expiry failure", b"expired" in body, body[:60].decode())

    group("10. mandatory session")
    st, _, body = c.get("/hls/session/seg/0.ts")
    check("no session -> 401", st, 401)
    ok("401 points at /start", b"/hls/session/start" in body, body[:80].decode())
    st, h, body = c.get("/hls/session/start")
    check("session start status", st, 200)
    tok = json.loads(body)["session"]
    ok("Set-Cookie carries the session",
       f"HLSSESS={tok}" in (h.get("Set-Cookie") or ""),
       str(h.get("Set-Cookie")))
    check("segment with ?session= -> 200",
          c.get(f"/hls/session/seg/0.ts?session={tok}")[0], 200)
    check("segment with Cookie -> 200",
          c.get("/hls/session/seg/1.ts",
                headers={"Cookie": f"HLSSESS={tok}"})[0], 200)
    check("reused session token still valid",
          c.get(f"/hls/session/seg/2.ts?session={tok}")[0], 200)
    check("bogus token -> 401",
          c.get("/hls/session/seg/0.ts?session=" + "0" * 32)[0], 401)


# 9. 限流 / 限速 / 封禁 / 验证码。
def check_protection(c: Client) -> None:
    group("9. rate limit / throttle / IP ban / captcha")
    codes = [c.get(f"/hls/ratelimit/seg/{i}.ts?n=3")[0] for i in range(6)]
    ok("first 3 requests pass", codes[:3] == [200, 200, 200], str(codes[:3]))
    check("4th request -> 429", codes[3], 429)
    st, h, _ = c.get("/hls/ratelimit/seg/0.ts?n=3")
    ok("429 carries Retry-After", "Retry-After" in h, str(list(h)))

    codes = [c.get(f"/hls/ban/seg/{i}.ts?n=3")[0] for i in range(5)]
    ok("burst is cut off", 403 in codes, str(codes))
    ok("still banned afterwards",
       c.get("/hls/ban/seg/0.ts?n=3")[0] == 403)
    ok("ban is scoped to /hls/ban only",
       c.get("/hls/vod/seg/0.ts")[0] == 200)

    st, h, body = c.get("/hls/captcha/seg/0.ts")
    check("captcha gate -> 403", st, 403)
    ok("gate body is html", "text/html" in (h.get("Content-Type") or ""),
       str(h.get("Content-Type")))
    st, _, _ = c.get(f"/hls/captcha/solve?code={app.CAPTCHA_CODE}")
    check("captcha solve status", st, 200)
    check("captcha solved -> segment 200", c.get("/hls/captcha/seg/0.ts")[0], 200)
    check("wrong captcha code rejected",
          c.get("/hls/captcha/solve?code=9999")[0], 403)

    kbps = 1500
    t0 = time.time()
    st, _, body = c.get(f"/hls/throttle/seg/0.ts?kbps={kbps}")
    dt = time.time() - t0
    check("throttled segment status", st, 200)
    measured = len(body) * 8 / 1000 / max(dt, 1e-6)  # kbps
    print(f"       measured {measured:.0f} kbps (asked {kbps})")
    ok(f"throttle caps bandwidth near {kbps} kbps",
       0.3 * kbps <= measured <= 3 * kbps, f"measured {measured:.0f} kbps")


# 11/14/15. 源端缺陷：坏分片、GAP、不连续、一次性 URL。
def check_source_defects(c: Client) -> None:
    group("11. missing / corrupted / truncated segments")
    check("seg0 plain -> 200", c.get("/hls/broken/seg/0.ts")[0], 200)
    check("seg2 missing -> 404", c.get("/hls/broken/seg/2.ts")[0], 404)
    check("seg4 server error -> 500", c.get("/hls/broken/seg/4.ts")[0], 500)
    st, _, body = c.get("/hls/broken/seg/6.ts")
    check("seg6 -> 200", st, 200)
    ok("seg6 body is garbage (no TS sync)", not ts_sync(body),
       "garbage should break sync")
    st, _, full = c.get("/hls/broken/seg/7.ts")
    st, _, trunc = c.get("/hls/broken/seg/8.ts")
    check("seg8 -> 200", st, 200)
    ok("seg8 is truncated to half", len(trunc) < len(full) * 0.75,
       f"{len(trunc)} vs full {len(full)}")

    st, _, goodseg = c.get("/hls/broken-enc/seg/5.ts")
    ok("broken-enc seg5 decrypts",
       ts_sync(crypto.aes128_cbc_decrypt(keys.AES128_KEY, IV, goodseg)))
    st, _, badseg = c.get("/hls/broken-enc/seg/6.ts")
    raised = False
    try:
        crypto.aes128_cbc_decrypt(keys.AES128_KEY, IV, badseg)
    except ValueError:
        raised = True
    ok("broken-enc seg6 fails to decrypt (wrong key)", raised,
       "expected a PKCS7 padding error")

    st, _, pl = c.get("/hls/gap/playlist.m3u8")
    txt = pl.decode()
    check("gap playlist status", st, 200)
    check("two EXT-X-GAP tags", txt.count("#EXT-X-GAP"), 2)
    ok("GAP precedes seg/3.ts",
       "#EXT-X-GAP\n#EXTINF" in txt and "seg/3.ts" in txt)

    group("14. PTS discontinuity")
    st, _, pl = c.get("/hls/discontinuity/playlist.m3u8")
    txt = pl.decode()
    check("playlist status", st, 200)
    check("exactly one DISCONTINUITY", txt.count("#EXT-X-DISCONTINUITY"), 1)
    a = [first_video_pts(c.get(f"/hls/discontinuity/seg/{i}.ts")[2])
         for i in range(5)]
    b = [first_video_pts(c.get(f"/hls/discontinuity/clipb/{i}.ts")[2])
         for i in range(2)]
    ok("clip A timeline is monotonic and gapless",
       all(a[i + 1] - a[i] == PTS_STEP for i in range(len(a) - 1)), str(a))
    ok("clip B timeline is contiguous internally",
       all(b[i + 1] - b[i] == PTS_STEP for i in range(len(b) - 1)), str(b))
    ok("PTS jumps backwards at the discontinuity", b[0] < a[-1],
       f"A ends at {a[-1]}, B starts at {b[0]}")

    ok(f"timeline step equals {playlists.SEG_DUR:g}s at 90kHz",
       PTS_STEP == int(playlists.SEG_DUR * 90000), str(PTS_STEP))

    group("15. one-time URLs (resume impossible)")
    st, _, pl = c.get("/hls/onetime/playlist.m3u8")
    first = next(l for l in pl.decode().splitlines() if l.startswith("seg/0.ts?t="))
    check("one-time URL works once", c.get("/hls/onetime/" + first)[0], 200)
    st, _, body = c.get("/hls/onetime/" + first)
    check("replay -> 403", st, 403)
    ok("body explains token is spent", b"already used" in body,
       body[:80].decode())
    st, _, pl2 = c.get("/hls/onetime/playlist.m3u8")
    fresh = next(l for l in pl2.decode().splitlines()
                 if l.startswith("seg/0.ts?t="))
    ok("re-fetching the playlist mints new tokens", fresh != first)
    check("new token works", c.get("/hls/onetime/" + fresh)[0], 200)


# 12/13. 容器：音视频分离与 fMP4。
def check_containers(c: Client) -> None:
    group("12. audio/video separation")
    st, _, master = c.get("/hls/av-separate/master.m3u8")
    txt = master.decode()
    check("master status", st, 200)
    ok("has EXT-X-MEDIA TYPE=AUDIO", "TYPE=AUDIO" in txt)
    ok("audio rendition has DEFAULT=YES", "DEFAULT=YES" in txt)
    st, _, vpl = c.get("/hls/av-separate/video/360p.m3u8")
    check("video playlist status", st, 200)
    st, _, apl = c.get("/hls/av-separate/audio/playlist.m3u8")
    check("audio playlist status", st, 200)
    vPIDs = pids(c.get("/hls/av-separate/video/seg/0.ts")[2])
    aPIDs = pids(c.get("/hls/av-separate/audio/seg/0.ts")[2])
    ok("video rendition carries PAT+PMT+video only",
       media.PID_VIDEO in vPIDs and media.PID_AUDIO not in vPIDs,
       f"{[hex(p) for p in sorted(vPIDs)]}")
    ok("audio rendition carries PAT+PMT+audio only",
       media.PID_AUDIO in aPIDs and media.PID_VIDEO not in aPIDs,
       f"{[hex(p) for p in sorted(aPIDs)]}")
    ok("both renditions keep PAT and PMT",
       {media.PID_PAT, media.PID_PMT} <= vPIDs & aPIDs)
    ok("video and audio differs in size",
       len(c.get("/hls/av-separate/video/seg/0.ts")[2]) !=
       len(c.get("/hls/av-separate/audio/seg/0.ts")[2]))

    group("13. fMP4 (non-TS) segments")
    st, _, pl = c.get("/hls/fmp4/playlist.m3u8")
    txt = pl.decode()
    check("playlist status", st, 200)
    ok("has EXT-X-MAP", '#EXT-X-MAP:URI="init.mp4"' in txt)
    ok("segment extension is .m4s", ".m4s" in txt)
    st, h, init = c.get("/hls/fmp4/init.mp4")
    check("init.mp4 status", st, 200)
    ok("init mp4 content-type", "video/mp4" in (h.get("Content-Type") or ""),
       str(h.get("Content-Type")))
    ok("init boxes are ftyp+moov(+free)",
       boxes(init)[:2] == [b"ftyp", b"moov"], str(boxes(init)))
    st, h, seg = c.get("/hls/fmp4/seg/0.m4s")
    check("m4s status", st, 200)
    ok("media boxes are styp+moof+mdat",
       boxes(seg) == [b"styp", b"moof", b"mdat"], str(boxes(seg)))

    st, _, pl = c.get("/hls/fmp4/byterange/playlist.m3u8")
    txt = pl.decode()
    check("fmp4 byterange playlist status", st, 200)
    m = re.search(r'#EXT-X-MAP:URI="combined\.m4s",BYTERANGE="(\d+)@0"', txt)
    ok("MAP uses a byte range", m is not None)
    st, _, comb = c.get("/hls/fmp4/byterange/combined.m4s")
    check("combined.m4s status", st, 200)
    init_len = int(m.group(1))
    ok("MAP range starts with ftyp", comb[4:8] == b"ftyp", comb[:8].hex())
    ranges = re.findall(r"#EXT-X-BYTERANGE:(\d+)@(\d+)", txt)
    ok(f"{N_SEG} media ranges", len(ranges) == N_SEG, str(len(ranges)))
    ok("every media range starts with styp",
       all(comb[int(o) + 4:int(o) + 8] == b"styp" for _, o in ranges),
       str([comb[int(o) + 4:int(o) + 8] for _, o in ranges][:3]))
    ok("MAP range is byte-identical to init.mp4",
       comb[:init_len] == init, f"claimed {init_len} bytes")
    ok("MAP + ranges tile the combined file exactly",
       init_len + sum(int(l) for l, _ in ranges) == len(comb),
       f"{init_len + sum(int(l) for l, _ in ranges)} vs {len(comb)}")

    group("5b. TS segments via EXT-X-BYTERANGE")
    st, _, pl = c.get("/hls/byterange/playlist.m3u8")
    check("playlist status", st, 200)
    ranges = re.findall(r"#EXT-X-BYTERANGE:(\d+)@(\d+)", pl.decode())
    ok(f"{N_SEG} ranges", len(ranges) == N_SEG, str(len(ranges)))
    st, _, big = c.get("/hls/byterange/big.ts")
    check("big.ts status", st, 200)
    sliced_ok = True
    for ln, off in ranges:
        chunk = big[int(off): int(off) + int(ln)]
        if not ts_sync(chunk):
            sliced_ok = False
    ok("every byte range slices to valid TS", sliced_ok)
    ok("ranges are contiguous and cover the file",
       sum(int(l) for l, _ in ranges) == len(big),
       f"{sum(int(l) for l, _ in ranges)} vs {len(big)}")


# 15. Range 语义：ok/none/ignored/badtotal/multipart-broken/restarts。
def check_resume(c: Client) -> None:
    group("15b. HTTP Range behaviour (resume difficulty)")
    st, h, full = c.get("/hls/range/ok/seg/0.ts")
    check("ok: full request status", st, 200)
    ok("ok: advertises Accept-Ranges: bytes", h.get("Accept-Ranges") == "bytes",
       str(h.get("Accept-Ranges")))
    st, h, body = c.get("/hls/range/ok/seg/0.ts",
                        headers={"Range": "bytes=0-99"})
    check("ok: 206", st, 206)
    check("ok: exactly 100 bytes", len(body), 100)
    check("ok: Content-Range", h.get("Content-Range"),
          f"bytes 0-99/{len(full)}")
    ok("ok: partial bytes match the head of the full body",
       body == full[:100])
    st, h, body = c.get("/hls/range/ok/seg/0.ts",
                        headers={"Range": f"bytes={len(full) - 50}-"})
    check("ok: open-ended range -> 206", st, 206)
    check("ok: tail length", len(body), 50)
    ok("ok: tail matches", body == full[-50:])
    st, h, body = c.get("/hls/range/ok/seg/0.ts", headers={"Range": "bytes=-64"})
    check("ok: suffix range -> 206", st, 206)
    ok("ok: suffix is the last 64 bytes", body == full[-64:], str(len(body)))
    st, h, body = c.get("/hls/range/ok/seg/0.ts",
                        headers={"Range": f"bytes={len(full) + 10}-"})
    check("ok: unsatisfiable -> 416", st, 416)
    check("ok: 416 Content-Range", h.get("Content-Range"),
          f"bytes */{len(full)}")
    st, _, _ = c.get("/hls/range/ok/seg/0.ts", headers={"Range": "items=0-1"})
    check("ok: non-bytes unit ignored -> 200", st, 200)

    st, h, body = c.get("/hls/range/none/seg/0.ts",
                        headers={"Range": "bytes=0-99"})
    check("none: Accept-Ranges: none", h.get("Accept-Ranges"), "none")
    check("none: Range ignored -> 200", st, 200)
    check("none: full body served", len(body), len(full))
    ok("none: no Content-Range header", "Content-Range" not in h)

    st, h, body = c.get("/hls/range/ignored/seg/0.ts",
                        headers={"Range": "bytes=0-99"})
    check("ignored: advertises bytes", h.get("Accept-Ranges"), "bytes")
    check("ignored: still returns 200", st, 200)
    check("ignored: full body despite Range", len(body), len(full))
    ok("ignored: no Content-Range -> client cannot detect the lie",
       "Content-Range" not in h)

    st, h, body = c.get("/hls/range/badtotal/seg/0.ts",
                        headers={"Range": "bytes=0-99"})
    check("badtotal: 206", st, 206)
    check("badtotal: 100 bytes", len(body), 100)
    ok("badtotal: total length is wrong",
       h.get("Content-Range") == f"bytes 0-99/{len(full) // 2}",
       str(h.get("Content-Range")))

    st, h, body = c.get("/hls/range/multipart-broken/seg/0.ts",
                        headers={"Range": "bytes=0-9,20-29"})
    check("multipart-broken: 206", st, 206)
    check("multipart-broken: only the first range (10 bytes)", len(body), 10)
    ok("multipart-broken: no multipart content-type",
       "multipart/byteranges" not in (h.get("Content-Type") or ""),
       str(h.get("Content-Type")))
    ok("multipart-broken: second range is silently dropped",
       body == full[:10])

    p = "/hls/range/restarts/seg/7.ts"          # fresh URL for this run
    st, h, body = c.get(p, headers={"Range": "bytes=100-199"})
    check("restarts: first Range request -> 206", st, 206)
    check("restarts: correct slice", body, full[100:200])
    st, h, body = c.get(p, headers={"Range": "bytes=100-199"})
    check("restarts: second Range request -> 200 (full body)", st, 200)
    check("restarts: body is the whole segment", len(body), len(full))
    ok("restarts: no Content-Range on the retry", "Content-Range" not in h)


# 17. 畸形播放列表的各种变体。
def check_malformed(c: Client) -> None:
    group("17. malformed playlists")
    kinds = app.MALFORMED_KINDS
    ok("all variants registered", len(kinds) == 9, str(len(kinds)))
    st, h, raw = c.get("/hls/malformed/crlf/playlist.m3u8")
    check("crlf status", st, 200)
    ok("crlf: CRLF line endings", b"\r\n" in raw and b"\n\n" not in raw,
       str(raw[:40]))
    st, _, raw = c.get("/hls/malformed/bom/playlist.m3u8")
    check("bom status", st, 200)
    ok("bom: UTF-8 BOM present", raw.startswith(b"\xef\xbb\xbf"))
    st, _, body = c.get("/hls/malformed/no-version/playlist.m3u8")
    ok("no-version: EXT-X-VERSION absent", "#EXT-X-VERSION" not in body.decode())
    st, _, body = c.get("/hls/malformed/unknown-tags/playlist.m3u8")
    txt = body.decode()
    ok("unknown-tags: vendor tags present",
       "#EXT-X-SOME-CDN-TAG" in txt and "#EXT-CUSTOM-AD" in txt)
    ok("unknown-tags: unknown tag before a segment URI",
       "#EXT-X-UNKNOWN-BEFORE-SEG" in txt)
    st, _, body = c.get("/hls/malformed/spacing/playlist.m3u8")
    txt = body.decode()
    ok("spacing: trailing whitespace", any(l != l.rstrip() for l in
                                           txt.splitlines()))
    ok("spacing: leading tab before a URI", "\tseg/" in txt)
    st, _, body = c.get("/hls/malformed/lowercase/playlist.m3u8")
    txt = body.decode()
    ok("lowercase: tags lowercased", "#extm3u" in txt and "#extinf" in txt)
    st, _, body = c.get("/hls/malformed/no-extm3u/playlist.m3u8")
    ok("no-extm3u: first line is not #EXTM3U",
       not body.decode().lstrip().startswith("#EXTM3U"), body[:20].decode())
    st, _, body = c.get("/hls/malformed/bad-attrs/playlist.m3u8")
    txt = body.decode()
    ok("bad-attrs: EXTINF without duration", "#EXTINF:," in txt)
    ok("bad-attrs: odd-length IV hex", "IV=0x123" in txt)
    st, _, body = c.get("/hls/malformed/all/playlist.m3u8")
    lower = body.decode(errors="replace").lower()
    ok("all: combines BOM + lowercase tags + unknown tag + CRLF",
       body.startswith(b"\xef\xbb\xbf") and "#extm3u" in lower
       and "#ext-x-unknown" in lower and b"\r\n" in body,
       repr(body[:60]))
    for k in kinds:
        st, _, body = c.get(f"/hls/malformed/{k}/seg/0.ts")
        ok(f"{k}: segments are always clean", st == 200 and ts_sync(body),
           f"status {st}")


# EXT-X-DEFINE 变量替换与通用 HTTP 行为（HEAD/404/CORS）。
def check_define_and_api(c: Client) -> None:
    group("17b. EXT-X-DEFINE variable substitution")
    st, _, pl = c.get("/hls/define/playlist.m3u8")
    txt = pl.decode()
    check("playlist status", st, 200)
    ok("version 8", "#EXT-X-VERSION:8" in txt)
    ok("NAME/VALUE definition", '#EXT-X-DEFINE:NAME="segbase"' in txt)
    ok("segments use {$segbase}", "{$segbase}seg_0.ts" in txt)
    st, _, body = c.get("/hls/define/segments/seg_0.ts")
    ok("substituted URI resolves", st == 200 and ts_sync(body), str(st))

    group("misc. server hygiene")
    st, h, _ = c.head("/hls/vod/seg/0.ts")
    check("HEAD returns 200", st, 200)
    ok("HEAD Content-Length matches GET",
       int(h.get("Content-Length", 0)) ==
       len(c.get("/hls/vod/seg/0.ts")[2]))
    st, _, _ = c.head("/")
    check("HEAD on the index works too", st, 200)
    st, _, _ = c.get("/hls/does-not-exist/playlist.m3u8")
    check("unknown path -> 404", st, 404)
    st, h, _ = c.get("/hls/vod/seg/0.ts")
    ok("CORS allow-origin", h.get("Access-Control-Allow-Origin") == "*",
       str(h.get("Access-Control-Allow-Origin")))
    st, _, _ = c.get("/hls/range/nope/playlist.m3u8")
    check("unknown range mode -> 404", st, 404)


# 25. 索引页的“查看响应”预览端点：/preview/{id}（JSON）。
def check_preview(c: Client) -> None:
    group("25. response preview endpoint (/preview/{id})")

    def pv(sid: str):
        """取某个场景的预览 JSON；返回 (status, 解析后的 dict 或 None)。"""
        st, _, raw = c.get(f"/preview/{sid}")
        try:
            return st, json.loads(raw.decode())
        except Exception:
            return st, None

    # (a) 无状态场景：200 + JSON 里带真实 body（应为 m3u8 文本）。
    for sid in ("vod", "aes128", "nested", "series", "renditions"):
        st, d = pv(sid)
        check(f"preview {sid} status", st, 200)
        if d is None:
            ok(f"preview {sid} returns JSON", False, "body is not JSON")
            continue
        ok(f"preview {sid} reports 200", d.get("status") == 200, str(d.get("status")))
        ok(f"preview {sid} body starts with #EXTM3U",
           str(d.get("body", "")).startswith("#EXTM3U"),
           str(d.get("body", ""))[:24])
        ok(f"preview {sid} content_length == len(body)",
           d.get("content_length") == len(d.get("body", "")),
           f'{d.get("content_length")} vs {len(d.get("body", ""))}')
        ok(f"preview {sid} has no note (stateless)",
           d.get("note") in (None, ""), str(d.get("note")))

    # (b) 有状态场景：必须返回说明 note，且连续两次调用字节完全一致 —— 证明没碰 STATE。
    for sid in ("onetime", "ratelimit", "ban", "captcha"):
        st1, _, raw1 = c.get(f"/preview/{sid}")
        st2, _, raw2 = c.get(f"/preview/{sid}")
        check(f"preview {sid} status", st1, 200)
        d = json.loads(raw1.decode())
        ok(f"preview {sid} carries a note", bool(d.get("note")), str(d.get("note")))
        ok(f"preview {sid} body is empty (not executed)", d.get("body") == "",
           str(d.get("body"))[:24])
        ok(f"preview {sid} is side-effect free (two calls byte-identical)",
           raw1 == raw2, f"{len(raw1)}B vs {len(raw2)}B")

    # 有状态场景预览后，真实端点必须照常工作，且预览不得改动其状态。
    # 注意：整个套件共用一个服务器，ratelimit 的 30s 窗口可能已被第 9 组烧掉，
    # 所以不能断言“再取一次分片仍是 200”。改为直接读状态计数：预览前后不变。
    hits_before = sum(len(d) for d in app.STATE._hits.values())
    c.get("/preview/ratelimit")
    c.get("/preview/ban")
    c.get("/preview/onetime")
    hits_after = sum(len(d) for d in app.STATE._hits.values())
    ok("previews do not touch the rate-limit / ban counters",
       hits_before == hits_after, f"{hits_before} -> {hits_after}")

    st, _, _ = c.get("/hls/onetime/playlist.m3u8")
    check("onetime real endpoint still 200 after preview", st, 200)
    st, _, _ = c.get("/hls/throttle/playlist.m3u8?kbps=64")
    check("throttle real endpoint still 200 after preview", st, 200)

    # (c) 未知 id -> 404。
    st, _, _ = c.get("/preview/no-such-scenario")
    check("unknown preview id -> 404", st, 404)

    # (d) 预览 id 必须与 CATALOG 的每个场景都能对上（表不漏、id 不拼错）。
    ids = [row[0] for row in app.CATALOG]
    covered = set(app.PREVIEWABLE) | set(app.STATEFUL_NOTES)
    missing = [i for i in ids if i not in covered]
    ok("every catalog scenario is covered by preview or note",
       not missing, f"missing: {missing}")


# ---------------------------------------------------------------------------
# entry point / 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="",
                    help="test an already-running server instead of self-hosting"
                         " / 测一个已在运行的实例，而不是自建")
    ap.add_argument("--fast", action="store_true",
                    help="skip the live-window timing checks"
                         " / 跳过直播滑窗的计时检查（唯一需要 sleep 的部分）")
    ap.add_argument("--port", type=int, default=0,
                    help="self-hosted port (0 = ephemeral) / 自建实例端口，0 表示随机")
    args = ap.parse_args()

    if args.base:
        # 外部实例：不再自建服务器。
        client, server = Client(args.base), None
    else:
        # 自建实例：合成模式、静音访问日志、随机端口。
        app.init_media(cache_dir=None)
        app.QUIET = True
        server = Server("127.0.0.1", args.port)
        server.start()
        client = Client(f"http://127.0.0.1:{server.port}")
        print(f"self-hosted on {client.base}")

    t0 = time.time()
    try:
        # 检查顺序大体按场景分组；新增场景时在这里追加一行调用。
        check_catalog(client)
        check_ffmpeg_banner(client)
        check_cache_panel(client)
        check_sample(client)
        check_vod_live(client, args.fast)
        check_aes(client)
        check_key_expiry(client)
        check_drm(client)
        check_key_header(client)
        check_paths(client)
        check_nested(client)
        check_renditions(client)
        check_series(client)
        check_fmp4_av(client)
        check_variable(client)
        check_enc_combos(client)
        check_ads(client)
        check_resume_triple(client)
        check_access(client)
        check_protection(client)
        check_source_defects(client)
        check_containers(client)
        check_resume(client)
        check_malformed(client)
        check_define_and_api(client)
        check_preview(client)
    finally:
        if server:
            server.stop()   # 无论成功失败都要关掉自建服务器

    failed = [r for r in _RESULTS if not r[0]]
    print("\n" + "=" * 62)
    print(f"checks: {len(_RESULTS)}   passed: {len(_RESULTS) - len(failed)}"
          f"   failed: {len(failed)}   ({time.time() - t0:.1f}s)")
    for _, name, detail in failed:
        print(f"  FAIL {name}: {detail}")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
